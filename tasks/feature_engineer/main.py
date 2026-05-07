"""Compute derived atmospheric features from raw ERA5 and storm event data.

Reads raw data from S3, computes convective indices, wind shear, spatial
gradients, and temporal tendencies, then writes processed features back to S3
as Parquet files.
"""

import gzip
import io
import json
import logging
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta

import boto3
import cfgrib
import numpy as np
import pandas as pd
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("feature-engineer")

S3_BUCKET = os.environ["S3_BUCKET"]
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "processed/")

GRID_RES = 0.25
LEAD_HOURS = [1, 2, 3]
PRESSURE_LEVELS = [500, 700, 850, 925]

s3 = boto3.client("s3")


# ── Geo helpers ────────────────────────────────────────────────

def snap_to_grid(lat: float, lon: float) -> tuple[float, float]:
    slat = round(round(lat / GRID_RES) * GRID_RES, 4)
    slon = round(round(lon / GRID_RES) * GRID_RES, 4)
    return slat, slon


def grid_patch(lat: float, lon: float) -> list[tuple[float, float]]:
    """3x3 grid points centred on (lat, lon)."""
    offsets = [(-1, -1), (-1, 0), (-1, 1),
               (0, -1), (0, 0), (0, 1),
               (1, -1), (1, 0), (1, 1)]
    return [
        (round(lat + dlat * GRID_RES, 4), round(lon + dlon * GRID_RES, 4))
        for dlat, dlon in offsets
    ]


# ── S3 data loading ───────────────────────────────────────────

def list_s3_keys(prefix: str) -> list[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def load_json_from_s3(key: str) -> list[dict]:
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read())


def load_gzip_json_from_s3(key: str) -> list[dict]:
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    raw = gzip.decompress(resp["Body"].read())
    return json.loads(raw)


def download_to_tempfile(key: str, suffix: str) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    s3.download_fileobj(S3_BUCKET, key, tmp)
    tmp.close()
    return tmp.name


# ── Event loading ─────────────────────────────────────────────

SEVERE_THRESHOLD = int(os.environ.get("SEVERE_THRESHOLD", "100"))


def load_lightning_flashes() -> pd.DataFrame:
    prefix = f"{RAW_PREFIX}lightning/"
    keys = list_s3_keys(prefix)
    all_flashes = []
    for key in keys:
        if key.endswith("/flashes.json.gz"):
            flashes = load_gzip_json_from_s3(key)
            all_flashes.extend(flashes)

    if not all_flashes:
        return pd.DataFrame()

    df = pd.DataFrame(all_flashes)
    df["datetime"] = pd.to_datetime(df["datetime"], format="ISO8601", utc=True)
    df = df.dropna(subset=["latitude", "longitude", "datetime"])
    logger.info("Loaded %d lightning flashes", len(df))
    return df


def label_severe_storms(flashes_df: pd.DataFrame) -> pd.DataFrame:
    """Cluster flashes by grid cell and hour, label cells with >SEVERE_THRESHOLD flashes."""
    if flashes_df.empty:
        return pd.DataFrame(columns=["datetime", "grid_lat", "grid_lon", "label", "flash_count"])
    df = flashes_df.copy()
    df["grid_lat"] = df.apply(
        lambda r: snap_to_grid(r["latitude"], r["longitude"])[0], axis=1,
    )
    df["grid_lon"] = df.apply(
        lambda r: snap_to_grid(r["latitude"], r["longitude"])[1], axis=1,
    )
    df["hour_floor"] = df["datetime"].dt.floor("h")

    counts = (
        df.groupby(["grid_lat", "grid_lon", "hour_floor"])
        .size()
        .reset_index(name="flash_count")
    )

    severe = counts[counts["flash_count"] >= SEVERE_THRESHOLD].copy()
    severe = severe.rename(columns={"hour_floor": "datetime"})
    severe["label"] = 1
    logger.info(
        "Found %d severe storm cell-hours (>=%d flashes) from %d total cell-hours",
        len(severe), SEVERE_THRESHOLD, len(counts),
    )
    return severe[["datetime", "grid_lat", "grid_lon", "label", "flash_count"]]


# ── ERA5 loading ──────────────────────────────────────────────

def open_era5_grib(key: str) -> xr.Dataset:
    path = download_to_tempfile(key, ".grib")
    datasets = cfgrib.open_datasets(
        path,
        backend_kwargs={"indexpath": ""},
    )
    if len(datasets) == 1:
        return datasets[0]
    merged = xr.merge(datasets, compat="override", join="outer")
    return merged


def era5_single_key(year: int, month: int) -> str:
    return f"{RAW_PREFIX}era5/single-levels/{year}/{month:02d}.grib"


