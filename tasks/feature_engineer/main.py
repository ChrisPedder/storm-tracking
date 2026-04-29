"""Compute derived atmospheric features from raw ERA5 and storm event data.

Reads raw data from S3, computes convective indices, wind shear, spatial
gradients, and temporal tendencies, then writes processed features back to S3
as Parquet files.
"""

import logging
import os
import sys

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("feature-engineer")

S3_BUCKET = os.environ["S3_BUCKET"]
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "processed/")

s3 = boto3.client("s3")


def main() -> None:
    logger.info("Feature engineer starting")
    logger.info("  S3 bucket: %s", S3_BUCKET)
    logger.info("  Raw prefix: %s", RAW_PREFIX)
    logger.info("  Output prefix: %s", OUTPUT_PREFIX)

    # Pipeline stages to implement:
    # 1. Load storm events from raw/eswd/ and raw/blitzortung/
    # 2. Merge and deduplicate events, snap to ERA5 grid
    # 3. For each event at (lat, lon, t_storm):
    #    a. Extract ERA5 data for t_storm-3h, t_storm-2h, t_storm-1h
    #    b. Extract 3x3 grid patch (centre + 8 neighbours)
    #    c. Compute derived indices (lifted index, K-index, etc.)
    #    d. Compute wind shear (0-1km, 0-3km, 0-6km)
    #    e. Compute temporal tendencies (CAPE change, temp change)
    #    f. Compute spatial gradients (moisture, temperature)
    # 4. Generate matched negative samples
    # 5. Encode time features (sin/cos hour, day-of-year)
    # 6. Write processed features to S3 as Parquet

    raise NotImplementedError(
        "Feature engineering pipeline not yet implemented — see pipeline stages above"
    )


if __name__ == "__main__":
    main()
