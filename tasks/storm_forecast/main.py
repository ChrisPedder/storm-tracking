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


# ── Vectorized feature extraction ─────────────────────────────

def find_nearest_idx(arr: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(arr - value)))


def precompute_grid_indices(
    ds_lats: np.ndarray, ds_lons: np.ndarray, grid: list[tuple[float, float]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Precompute lat/lon array indices for centre and 3x3 patch of every grid point."""
    n = len(grid)
    centre_ilat = np.empty(n, dtype=int)
    centre_ilon = np.empty(n, dtype=int)
    patch_ilat = np.empty((n, 9), dtype=int)
    patch_ilon = np.empty((n, 9), dtype=int)

    offsets = [(-1, -1), (-1, 0), (-1, 1),
               (0, -1),  (0, 0),  (0, 1),
               (1, -1),  (1, 0),  (1, 1)]

    for k, (lat, lon) in enumerate(grid):
        ci = find_nearest_idx(ds_lats, lat)
        cj = find_nearest_idx(ds_lons, lon)
        centre_ilat[k] = ci
        centre_ilon[k] = cj
        for p, (di, dj) in enumerate(offsets):
            pi = find_nearest_idx(ds_lats, lat + di * GRID_RES)
            pj = find_nearest_idx(ds_lons, lon + dj * GRID_RES)
            patch_ilat[k, p] = pi
            patch_ilon[k, p] = pj

    return centre_ilat, centre_ilon, patch_ilat, patch_ilon


def extract_all_patch_features(
    ds_slice: xr.Dataset,
    centre_ilat: np.ndarray, centre_ilon: np.ndarray,
    patch_ilat: np.ndarray, patch_ilon: np.ndarray,
) -> dict[str, np.ndarray]:
    """Extract centre + spatial stats for all grid points at once using numpy indexing."""
    n = len(centre_ilat)
    features: dict[str, np.ndarray] = {}

    for var in ds_slice.data_vars:
        if var in SKIP_VARS:
            continue
        arr = ds_slice[var].values
        while arr.ndim > 2:
            arr = arr[0]
        if arr.ndim != 2:
            continue

        centre_vals = arr[centre_ilat, centre_ilon].astype(float)
        features[f"{var}_centre"] = centre_vals

        patch_vals = arr[patch_ilat, patch_ilon].astype(float)
        with np.errstate(all="ignore"):
            features[f"{var}_mean"] = np.nanmean(patch_vals, axis=1)
            features[f"{var}_std"] = np.nanstd(patch_vals, axis=1)
            features[f"{var}_max"] = np.nanmax(patch_vals, axis=1)
            features[f"{var}_min"] = np.nanmin(patch_vals, axis=1)

    return features


def extract_all_point_features(
    ds_slice: xr.Dataset,
    centre_ilat: np.ndarray, centre_ilon: np.ndarray,
) -> dict[str, np.ndarray]:
    """Extract single-point features for all grid points using numpy indexing."""
    features: dict[str, np.ndarray] = {}
    for var in ds_slice.data_vars:
        if var in SKIP_VARS:
            continue
        arr = ds_slice[var].values
        while arr.ndim > 2:
            arr = arr[0]
        if arr.ndim != 2:
            continue
        features[var] = arr[centre_ilat, centre_ilon].astype(float)
    return features


def compute_wind_shear_vec(
    pres_feats: dict[int, dict[str, np.ndarray]], n: int,
) -> dict[str, np.ndarray]:
    shear: dict[str, np.ndarray] = {}
    for low, high in [(925, 700), (925, 500)]:
        if low in pres_feats and high in pres_feats:
            du = pres_feats[high].get("u", np.zeros(n)) - pres_feats[low].get("u", np.zeros(n))
            dv = pres_feats[high].get("v", np.zeros(n)) - pres_feats[low].get("v", np.zeros(n))
            shear[f"wind_shear_{low}_{high}"] = np.sqrt(du ** 2 + dv ** 2)
    return shear


def compute_tendency_vec(
    curr: dict[str, np.ndarray], prev: dict[str, np.ndarray], n: int,
) -> dict[str, np.ndarray]:
    tendencies: dict[str, np.ndarray] = {}
    for var in ["cape_centre", "t2m_centre", "sp_centre"]:
        if var in curr and var in prev:
            tendencies[f"{var}_tendency"] = curr[var] - prev[var]
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
    indices: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    pl_indices: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> pd.DataFrame:
    valid_time = base_time + timedelta(hours=target_step)
    n = len(grid)
    ci_lat, ci_lon, pi_lat, pi_lon = indices
    pci_lat, pci_lon, _, _ = pl_indices

    columns: dict[str, np.ndarray] = {}
    prev_sfc_feats = None

    for i, lead_h in enumerate(LEAD_OFFSETS_H, start=1):
        step_h = target_step - lead_h
        sfc_sl = step_slice(ds_sfc, step_h)
        sfc_feats = extract_all_patch_features(sfc_sl, ci_lat, ci_lon, pi_lat, pi_lon)
        for k, v in sfc_feats.items():
            columns[f"h{i}_{k}"] = v

        if prev_sfc_feats is not None:
            for k, v in compute_tendency_vec(sfc_feats, prev_sfc_feats, n).items():
                columns[f"h{i}_{k}"] = v
        prev_sfc_feats = sfc_feats

        pres_by_level: dict[int, dict[str, np.ndarray]] = {}
        for lev in PRESSURE_LEVELS:
            try:
                pl_sl = step_slice(ds_pl.sel(isobaricInhPa=lev), step_h)
            except (KeyError, ValueError):
                continue
            pf = extract_all_point_features(pl_sl, pci_lat, pci_lon)
            for k, v in pf.items():
                columns[f"h{i}_p{lev}_{k}"] = v
            pres_by_level[lev] = pf

        for k, v in compute_wind_shear_vec(pres_by_level, n).items():
            columns[f"h{i}_{k}"] = v

    time_feats = encode_time_features(valid_time)
    for k, v in time_feats.items():
        columns[k] = np.full(n, v)

    df = pd.DataFrame(columns)
    df.insert(0, "lat", [p[0] for p in grid])
    df.insert(1, "lon", [p[1] for p in grid])
    df.insert(2, "valid_time", valid_time)
    return df


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

        sfc_lats = ds_sfc.latitude.values
        sfc_lons = ds_sfc.longitude.values
        sfc_idx = precompute_grid_indices(sfc_lats, sfc_lons, grid)
        logger.info("Precomputed single-level indices")

        first_lev = PRESSURE_LEVELS[0]
        pl_step0 = step_slice(ds_pl.sel(isobaricInhPa=first_lev), 0)
        pl_lats = pl_step0.latitude.values
        pl_lons = pl_step0.longitude.values
        pl_idx = precompute_grid_indices(pl_lats, pl_lons, grid)
        logger.info("Precomputed pressure-level indices")

        all_results = []
        for step in TARGET_STEPS_H:
            vt = base_time + timedelta(hours=step)
            logger.info("Scoring T+%d (%s)", step, vt.strftime("%H:%M UTC"))
            df = extract_grid_features(
                ds_sfc, ds_pl, step, base_time, grid, sfc_idx, pl_idx,
            )
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
