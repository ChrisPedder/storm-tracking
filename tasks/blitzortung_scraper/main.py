"""Scrape Blitzortung.org lightning archive for Swiss stroke data."""

import gzip
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import boto3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("blitzortung-scraper")

S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/blitzortung/")
START_YEAR = int(os.environ.get("START_YEAR", "2010"))
END_YEAR = int(os.environ.get("END_YEAR", "2023"))
BBOX_NORTH = float(os.environ.get("BBOX_NORTH", "48.3"))
BBOX_SOUTH = float(os.environ.get("BBOX_SOUTH", "45.3"))
BBOX_WEST = float(os.environ.get("BBOX_WEST", "5.5"))
BBOX_EAST = float(os.environ.get("BBOX_EAST", "11.0"))

ARCHIVE_URL = "https://data.blitzortung.org/Data/Protected/Strokes"
SESSION = requests.Session()

s3 = boto3.client("s3")


def s3_key(year: int, month: int, day: int) -> str:
    return f"{S3_PREFIX}{year}/{month:02d}/{day:02d}/strokes.json.gz"


def already_exists(year: int, month: int, day: int) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=s3_key(year, month, day))
        return True
    except s3.exceptions.ClientError:
        return False


def fetch_strokes(year: int, month: int, day: int, hour: int) -> list[dict]:
    """Fetch lightning strokes for one hour from the Blitzortung archive.

    Blitzortung stores data as gzipped CSVs at predictable URLs. Each row
    contains timestamp, latitude, longitude, peak current, and station count.
    We filter to strokes within our bounding box.
    """
    url = f"{ARCHIVE_URL}/{year}/{month:02d}/{day:02d}/{hour:02d}.json.gz"

    try:
        resp = SESSION.get(url, timeout=30)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return []

    strokes = []
    try:
        raw = gzip.decompress(resp.content)
        for line in raw.decode("utf-8", errors="replace").strip().split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)
            lat = record.get("lat", 0)
            lon = record.get("lon", 0)
            if BBOX_SOUTH <= lat <= BBOX_NORTH and BBOX_WEST <= lon <= BBOX_EAST:
                strokes.append({
                    "timestamp_ns": record.get("time"),
                    "latitude": lat,
                    "longitude": lon,
                    "peak_current_ka": record.get("sig"),
                    "num_stations": record.get("num_sta"),
                })
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", url, exc)

    return strokes


def scrape_day(year: int, month: int, day: int) -> list[dict]:
    """Scrape all 24 hours of stroke data for a single day."""
    all_strokes = []
    for hour in range(24):
        all_strokes.extend(fetch_strokes(year, month, day, hour))
        time.sleep(0.5)
    return all_strokes


def upload_strokes(strokes: list[dict], year: int, month: int, day: int) -> None:
    key = s3_key(year, month, day)
    body = gzip.compress(json.dumps(strokes, default=str).encode("utf-8"))
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/gzip",
    )
    logger.info(
        "Uploaded %d strokes to s3://%s/%s", len(strokes), S3_BUCKET, key,
    )


def days_in_range():
    """Yield (year, month, day) tuples for the configured date range."""
    dt = datetime(START_YEAR, 1, 1)
    end = datetime(END_YEAR, 12, 31)
    while dt <= end:
        yield dt.year, dt.month, dt.day
        dt += timedelta(days=1)


def main() -> None:
    logger.info(
        "Blitzortung scraper starting: years=%d-%d bbox=[%.1f,%.1f,%.1f,%.1f]",
        START_YEAR, END_YEAR, BBOX_SOUTH, BBOX_WEST, BBOX_NORTH, BBOX_EAST,
    )

    total = 0
    for year, month, day in days_in_range():
        if already_exists(year, month, day):
            continue
        strokes = scrape_day(year, month, day)
        upload_strokes(strokes, year, month, day)
        total += len(strokes)

    logger.info("Blitzortung scraper complete — %d total strokes", total)


if __name__ == "__main__":
    main()
