"""Build the final ML-ready dataset from processed features.

Reads processed feature Parquet files from S3, applies quality checks,
splits into train/validation/test, and writes partitioned output with a
metadata manifest.
"""

import io
import json
import logging
import os
import sys

import boto3
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("dataset-builder")

S3_BUCKET = os.environ["S3_BUCKET"]
INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "processed/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "output/")

TRAIN_FRAC = 0.7
VAL_FRAC = 0.15
TEST_FRAC = 0.15

MAX_MISSING_FRAC = 0.5

s3 = boto3.client("s3")


def list_s3_keys(prefix: str) -> list[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def load_all_features() -> pd.DataFrame:
    prefix = f"{INPUT_PREFIX}features/"
    keys = [k for k in list_s3_keys(prefix) if k.endswith(".parquet")]

    if not keys:
        logger.error("No feature Parquet files found at %s", prefix)
        sys.exit(1)

    frames = []
    for key in keys:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        df = pd.read_parquet(io.BytesIO(resp["Body"].read()))
        frames.append(df)
        logger.info("Loaded %s (%d rows)", key, len(df))

    combined = pd.concat(frames, ignore_index=True)
    logger.info("Total rows loaded: %d", len(combined))
    return combined


def quality_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with too many missing features and remove constant columns."""
    n_features = len([c for c in df.columns if c.startswith("h")])
    if n_features == 0:
        return df

    feature_cols = [c for c in df.columns if c.startswith("h")]
    missing_per_row = df[feature_cols].isna().sum(axis=1) / len(feature_cols)
    mask = missing_per_row <= MAX_MISSING_FRAC
    dropped = (~mask).sum()
    if dropped > 0:
        logger.info("Dropped %d rows with >%.0f%% missing features", dropped, MAX_MISSING_FRAC * 100)
    df = df[mask].reset_index(drop=True)

    constant_cols = [c for c in feature_cols if df[c].nunique(dropna=False) <= 1]
    if constant_cols:
        logger.info("Dropping %d constant columns", len(constant_cols))
        df = df.drop(columns=constant_cols)

    return df


def stratified_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split by year to avoid temporal leakage.

    Assigns entire years to splits, approximately matching target fractions.
    """
    df["_year"] = pd.to_datetime(df["event_datetime"]).dt.year
    years = sorted(df["_year"].unique())
    n_years = len(years)

    n_test = max(1, round(n_years * TEST_FRAC))
    remaining = n_years - n_test
    n_val = max(1, round(remaining * VAL_FRAC / max(VAL_FRAC + TRAIN_FRAC, 1e-9)))
    n_train = remaining - n_val

    if n_train < 1:
        n_train, n_val, n_test = 1, max(0, n_years - 2), 1

    test_years = years[n_train + n_val:]
    val_years = years[n_train:n_train + n_val]
    train_years = years[:n_train]

    train = df[df["_year"].isin(train_years)].drop(columns=["_year"])
    val = df[df["_year"].isin(val_years)].drop(columns=["_year"])
    test = df[df["_year"].isin(test_years)].drop(columns=["_year"])

    logger.info(
        "Split: train=%d (%d yr), val=%d (%d yr), test=%d (%d yr)",
        len(train), len(train_years),
        len(val), len(val_years),
        len(test), len(test_years),
    )
    return train, val, test


def upload_parquet(df: pd.DataFrame, key: str) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue())
    logger.info("Uploaded %d rows to s3://%s/%s", len(df), S3_BUCKET, key)


def generate_manifest(train: pd.DataFrame, val: pd.DataFrame, test: pd.DataFrame) -> dict:
    """Generate a metadata manifest describing the dataset."""
    all_data = pd.concat([train, val, test], ignore_index=True)
    feature_cols = [c for c in all_data.columns if c.startswith("h") or c.endswith(("_sin", "_cos"))]
    meta_cols = ["event_datetime", "grid_lat", "grid_lon", "label", "event_type"]

    feature_stats = {}
    for col in feature_cols:
        series = all_data[col].dropna()
        feature_stats[col] = {
            "dtype": str(all_data[col].dtype),
            "missing_frac": float(all_data[col].isna().mean()),
            "mean": float(series.mean()) if len(series) > 0 else None,
            "std": float(series.std()) if len(series) > 0 else None,
            "min": float(series.min()) if len(series) > 0 else None,
            "max": float(series.max()) if len(series) > 0 else None,
        }

    label_counts = all_data["label"].value_counts().to_dict()

    return {
        "n_samples": {"train": len(train), "val": len(val), "test": len(test)},
        "n_features": len(feature_cols),
        "feature_columns": feature_cols,
        "meta_columns": [c for c in meta_cols if c in all_data.columns],
        "label_distribution": {str(k): int(v) for k, v in label_counts.items()},
        "feature_stats": feature_stats,
    }


def validate_dataset(manifest: dict) -> None:
    """Fail the pipeline if the dataset doesn't meet minimum quality thresholds."""
    issues = []

    total = sum(manifest["n_samples"].values())
    if total < 100:
        issues.append(f"Too few samples: {total} (minimum 100)")

    if manifest["n_features"] < 10:
        issues.append(f"Too few features: {manifest['n_features']} (minimum 10)")

    label_dist = manifest["label_distribution"]
    positives = label_dist.get("1", 0)
    negatives = label_dist.get("0", 0)
    if positives == 0:
        issues.append("No positive samples in dataset")
    if negatives == 0:
        issues.append("No negative samples in dataset")
    if positives > 0 and negatives > 0:
        ratio = negatives / positives
        if ratio < 1.0 or ratio > 10.0:
            issues.append(f"Class ratio out of range: {ratio:.1f} (expected 1-10)")

    for col, stats in manifest.get("feature_stats", {}).items():
        if stats.get("missing_frac", 0) > 0.8:
            issues.append(f"Feature '{col}' has {stats['missing_frac']:.0%} missing")

    if issues:
        for issue in issues:
            logger.error("VALIDATION FAILED: %s", issue)
        sys.exit(1)

    logger.info("Validation passed: %d samples, %d features, ratio=%.1f",
                total, manifest["n_features"], negatives / max(positives, 1))


def main() -> None:
    logger.info("Dataset builder starting")
    logger.info("  S3 bucket: %s", S3_BUCKET)
    logger.info("  Input prefix: %s", INPUT_PREFIX)
    logger.info("  Output prefix: %s", OUTPUT_PREFIX)

    df = load_all_features()
    df = quality_filter(df)

    if df.empty:
        logger.error("No data remaining after quality filtering")
        sys.exit(1)

    train, val, test = stratified_split(df)

    upload_parquet(train, f"{OUTPUT_PREFIX}train.parquet")
    upload_parquet(val, f"{OUTPUT_PREFIX}val.parquet")
    upload_parquet(test, f"{OUTPUT_PREFIX}test.parquet")

    manifest = generate_manifest(train, val, test)

    validate_dataset(manifest)

    manifest_body = json.dumps(manifest, indent=2)
    manifest_key = f"{OUTPUT_PREFIX}manifest.json"
    s3.put_object(Bucket=S3_BUCKET, Key=manifest_key, Body=manifest_body.encode())
    logger.info("Uploaded manifest to s3://%s/%s", S3_BUCKET, manifest_key)

    logger.info("Dataset builder complete")
    logger.info(
        "  Total: %d train + %d val + %d test = %d samples, %d features",
        len(train), len(val), len(test),
        len(train) + len(val) + len(test),
        manifest["n_features"],
    )


if __name__ == "__main__":
    main()
