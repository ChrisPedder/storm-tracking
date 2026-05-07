"""Score severe storm probability over Switzerland using the latest IFS forecast."""

import io
import json
import logging
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import boto3
import cfgrib
import lightgbm as lgb
import numpy as np
import pandas as pd
import xarray as xr
from ecmwf.opendata import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("storm-forecast")

S3_BUCKET = os.environ["S3_BUCKET"]
MODEL_PREFIX = os.environ.get("MODEL_PREFIX", "output/model/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "forecast/")

BBOX_NORTH = float(os.environ.get("BBOX_NORTH", "48.3"))
BBOX_SOUTH = float(os.environ.get("BBOX_SOUTH", "45.3"))
BBOX_WEST = float(os.environ.get("BBOX_WEST", "5.5"))
BBOX_EAST = float(os.environ.get("BBOX_EAST", "11.0"))

GRID_RES = 0.25
PRESSURE_LEVELS = [500, 700, 850, 925]

# IFS has 3-hourly steps; map to h1/h2/h3 feature prefixes.
LEAD_OFFSETS_H = [3, 6, 9]

# Forecast steps (hours from base time) covering daytime convective period.
TARGET_STEPS_H = list(range(9, 25, 3))

# Variables to exclude when iterating over data_vars.
SKIP_VARS = frozenset({
    "cin", "tp", "cp", "kx", "totalx",
    "number", "surface", "valid_time", "time", "step",
})

s3 = boto3.client("s3")


# ── Grid helpers ──────────────────────────────────────────────

def snap_to_grid(lat: float, lon: float) -> tuple[float, float]:
    slat = round(round(lat / GRID_RES) * GRID_RES, 4)
    slon = round(round(lon / GRID_RES) * GRID_RES, 4)
    return slat, slon


def grid_patch(lat: float, lon: float) -> list[tuple[float, float]]:
    offsets = [(-1, -1), (-1, 0), (-1, 1),
               (0, -1),  (0, 0),  (0, 1),
               (1, -1),  (1, 0),  (1, 1)]
    return [
        (round(lat + dlat * GRID_RES, 4), round(lon + dlon * GRID_RES, 4))
        for dlat, dlon in offsets
    ]


def build_grid() -> list[tuple[float, float]]:
    lats = np.arange(BBOX_SOUTH, BBOX_NORTH + GRID_RES / 2, GRID_RES)
    lons = np.arange(BBOX_WEST, BBOX_EAST + GRID_RES / 2, GRID_RES)
    return [snap_to_grid(la, lo) for la in lats for lo in lons]


# ── Feature extraction ────────────────────────────────────────

def extract_point(ds_slice: xr.Dataset, lat: float, lon: float) -> dict[str, float]:
    try:
        point = ds_slice.sel(latitude=lat, longitude=lon, method="nearest")
    except (KeyError, ValueError):
        return {}
    features = {}
    for var in ds_slice.data_vars:
        if var in SKIP_VARS:
            continue
        val = point[var].values
        if not np.isscalar(val):
            val = val.flat[0]
        features[var] = float(val) if np.isfinite(val) else np.nan
    return features


def extract_patch(ds_slice: xr.Dataset, lat: float, lon: float) -> dict[str, float]:
    patch_points = grid_patch(lat, lon)
    values_by_var: dict[str, list[float]] = {}
    for plat, plon in patch_points:
        pf = extract_point(ds_slice, plat, plon)
        for var, val in pf.items():
            values_by_var.setdefault(var, []).append(val)

    features = {}
    centre = extract_point(ds_slice, lat, lon)
    for var, val in centre.items():
        features[f"{var}_centre"] = val

    for var, vals in values_by_var.items():
        arr = np.array(vals)
        valid = arr[np.isfinite(arr)]
        if len(valid) > 0:
            features[f"{var}_mean"] = float(np.mean(valid))
            features[f"{var}_std"] = float(np.std(valid))
            features[f"{var}_max"] = float(np.max(valid))
            features[f"{var}_min"] = float(np.min(valid))
        else:
            for suffix in ("_mean", "_std", "_max", "_min"):
                features[f"{var}{suffix}"] = np.nan
    return features


def compute_wind_shear(pressure_feats: dict[str, dict[str, float]]) -> dict[str, float]:
    shear = {}
    for low, high in [(925, 700), (925, 500)]:
        lk, hk = str(low), str(high)
        if lk in pressure_feats and hk in pressure_feats:
            du = pressure_feats[hk].get("u_centre", 0) - pressure_feats[lk].get("u_centre", 0)
            dv = pressure_feats[hk].get("v_centre", 0) - pressure_feats[lk].get("v_centre", 0)
            shear[f"wind_shear_{low}_{high}"] = math.sqrt(du ** 2 + dv ** 2)
    return shear


def compute_temporal_tendency(current: dict, previous: dict) -> dict[str, float]:
    tendency_vars = ["cape_centre", "cin_centre", "t2m_centre", "sp_centre"]
    tendencies = {}
    for var in tendency_vars:
        c, p = current.get(var), previous.get(var)
        if c is not None and p is not None and np.isfinite(c) and np.isfinite(p):
            tendencies[f"{var}_tendency"] = c - p
    return tendencies


def encode_time_features(dt: datetime) -> dict[str, float]:
    hour = dt.hour + dt.minute / 60.0
    doy = dt.timetuple().tm_yday
    return {
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "doy_sin": math.sin(2 * math.pi * doy / 365),
        "doy_cos": math.cos(2 * math.pi * doy / 365),
    }


# ── ECMWF Open Data ──────────────────────────────────────────

def required_steps() -> list[int]:
    needed = set(TARGET_STEPS_H)
    for target in TARGET_STEPS_H:
        for lead in LEAD_OFFSETS_H:
            needed.add(target - lead)
    return sorted(s for s in needed if s >= 0)


def download_forecast(work_dir: str) -> tuple[str, str]:
    client = Client(source="ecmwf", model="ifs", resol="0p25")
    steps = required_steps()
    logger.info("Downloading IFS forecast, steps %s", steps)

    sfc_path = os.path.join(work_dir, "sfc.grib2")
    pl_path = os.path.join(work_dir, "pl.grib2")

    client.retrieve(
        type="fc",
        step=steps,
        levtype="sfc",
        param=["mucape", "msl", "2t", "sp", "2d", "tcwv", "10u", "10v"],
        target=sfc_path,
    )
    logger.info("Downloaded single-level fields (%.1f MB)", os.path.getsize(sfc_path) / 1e6)

    client.retrieve(
        type="fc",
        step=steps,
        levtype="pl",
        param=["t", "q", "u", "v", "gh"],
        levelist=PRESSURE_LEVELS,
        target=pl_path,
    )
    logger.info("Downloaded pressure-level fields (%.1f MB)", os.path.getsize(pl_path) / 1e6)

    return sfc_path, pl_path


# ── GRIB loading & variable mapping ──────────────────────────

IFS_TO_ERA5 = {
    "mucape": "cape",
    "2t": "t2m",
    "2d": "d2m",
    "10u": "u10",
    "10v": "v10",
    "gh": "z",
}


def open_ifs_grib(path: str) -> xr.Dataset:
    datasets = cfgrib.open_datasets(path, backend_kwargs={"indexpath": ""})
    ds = datasets[0] if len(datasets) == 1 else xr.merge(
        datasets, compat="override", join="outer",
    )

    rename = {k: v for k, v in IFS_TO_ERA5.items()
              if k in ds.data_vars and v not in ds.data_vars}
    if rename:
        ds = ds.rename(rename)
        logger.info("Renamed IFS variables: %s", rename)

    # Geopotential height (m) → geopotential (m²/s²)
    if "z" in ds.data_vars and ds["z"].attrs.get("units", "") != "m**2 s**-2":
        ds["z"] = ds["z"] * 9.80665

    margin = GRID_RES * 2
    ds = ds.sel(
        latitude=slice(BBOX_NORTH + margin, BBOX_SOUTH - margin),
        longitude=slice(BBOX_WEST - margin, BBOX_EAST + margin),
    )
    return ds


def step_slice(ds: xr.Dataset, step_hours: int) -> xr.Dataset:
    if "step" not in ds.dims:
        return ds
    return ds.sel(step=pd.Timedelta(hours=step_hours), method="nearest")


def get_base_time(ds: xr.Dataset) -> datetime:
    t = ds.coords.get("time")
    if t is not None:
        val = t.values
        if hasattr(val, "item"):
            val = val.item()
        return pd.Timestamp(val).to_pydatetime().replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


# ── Model ─────────────────────────────────────────────────────

def load_model() -> lgb.Booster:
    key = f"{MODEL_PREFIX}model.lgb"
    logger.info("Loading model from s3://%s/%s", S3_BUCKET, key)
    with tempfile.NamedTemporaryFile(suffix=".lgb", delete=False) as tmp:
        s3.download_fileobj(S3_BUCKET, key, tmp)
        tmp_path = tmp.name
    model = lgb.Booster(model_file=tmp_path)
    os.unlink(tmp_path)
    logger.info("Model loaded: %d features, %d iterations",
                model.num_feature(), model.best_iteration)
    return model


# ── Scoring ───────────────────────────────────────────────────

def extract_grid_features(
    ds_sfc: xr.Dataset,
    ds_pl: xr.Dataset,
    target_step: int,
    base_time: datetime,
    grid: list[tuple[float, float]],
) -> pd.DataFrame:
    valid_time = base_time + timedelta(hours=target_step)
    rows = []

    for lat, lon in grid:
        features: dict[str, float] = {}
        prev_feats = None

        for i, lead_h in enumerate(LEAD_OFFSETS_H, start=1):
            step_h = target_step - lead_h
            sfc_sl = step_slice(ds_sfc, step_h)
            feats = extract_patch(sfc_sl, lat, lon)
            features.update({f"h{i}_{k}": v for k, v in feats.items()})

            if prev_feats is not None:
                tend = compute_temporal_tendency(feats, prev_feats)
                features.update({f"h{i}_{k}": v for k, v in tend.items()})
            prev_feats = feats

            pres_by_level: dict[str, dict[str, float]] = {}
            for lev in PRESSURE_LEVELS:
                try:
                    pl_sl = step_slice(ds_pl.sel(isobaricInhPa=lev), step_h)
                except (KeyError, ValueError):
                    continue
                pf = extract_point(pl_sl, lat, lon)
                features.update({f"h{i}_p{lev}_{k}": v for k, v in pf.items()})
                pres_by_level[str(lev)] = {
                    "u_centre": pf.get("u", 0),
                    "v_centre": pf.get("v", 0),
                }
            shear = compute_wind_shear(pres_by_level)
            features.update({f"h{i}_{k}": v for k, v in shear.items()})

        features.update(encode_time_features(valid_time))
        rows.append({"lat": lat, "lon": lon, "valid_time": valid_time, **features})

    return pd.DataFrame(rows)


def score(df: pd.DataFrame, model: lgb.Booster) -> np.ndarray:
    model_features = model.feature_name()
    for col in model_features:
        if col not in df.columns:
            df[col] = np.nan
    return model.predict(df[model_features])


# ── Output ────────────────────────────────────────────────────

def build_geojson(results: pd.DataFrame) -> dict:
    peak = results.loc[results.groupby(["lat", "lon"])["probability"].idxmax()]
    features = []
    for _, row in peak.iterrows():
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(row["lon"], 4), round(row["lat"], 4)],
            },
            "properties": {
                "probability": round(row["probability"], 4),
                "peak_time": row["valid_time"].isoformat(),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def upload_results(results: pd.DataFrame, base_time: datetime, model: lgb.Booster) -> None:
    date_str = base_time.strftime("%Y%m%d_%Hz")

    parquet_key = f"{OUTPUT_PREFIX}{date_str}/probabilities.parquet"
    buf = io.BytesIO()
    results.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)
    s3.put_object(Bucket=S3_BUCKET, Key=parquet_key, Body=buf.getvalue())
    logger.info("Uploaded %s", parquet_key)

    geojson = build_geojson(results)
    geojson_key = f"{OUTPUT_PREFIX}{date_str}/forecast.geojson"
    s3.put_object(
        Bucket=S3_BUCKET, Key=geojson_key,
        Body=json.dumps(geojson, indent=2).encode(),
        ContentType="application/geo+json",
    )
    logger.info("Uploaded %s", geojson_key)

    max_by_cell = results.groupby(["lat", "lon"])["probability"].max()
    summary = {
        "base_time": base_time.isoformat(),
        "valid_times": [
            (base_time + timedelta(hours=s)).isoformat() for s in TARGET_STEPS_H
        ],
        "grid_points": len(max_by_cell),
        "model_features": model.num_feature(),
        "max_probability": round(float(max_by_cell.max()), 4),
        "cells_above_50pct": int((max_by_cell >= 0.5).sum()),
        "cells_above_30pct": int((max_by_cell >= 0.3).sum()),
        "high_risk_cells": sorted(
            [
                {"lat": lat, "lon": lon, "probability": round(float(p), 4)}
                for (lat, lon), p in max_by_cell.items() if p >= 0.3
            ],
            key=lambda c: -c["probability"],
        ),
    }
    summary_key = f"{OUTPUT_PREFIX}{date_str}/summary.json"
    s3.put_object(
        Bucket=S3_BUCKET, Key=summary_key,
        Body=json.dumps(summary, indent=2).encode(),
    )
    logger.info("Uploaded %s", summary_key)

    logger.info(
        "Forecast complete: max_prob=%.4f, %d cells >=50%%, %d cells >=30%%",
        summary["max_probability"],
        summary["cells_above_50pct"],
        summary["cells_above_30pct"],
    )


