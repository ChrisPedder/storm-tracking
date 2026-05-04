"""Fetch severe weather events from the ESWD v2 REST API."""

import json
import logging
import os
import sys
import time
from calendar import monthrange

import boto3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("eswd-scraper")

S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/eswd/")
START_YEAR = int(os.environ.get("START_YEAR", "2010"))
END_YEAR = int(os.environ.get("END_YEAR", "2023"))
BBOX_NORTH = float(os.environ.get("BBOX_NORTH", "48.3"))
BBOX_SOUTH = float(os.environ.get("BBOX_SOUTH", "45.3"))
BBOX_WEST = float(os.environ.get("BBOX_WEST", "5.5"))
BBOX_EAST = float(os.environ.get("BBOX_EAST", "11.0"))

ESWD_API_TOKEN = os.environ["ESWD_API_TOKEN"]
ESWD_API_URL = "https://eswd.eu/api/v2/reportList"

EVENT_TYPES = "TORNADO,WIND,HAIL,PRECIP,FUNNEL,LIGHTNING"

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {ESWD_API_TOKEN}",
    "Accept": "application/json",
})

s3 = boto3.client("s3")


def s3_key(year: int, month: int) -> str:
    return f"{S3_PREFIX}{year}/{month:02d}/events.json"


def already_exists(year: int, month: int) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=s3_key(year, month))
        return True
    except s3.exceptions.ClientError:
        return False


def fetch_month(year: int, month: int) -> list[dict]:
    """Fetch all Swiss convective events for a given month from the ESWD v2 API."""
    last_day = monthrange(year, month)[1]
    params = {
        "sd": f"{year}-{month:02d}-01T00:00:00Z",
        "ed": f"{year}-{month:02d}-{last_day}T23:59:59Z",
        "y0": str(BBOX_SOUTH),
        "y1": str(BBOX_NORTH),
        "x0": str(BBOX_WEST),
        "x1": str(BBOX_EAST),
        "countries": "CH",
        "types": EVENT_TYPES,
    }

    resp = SESSION.get(ESWD_API_URL, params=params, timeout=60)
    resp.raise_for_status()
    reports = resp.json()

    if not isinstance(reports, list):
        logger.warning("Unexpected response for %d-%02d: %s", year, month, type(reports))
        return []

    logger.info("Found %d events for %d-%02d", len(reports), year, month)
    return reports


def upload_events(events: list[dict], year: int, month: int) -> None:
    key = s3_key(year, month)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(events, default=str, ensure_ascii=False),
        ContentType="application/json",
    )
    logger.info("Uploaded %d events to s3://%s/%s", len(events), S3_BUCKET, key)


def main() -> None:
    logger.info(
        "ESWD scraper starting: years=%d-%d bbox=[%.1f,%.1f,%.1f,%.1f]",
        START_YEAR, END_YEAR, BBOX_SOUTH, BBOX_WEST, BBOX_NORTH, BBOX_EAST,
    )

    total = 0
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            if already_exists(year, month):
                logger.info("Skipping %d-%02d (already in S3)", year, month)
                continue
            events = fetch_month(year, month)
            upload_events(events, year, month)
            total += len(events)
            time.sleep(1)

    logger.info("ESWD scraper complete - %d total events", total)


if __name__ == "__main__":
    main()
