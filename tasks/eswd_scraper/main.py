"""Scrape the European Severe Weather Database for Swiss thunderstorm events."""

import json
import logging
import os
import sys
import time

import boto3
import requests
from bs4 import BeautifulSoup

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

ESWD_SEARCH_URL = "https://eswd.eu/cgi-bin/eswd.cgi"

EVENT_TYPES = [
    "1",  # Tornado
    "2",  # Damaging wind / severe wind gust
    "3",  # Large hail
    "4",  # Heavy rain / flash flood
    "5",  # Funnel cloud
    "7",  # Damaging lightning
]

s3 = boto3.client("s3")


def s3_key(year: int, month: int) -> str:
    return f"{S3_PREFIX}{year}/{month:02d}/events.json"


def already_exists(year: int, month: int) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=s3_key(year, month))
        return True
    except s3.exceptions.ClientError:
        return False


def scrape_month(year: int, month: int) -> list[dict]:
    """Query ESWD for all Swiss convective events in a given month."""
    last_day = 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30
    if month == 2:
        last_day = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28

    params = {
        "opt": "sch",
        "continent": "Europe",
        "country": "CH",
        "sdate": f"{year}-{month:02d}-01",
        "edate": f"{year}-{month:02d}-{last_day}",
        "lat_s": str(BBOX_SOUTH),
        "lat_n": str(BBOX_NORTH),
        "lon_w": str(BBOX_WEST),
        "lon_e": str(BBOX_EAST),
        "format": "html",
    }

    events = []
    for event_type in EVENT_TYPES:
        params["ession_type"] = event_type
        resp = requests.get(ESWD_SEARCH_URL, params=params, timeout=60)
        resp.raise_for_status()
        events.extend(_parse_response(resp.text, year, month))
        time.sleep(2)

    logger.info("Found %d events for %d-%02d", len(events), year, month)
    return events


def _parse_response(html: str, year: int, month: int) -> list[dict]:
    """Parse ESWD HTML response into structured event records.

    The ESWD results page contains a table of events with columns for date/time,
    location, coordinates, event type, and intensity. This parser extracts those
    fields into dictionaries.
    """
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("table.results tr")
    events = []

    for row in rows[1:]:  # skip header
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        events.append({
            "date": cells[0].get_text(strip=True),
            "time_utc": cells[1].get_text(strip=True),
            "location": cells[2].get_text(strip=True),
            "latitude": cells[3].get_text(strip=True),
            "longitude": cells[4].get_text(strip=True),
            "event_type": cells[5].get_text(strip=True),
            "intensity": cells[6].get_text(strip=True) if len(cells) > 6 else "",
            "source": "ESWD",
            "query_year": year,
            "query_month": month,
        })

    return events


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
            events = scrape_month(year, month)
            upload_events(events, year, month)
            total += len(events)

    logger.info("ESWD scraper complete — %d total events", total)


if __name__ == "__main__":
    main()