def era5_pressure_key(year: int, month: int) -> str:
    return f"{RAW_PREFIX}era5/pressure-levels/{year}/{month:02d}.grib"


# ── Feature extraction ────────────────────────────────────────

SKIP_VARS = {"cin", "tp", "cp", "kx", "totalx"}


def extract_point_features(
    ds: xr.Dataset, lat: float, lon: float, time: np.datetime64,
) -> dict[str, float]:
    """Extract all variables at a single grid point and time."""
    try:
        point = ds.sel(latitude=lat, longitude=lon, time=time, method="nearest")
    except (KeyError, ValueError):
        return {}

    features = {}
    for var in ds.data_vars:
        if var in SKIP_VARS:
            continue
        val = point[var].values
        if not np.isscalar(val):
            val = val.flat[0]
        features[var] = float(val) if np.isfinite(val) else np.nan
    return features


def extract_patch_features(
    ds: xr.Dataset, lat: float, lon: float, time: np.datetime64,
) -> dict[str, float]:
    """Extract features for the 3x3 grid patch, computing centre + spatial stats."""
    patch_points = grid_patch(lat, lon)
    values_by_var: dict[str, list[float]] = {}

    for plat, plon in patch_points:
        point_feats = extract_point_features(ds, plat, plon, time)
        for var, val in point_feats.items():
            values_by_var.setdefault(var, []).append(val)

    features = {}
    centre_feats = extract_point_features(ds, lat, lon, time)
    for var, val in centre_feats.items():
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
            features[f"{var}_mean"] = np.nan
            features[f"{var}_std"] = np.nan
            features[f"{var}_max"] = np.nan
            features[f"{var}_min"] = np.nan

    return features


def compute_wind_shear(pressure_feats: dict[str, dict[str, float]]) -> dict[str, float]:
    """Compute bulk wind shear between pressure levels."""
    shear = {}
    level_pairs = [(925, 700), (925, 500)]
    for low, high in level_pairs:
        low_key = str(low)
        high_key = str(high)
        if low_key in pressure_feats and high_key in pressure_feats:
            du = pressure_feats[high_key].get("u_centre", 0) - pressure_feats[low_key].get("u_centre", 0)
            dv = pressure_feats[high_key].get("v_centre", 0) - pressure_feats[low_key].get("v_centre", 0)
            shear[f"wind_shear_{low}_{high}"] = math.sqrt(du**2 + dv**2)
    return shear


def compute_temporal_tendency(
    features_t: dict[str, float], features_t_prev: dict[str, float],
) -> dict[str, float]:
    """Compute 1-hour change for key variables."""
    tendency_vars = ["cape_centre", "cin_centre", "t2m_centre", "sp_centre"]
    tendencies = {}
    for var in tendency_vars:
        curr = features_t.get(var)
        prev = features_t_prev.get(var)
        if curr is not None and prev is not None and np.isfinite(curr) and np.isfinite(prev):
            tendencies[f"{var}_tendency"] = curr - prev
    return tendencies


def encode_time_features(dt: datetime) -> dict[str, float]:
    """Cyclical encoding of hour-of-day and day-of-year."""
    hour = dt.hour + dt.minute / 60.0
    doy = dt.timetuple().tm_yday
    return {
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "doy_sin": math.sin(2 * math.pi * doy / 365),
        "doy_cos": math.cos(2 * math.pi * doy / 365),
    }


# ── Negative sampling ─────────────────────────────────────────

def generate_negatives(
    events_df: pd.DataFrame, n_per_positive: int = 3,
) -> pd.DataFrame:
    """Generate negative samples: same grid locations, shifted in time.

    For each positive event, pick random times from the same month that had
    no storm activity within 6 hours and 50km.
    """
    rng = np.random.default_rng(42)
    negatives = []

    for _, row in events_df.iterrows():
        event_dt = row["datetime"]
        lat, lon = row["grid_lat"], row["grid_lon"]

        for _ in range(n_per_positive):
            offset_days = rng.integers(-14, 15)
            offset_hours = rng.integers(0, 24)
            neg_dt = event_dt + timedelta(days=int(offset_days), hours=int(offset_hours))

            time_diff = abs((neg_dt - events_df["datetime"]).dt.total_seconds())
            too_close = (time_diff < 6 * 3600) & (
                (events_df["grid_lat"] == lat) & (events_df["grid_lon"] == lon)
            )
            if too_close.any():
                continue

            negatives.append({
                "datetime": neg_dt,
                "grid_lat": lat,
                "grid_lon": lon,
                "label": 0,
            })

    return pd.DataFrame(negatives)


# ── Main pipeline ─────────────────────────────────────────────

