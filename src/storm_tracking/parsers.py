"""Parsers for storm event data from EUMETSAT Lightning Imager."""

from __future__ import annotations

import gzip
import json
from typing import Any


def parse_lightning_flashes(
    data: bytes,
    bbox_north: float,
    bbox_south: float,
    bbox_east: float,
    bbox_west: float,
) -> list[dict[str, Any]]:
    """Parse gzipped JSON lightning flash data and filter to a bounding box.

    Each record in the decompressed JSON array has keys: datetime, latitude,
    longitude, and optionally radiance.

    Returns a list of flash dictionaries within the bounding box.
    """
    try:
        raw = gzip.decompress(data)
    except (gzip.BadGzipFile, OSError):
        return []

    try:
        records = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(records, list):
        return []

    flashes: list[dict[str, Any]] = []
    for record in records:
        lat = record.get("latitude", 0)
        lon = record.get("longitude", 0)
        if bbox_south <= lat <= bbox_north and bbox_west <= lon <= bbox_east:
            flash: dict[str, Any] = {
                "datetime": record.get("datetime"),
                "latitude": lat,
                "longitude": lon,
            }
            if "radiance" in record:
                flash["radiance"] = record["radiance"]
            flashes.append(flash)

    return flashes
