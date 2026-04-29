"""Tests for storm_tracking.parsers module."""

import gzip
import json

from storm_tracking.parsers import parse_blitzortung_strokes, parse_eswd_html

SAMPLE_ESWD_HTML = """
<html>
<body>
<table class="results">
<tr><th>Date</th><th>Time</th><th>Location</th><th>Lat</th><th>Lon</th><th>Type</th><th>Intensity</th></tr>
<tr>
  <td>2023-07-15</td>
  <td>14:30</td>
  <td>Bern</td>
  <td>46.95</td>
  <td>7.45</td>
  <td>Large hail</td>
  <td>4 cm</td>
</tr>
<tr>
  <td>2023-07-15</td>
  <td>15:00</td>
  <td>Thun</td>
  <td>46.76</td>
  <td>7.63</td>
  <td>Severe wind</td>
  <td>90 km/h</td>
</tr>
</table>
</body>
</html>
"""

SAMPLE_ESWD_HTML_NO_INTENSITY = """
<html>
<body>
<table class="results">
<tr><th>Date</th><th>Time</th><th>Location</th><th>Lat</th><th>Lon</th><th>Type</th></tr>
<tr>
  <td>2023-07-15</td>
  <td>16:00</td>
  <td>Zurich</td>
  <td>47.37</td>
  <td>8.54</td>
  <td>Damaging lightning</td>
</tr>
</table>
</body>
</html>
"""


class TestParseEswdHtml:
    def test_parses_two_events(self):
        events = parse_eswd_html(SAMPLE_ESWD_HTML)
        assert len(events) == 2

    def test_first_event_fields(self):
        events = parse_eswd_html(SAMPLE_ESWD_HTML)
        assert events[0]["date"] == "2023-07-15"
        assert events[0]["time_utc"] == "14:30"
        assert events[0]["location"] == "Bern"
        assert events[0]["latitude"] == "46.95"
        assert events[0]["longitude"] == "7.45"
        assert events[0]["event_type"] == "Large hail"
        assert events[0]["intensity"] == "4 cm"

    def test_missing_intensity_column(self):
        events = parse_eswd_html(SAMPLE_ESWD_HTML_NO_INTENSITY)
        assert len(events) == 1
        assert events[0]["intensity"] == ""

    def test_empty_html(self):
        events = parse_eswd_html("<html><body></body></html>")
        assert events == []

    def test_no_results_table(self):
        events = parse_eswd_html("<html><body><table></table></body></html>")
        assert events == []

    def test_header_only_table(self):
        html = """
        <table class="results">
        <tr><th>Date</th><th>Time</th><th>Location</th><th>Lat</th><th>Lon</th><th>Type</th></tr>
        </table>
        """
        events = parse_eswd_html(html)
        assert events == []


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
