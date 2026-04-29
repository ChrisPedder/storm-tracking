"""Parsers for storm event data from ESWD and Blitzortung."""

from __future__ import annotations

import gzip
import json
from typing import Any

from bs4 import BeautifulSoup


def parse_eswd_html(html: str) -> list[dict[str, str]]:
    """Parse an ESWD search results HTML page into structured event records.

    Each event is a dictionary with keys: date, time_utc, location,
    latitude, longitude, event_type, and intensity.

    Returns an empty list if no results table is found.
    """
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("table.results tr")
    events: list[dict[str, str]] = []

    for row in rows[1:]:
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
        })

    return events


def parse_blitzortung_strokes(
    data: bytes,
    bbox_north: float,
    bbox_south: float,
    bbox_east: float,
    bbox_west: float,
) -> list[dict[str, Any]]:
    """Parse gzipped Blitzortung JSON lines and filter to a bounding box.

    Each line in the decompressed data is a JSON object with keys: time, lat,
    lon, sig (peak current), num_sta (station count).

    Returns a list of stroke dictionaries within the bounding box.
    """
    try:
        raw = gzip.decompress(data)
    except (gzip.BadGzipFile, OSError):
        return []

    strokes: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").strip().split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        lat = record.get("lat", 0)
        lon = record.get("lon", 0)
        if bbox_south <= lat <= bbox_north and bbox_west <= lon <= bbox_east:
            strokes.append({
                "timestamp_ns": record.get("time"),
                "latitude": lat,
                "longitude": lon,
                "peak_current_ka": record.get("sig"),
                "num_stations": record.get("num_sta"),
            })

    return strokes
