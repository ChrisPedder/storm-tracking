"""Download MTG Lightning Imager flash data from the EUMETSAT Data Store."""

import gzip
import io
import json
import logging
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

import boto3
import eumdac
import netCDF4
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("eumetsat-lightning")

S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/lightning/")
START_YEAR = int(os.environ.get("START_YEAR", "2024"))
END_YEAR = int(os.environ.get("END_YEAR", "2024"))
BBOX_NORTH = float(os.environ.get("BBOX_NORTH", "48.3"))
BBOX_SOUTH = float(os.environ.get("BBOX_SOUTH", "45.3"))
BBOX_WEST = float(os.environ.get("BBOX_WEST", "5.5"))
BBOX_EAST = float(os.environ.get("BBOX_EAST", "11.0"))

EUMETSAT_KEY = os.environ["EUMETSAT_KEY"]
EUMETSAT_SECRET = os.environ["EUMETSAT_SECRET"]

LI_FLASH_COLLECTION = "EO:EUM:DAT:0691"

s3 = boto3.client("s3")


def s3_key(year: int, month: int, day: int) -> str:
    return f"{S3_PREFIX}{year}/{month:02d}/{day:02d}/flashes.json.gz"


def already_exists(year: int, month: int, day: int) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=s3_key(year, month, day))
        return True
    except s3.exceptions.ClientError:
        return False


def extract_flashes_from_nc(nc_bytes: bytes) -> list[dict]:
    """Extract flash records from a LI L2 NetCDF file, filtering to bbox."""
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
        tmp.write(nc_bytes)
        tmp_path = tmp.name

    flashes = []
    try:
        ds = netCDF4.Dataset(tmp_path, "r")
        try:
            if "flash_time" not in ds.variables:
                return []

            times = netCDF4.num2date(
                ds.variables["flash_time"][:],
                units=ds.variables["flash_time"].units,
                only_use_cftime_datetimes=False,
                only_use_python_datetimes=True,
            )
            lats = ds.variables["flash_lat"][:]
            lons = ds.variables["flash_lon"][:]
            radiances = (
                ds.variables["flash_radiance"][:]
                if "flash_radiance" in ds.variables
                else [None] * len(lats)
            )

            for i in range(len(lats)):
                lat = float(lats[i])
                lon = float(lons[i])
                if BBOX_SOUTH <= lat <= BBOX_NORTH and BBOX_WEST <= lon <= BBOX_EAST:
                    flash = {
                        "datetime": times[i].isoformat(),
                        "latitude": lat,
                        "longitude": lon,
                    }
                    if radiances[i] is not None:
                        rad = float(radiances[i])
                        if np.isfinite(rad):
                            flash["radiance"] = rad
                    flashes.append(flash)
        finally:
            ds.close()
    finally:
        os.unlink(tmp_path)

    return flashes


def fetch_day(token: eumdac.AccessToken, year: int, month: int, day: int) -> list[dict]:
    """Download all LI flash products for a single day and extract Swiss flashes."""
    datastore = eumdac.DataStore(token)
    collection = datastore.get_collection(LI_FLASH_COLLECTION)

    dt_start = datetime(year, month, day, 0, 0, 0)
    dt_end = datetime(year, month, day, 23, 59, 59)

    try:
        products = collection.search(dtstart=dt_start, dtend=dt_end)
    except Exception as exc:
        logger.warning("Search failed for %d-%02d-%02d: %s", year, month, day, exc)
        return []

    all_flashes = []
    product_count = 0
    for product in products:
        try:
            with product.open() as fsrc:
                zip_bytes = fsrc.read()
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if name.endswith(".nc"):
                        nc_bytes = zf.read(name)
                        all_flashes.extend(extract_flashes_from_nc(nc_bytes))
            product_count += 1
        except Exception as exc:
            logger.warning("Failed to process product %s: %s", product, exc)
            continue

    logger.info(
        "Fetched %d flashes from %d products for %d-%02d-%02d",
        len(all_flashes), product_count, year, month, day,
    )
    return all_flashes


def upload_flashes(flashes: list[dict], year: int, month: int, day: int) -> None:
    key = s3_key(year, month, day)
    body = gzip.compress(json.dumps(flashes, default=str).encode("utf-8"))
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/gzip")
    logger.info("Uploaded %d flashes to s3://%s/%s", len(flashes), S3_BUCKET, key)


def days_in_range():
    dt = datetime(START_YEAR, 7, 1) if START_YEAR == 2024 else datetime(START_YEAR, 1, 1)
    end = datetime(END_YEAR, 12, 31)
    while dt <= end:
        yield dt.year, dt.month, dt.day
        dt += timedelta(days=1)


def main() -> None:
    logger.info(
        "EUMETSAT Lightning downloader starting: years=%d-%d bbox=[%.1f,%.1f,%.1f,%.1f]",
        START_YEAR, END_YEAR, BBOX_SOUTH, BBOX_WEST, BBOX_NORTH, BBOX_EAST,
    )

    token = eumdac.AccessToken((EUMETSAT_KEY, EUMETSAT_SECRET))
    logger.info("Authenticated with EUMETSAT Data Store")

    total = 0
    for year, month, day in days_in_range():
        if already_exists(year, month, day):
            logger.info("Skipping %d-%02d-%02d (already in S3)", year, month, day)
            continue
        flashes = fetch_day(token, year, month, day)
        upload_flashes(flashes, year, month, day)
        total += len(flashes)

    logger.info("EUMETSAT Lightning downloader complete — %d total flashes", total)


if __name__ == "__main__":
    main()
