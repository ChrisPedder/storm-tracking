"""Download Copernicus DEM GLO-30 tiles covering Switzerland from AWS open data."""

import logging
import math
import os
import sys

import boto3
from botocore import UNSIGNED
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("topo-downloader")

S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/topography/")
BBOX_NORTH = float(os.environ.get("BBOX_NORTH", "48.3"))
BBOX_SOUTH = float(os.environ.get("BBOX_SOUTH", "45.3"))
BBOX_WEST = float(os.environ.get("BBOX_WEST", "5.5"))
BBOX_EAST = float(os.environ.get("BBOX_EAST", "11.0"))

SOURCE_BUCKET = "copernicus-dem-30m"

public_s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
dest_s3 = boto3.client("s3")


def tile_name(lat: int, lon: int) -> str:
    """Build the Copernicus DEM tile directory name for a 1°×1° cell."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tiles_for_bbox() -> list[str]:
    """Return all 1°×1° tile names covering the bounding box."""
    lat_min = math.floor(BBOX_SOUTH)
    lat_max = math.floor(BBOX_NORTH)
    lon_min = math.floor(BBOX_WEST)
    lon_max = math.floor(BBOX_EAST)

    tiles = []
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            tiles.append(tile_name(lat, lon))
    return tiles


def already_exists(name: str) -> bool:
    key = f"{S3_PREFIX}{name}/{name}.tif"
    try:
        dest_s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except dest_s3.exceptions.ClientError:
        return False


def download_tile(name: str) -> None:
    """Copy a single DEM tile from the public bucket to our bucket."""
    source_key = f"{name}/{name}.tif"
    dest_key = f"{S3_PREFIX}{name}/{name}.tif"

    try:
        public_s3.head_object(Bucket=SOURCE_BUCKET, Key=source_key)
    except public_s3.exceptions.ClientError:
        logger.warning("Tile not found in source: %s (might be ocean)", name)
        return

    logger.info("Downloading %s from public bucket", name)
    obj = public_s3.get_object(Bucket=SOURCE_BUCKET, Key=source_key)
    dest_s3.upload_fileobj(obj["Body"], S3_BUCKET, dest_key)
    logger.info("Uploaded to s3://%s/%s", S3_BUCKET, dest_key)


def main() -> None:
    tiles = tiles_for_bbox()
    logger.info(
        "Topography downloader starting: %d tiles for bbox [%.1f,%.1f,%.1f,%.1f]",
        len(tiles), BBOX_SOUTH, BBOX_WEST, BBOX_NORTH, BBOX_EAST,
    )

    downloaded = 0
    for name in tiles:
        if already_exists(name):
            logger.info("Skipping %s (already exists)", name)
            continue
        download_tile(name)
        downloaded += 1

    logger.info("Topography downloader complete — %d tiles transferred", downloaded)


if __name__ == "__main__":
    main()