# ── Main ──────────────────────────────────────────────────────

def main() -> None:
    logger.info("Storm forecast starting")
    logger.info("  Bbox: N=%.1f S=%.1f W=%.1f E=%.1f",
                BBOX_NORTH, BBOX_SOUTH, BBOX_WEST, BBOX_EAST)

    grid = build_grid()
    logger.info("Grid: %d points at %.2f° resolution", len(grid), GRID_RES)

    model = load_model()

    work_dir = tempfile.mkdtemp()
    try:
        sfc_path, pl_path = download_forecast(work_dir)
        ds_sfc = open_ifs_grib(sfc_path)
        ds_pl = open_ifs_grib(pl_path)

        base_time = get_base_time(ds_sfc)
        logger.info("Forecast base time: %s", base_time.isoformat())
        logger.info("Single-level vars: %s", list(ds_sfc.data_vars))
        logger.info("Pressure-level vars: %s", list(ds_pl.data_vars))

        all_results = []
        for step in TARGET_STEPS_H:
            vt = base_time + timedelta(hours=step)
            logger.info("Scoring T+%d (%s)", step, vt.strftime("%H:%M UTC"))
            df = extract_grid_features(ds_sfc, ds_pl, step, base_time, grid)
            df["probability"] = score(df, model)
            high = (df["probability"] >= 0.5).sum()
            logger.info("  %d / %d cells >= 0.5", high, len(grid))
            all_results.append(df[["lat", "lon", "valid_time", "probability"]])

        ds_sfc.close()
        ds_pl.close()
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)

    results = pd.concat(all_results, ignore_index=True)
    upload_results(results, base_time, model)


if __name__ == "__main__":
    main()
