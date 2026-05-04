"""Tests for storm_tracking.parsers module."""

import gzip
import json

from storm_tracking.parsers import parse_blitzortung_strokes, parse_eswd_reports

SAMPLE_REPORTS = [
    {
        "id": "2023-CH-0042",
        "datetime": "2023-07-15T14:30:00Z",
        "lat": 46.95,
        "lon": 7.45,
        "type": "HAIL",
        "country": "CH",
        "city": "Bern",
        "qc_level": "QC1",
    },
    {
        "id": "2023-CH-0043",
        "datetime": "2023-07-15T15:00:00Z",
        "lat": 46.76,
        "lon": 7.63,
        "type": "WIND",
        "country": "CH",
        "city": "Thun",
        "qc_level": "QC0+",
    },
]


class TestParseEswdReports:
    def test_parses_two_reports(self):
        events = parse_eswd_reports(SAMPLE_REPORTS)
        assert len(events) == 2

    def test_first_event_fields(self):
        events = parse_eswd_reports(SAMPLE_REPORTS)
        assert events[0]["id"] == "2023-CH-0042"
        assert events[0]["datetime"] == "2023-07-15T14:30:00Z"
        assert events[0]["latitude"] == 46.95
        assert events[0]["longitude"] == 7.45
        assert events[0]["event_type"] == "HAIL"
        assert events[0]["country"] == "CH"
        assert events[0]["city"] == "Bern"
        assert events[0]["qc_level"] == "QC1"
        assert events[0]["source"] == "ESWD"

    def test_empty_list(self):
        assert parse_eswd_reports([]) == []

    def test_missing_fields_default_to_none(self):
        events = parse_eswd_reports([{"type": "TORNADO"}])
        assert len(events) == 1
        assert events[0]["id"] is None
        assert events[0]["latitude"] is None
        assert events[0]["event_type"] == "TORNADO"
        assert events[0]["source"] == "ESWD"

    def test_preserves_all_reports(self):
        reports = [{"id": str(i)} for i in range(100)]
        assert len(parse_eswd_reports(reports)) == 100


def _make_blitzortung_data(strokes: list[dict]) -> bytes:
    """Create gzipped Blitzortung JSON lines from a list of stroke dicts."""
    lines = "\n".join(json.dumps(s) for s in strokes)
    return gzip.compress(lines.encode("utf-8"))


class TestParseBlitzortungStrokes:
    def test_filters_to_bbox(self):
        data = _make_blitzortung_data([
            {"time": 1000, "lat": 47.0, "lon": 8.0, "sig": 15, "num_sta": 5},
            {"time": 2000, "lat": 50.0, "lon": 8.0, "sig": 20, "num_sta": 3},
            {"time": 3000, "lat": 46.5, "lon": 7.0, "sig": 10, "num_sta": 4},
        ])
        result = parse_blitzortung_strokes(data, 48.3, 45.3, 11.0, 5.5)
        assert len(result) == 2

    def test_stroke_fields(self):
        data = _make_blitzortung_data([
            {"time": 1000, "lat": 47.0, "lon": 8.0, "sig": 15.5, "num_sta": 5},
        ])
        result = parse_blitzortung_strokes(data, 48.3, 45.3, 11.0, 5.5)
        assert result[0]["timestamp_ns"] == 1000
        assert result[0]["latitude"] == 47.0
        assert result[0]["longitude"] == 8.0
        assert result[0]["peak_current_ka"] == 15.5
        assert result[0]["num_stations"] == 5

    def test_empty_data(self):
        data = gzip.compress(b"")
        result = parse_blitzortung_strokes(data, 48.3, 45.3, 11.0, 5.5)
        assert result == []

    def test_invalid_gzip(self):
        result = parse_blitzortung_strokes(b"not gzip", 48.3, 45.3, 11.0, 5.5)
        assert result == []

    def test_malformed_json_lines_skipped(self):
        raw = b'{"time":1,"lat":47,"lon":8,"sig":10,"num_sta":3}\nnot json\n'
        data = gzip.compress(raw)
        result = parse_blitzortung_strokes(data, 48.3, 45.3, 11.0, 5.5)
        assert len(result) == 1

    def test_all_strokes_outside_bbox(self):
        data = _make_blitzortung_data([
            {"time": 1000, "lat": 60.0, "lon": 20.0, "sig": 10, "num_sta": 2},
        ])
        result = parse_blitzortung_strokes(data, 48.3, 45.3, 11.0, 5.5)
        assert result == []