def process_month(
    events: pd.DataFrame,
    year: int,
    month: int,
) -> pd.DataFrame:
    """Process all events in a given month, extracting ERA5 features."""
    single_key = era5_single_key(year, month)
    pressure_key = era5_pressure_key(year, month)

    try:
        ds_single = open_era5_grib(single_key)
    except Exception as exc:
        logger.warning("Cannot open single-level GRIB for %d-%02d: %s", year, month, exc)
        return pd.DataFrame()

    try:
        ds_pressure = open_era5_grib(pressure_key)
    except Exception as exc:
        logger.warning("Cannot open pressure-level GRIB for %d-%02d: %s", year, month, exc)
        ds_pressure = None

    rows = []
    for _, event in events.iterrows():
        lat, lon = event["grid_lat"], event["grid_lon"]
        event_time = pd.Timestamp(event["datetime"])

        all_features = {
            "event_datetime": event_time.isoformat(),
            "grid_lat": lat,
            "grid_lon": lon,
            "label": event["label"],
        }

        if "flash_count" in event:
            all_features["flash_count"] = event["flash_count"]

        prev_features = None
        for lead_h in LEAD_HOURS:
            t = event_time - pd.Timedelta(hours=lead_h)
            t_np = np.datetime64(t.to_pydatetime())

            feats = extract_patch_features(ds_single, lat, lon, t_np)
            prefixed = {f"h{lead_h}_{k}": v for k, v in feats.items()}
            all_features.update(prefixed)

            if prev_features is not None:
                tendencies = compute_temporal_tendency(feats, prev_features)
                tend_prefixed = {f"h{lead_h}_{k}": v for k, v in tendencies.items()}
                all_features.update(tend_prefixed)
            prev_features = feats

            if ds_pressure is not None:
                pressure_by_level = {}
                for level in PRESSURE_LEVELS:
                    try:
                        ds_lev = ds_pressure.sel(isobaricInhPa=level)
                        pfeats = extract_point_features(ds_lev, lat, lon, t_np)
                        prefixed_p = {f"h{lead_h}_p{level}_{k}": v for k, v in pfeats.items()}
                        all_features.update(prefixed_p)
                        pressure_by_level[str(level)] = {
                            "u_centre": pfeats.get("u", 0),
                            "v_centre": pfeats.get("v", 0),
                        }
                    except (KeyError, ValueError):
                        pass

                shear = compute_wind_shear(pressure_by_level)
                shear_prefixed = {f"h{lead_h}_{k}": v for k, v in shear.items()}
                all_features.update(shear_prefixed)

        all_features.update(encode_time_features(event_time.to_pydatetime()))
        rows.append(all_features)

    ds_single.close()
    if ds_pressure is not None:
        ds_pressure.close()

    return pd.DataFrame(rows)


def upload_parquet(df: pd.DataFrame, key: str) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue())
    logger.info("Uploaded %d rows to s3://%s/%s", len(df), S3_BUCKET, key)


def main() -> None:
    logger.info("Feature engineer starting")
    logger.info("  S3 bucket: %s", S3_BUCKET)
    logger.info("  Raw prefix: %s", RAW_PREFIX)
    logger.info("  Output prefix: %s", OUTPUT_PREFIX)
    logger.info("  Severe threshold: %d strokes", SEVERE_THRESHOLD)

    strokes_df = load_lightning_flashes()
    if strokes_df.empty:
        logger.error("No lightning flashes found - cannot proceed")
        sys.exit(1)

    severe_df = label_severe_storms(strokes_df)
    if severe_df.empty:
        logger.error("No severe storm cell-hours found - cannot proceed")
        sys.exit(1)

    logger.info("Generating negative samples")
    negatives_df = generate_negatives(severe_df)
    logger.info("Generated %d negative samples", len(negatives_df))

    all_samples = pd.concat([
        severe_df[["datetime", "grid_lat", "grid_lon", "label"]],
        negatives_df,
    ], ignore_index=True)
    all_samples["year"] = all_samples["datetime"].dt.year
    all_samples["month"] = all_samples["datetime"].dt.month

    total_rows = 0
    for (year, month), group in all_samples.groupby(["year", "month"]):
        logger.info("Processing %d-%02d (%d samples)", year, month, len(group))
        result_df = process_month(group, int(year), int(month))
        if result_df.empty:
            logger.warning("No features extracted for %d-%02d", year, month)
            continue

        out_key = f"{OUTPUT_PREFIX}features/{year}/{month:02d}.parquet"
        upload_parquet(result_df, out_key)
        total_rows += len(result_df)

    logger.info("Feature engineer complete - %d total rows across all months", total_rows)


if __name__ == "__main__":
    main()
