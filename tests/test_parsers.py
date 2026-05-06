"""Tests for storm_tracking.parsers module."""

import gzip
import json

from storm_tracking.parsers import parse_lightning_flashes


def _make_flash_data(flashes: list[dict]) -> bytes:
    """Create gzipped JSON from a list of flash dicts."""
    return gzip.compress(json.dumps(flashes).encode("utf-8"))


class TestParseLightningFlashes:
    def test_filters_to_bbox(self):
        data = _make_flash_data([
            {"datetime": "2024-07-15T14:00:00", "latitude": 47.0, "longitude": 8.0},
            {"datetime": "2024-07-15T14:01:00", "latitude": 50.0, "longitude": 8.0},
            {"datetime": "2024-07-15T14:02:00", "latitude": 46.5, "longitude": 7.0},
        ])
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert len(result) == 2

    def test_flash_fields(self):
        data = _make_flash_data([
            {"datetime": "2024-07-15T14:00:00", "latitude": 47.0, "longitude": 8.0, "radiance": 1.5e-3},
        ])
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert result[0]["datetime"] == "2024-07-15T14:00:00"
        assert result[0]["latitude"] == 47.0
        assert result[0]["longitude"] == 8.0
        assert result[0]["radiance"] == 1.5e-3

    def test_flash_without_radiance(self):
        data = _make_flash_data([
            {"datetime": "2024-07-15T14:00:00", "latitude": 47.0, "longitude": 8.0},
        ])
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert "radiance" not in result[0]

    def test_empty_data(self):
        data = gzip.compress(b"[]")
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert result == []

    def test_invalid_gzip(self):
        result = parse_lightning_flashes(b"not gzip", 48.3, 45.3, 11.0, 5.5)
        assert result == []

    def test_invalid_json(self):
        data = gzip.compress(b"not json")
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert result == []

    def test_all_flashes_outside_bbox(self):
        data = _make_flash_data([
            {"datetime": "2024-07-15T14:00:00", "latitude": 60.0, "longitude": 20.0},
        ])
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert result == []

    def test_preserves_all_flashes(self):
        flashes = [
            {"datetime": f"2024-07-15T{i:02d}:00:00", "latitude": 47.0, "longitude": 8.0}
            for i in range(100)
        ]
        data = _make_flash_data(flashes)
        result = parse_lightning_flashes(data, 48.3, 45.3, 11.0, 5.5)
        assert len(result) == 100
