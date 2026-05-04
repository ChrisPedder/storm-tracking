"""Parsers for storm event data from ESWD and Blitzortung."""

from __future__ import annotations

import gzip
import json
from typing import Any


def parse_eswd_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise ESWD v2 API report objects into a consistent schema.

    Extracts the fields we need for labelling and drops the rest.
    Tolerates missing keys gracefully.
    """
    events: list[dict[str, Any]] = []
    for r in reports:
        event = {
            "id": r.get("id"),
            "datetime": r.get("datetime"),
            "latitude": r.get("lat"),
            "longitude": r.get("lon"),
            "event_type": r.get("type"),
            "country": r.get("country"),
            "city": r.get("city"),
            "qc_level": r.get("qc_level"),
            "source": "ESWD",
        }
        events.append(event)
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
