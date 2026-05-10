"""Tests for feature_engineer pure functions.

The feature_engineer task reads S3_BUCKET from os.environ at import time,
so we patch that before importing the module.
"""

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest


# Stub out boto3, cfgrib, and native libs before importing the module
sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("cfgrib", MagicMock())
sys.modules.setdefault("eccodes", MagicMock())
sys.modules.setdefault("gribapi", MagicMock())
sys.modules.setdefault("xarray", MagicMock())
os.environ.setdefault("S3_BUCKET", "test-bucket")

_fe_path = Path(__file__).resolve().parent.parent / "tasks" / "feature_engineer" / "main.py"
_spec = importlib.util.spec_from_file_location("feature_engineer_main", _fe_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

snap_to_grid = _mod.snap_to_grid
grid_patch = _mod.grid_patch
compute_wind_shear = _mod.compute_wind_shear
compute_temporal_tendency = _mod.compute_temporal_tendency
encode_time_features = _mod.encode_time_features
generate_negatives = _mod.generate_negatives
label_severe_storms = _mod.label_severe_storms


class TestSnapToGrid:
    def test_exact_grid_point(self):
        assert snap_to_grid(47.0, 8.0) == (47.0, 8.0)

    def test_rounds_to_nearest(self):
        assert snap_to_grid(46.83, 7.42) == (46.75, 7.5)

    def test_rounds_down(self):
        assert snap_to_grid(46.1, 7.1) == (46.0, 7.0)

    def test_rounds_up(self):
        assert snap_to_grid(46.88, 7.88) == (47.0, 8.0)

    def test_negative_coordinates(self):
        lat, lon = snap_to_grid(-0.1, -0.1)
        assert lat == 0.0
        assert lon == 0.0


class TestGridPatch:
    def test_returns_nine_points(self):
        assert len(grid_patch(47.0, 8.0)) == 9

    def test_centre_is_included(self):
        assert (47.0, 8.0) in grid_patch(47.0, 8.0)

    def test_correct_offsets(self):
        points = grid_patch(47.0, 8.0)
        lats = sorted(set(lat for lat, _ in points))
        lons = sorted(set(lon for _, lon in points))
        assert lats == [46.75, 47.0, 47.25]
        assert lons == [7.75, 8.0, 8.25]

    def test_all_unique(self):
        points = grid_patch(47.0, 8.0)
        assert len(set(points)) == 9


class TestComputeWindShear:
    def test_zero_shear(self):
        feats = {
            "925": {"u_centre": 10.0, "v_centre": 5.0},
            "700": {"u_centre": 10.0, "v_centre": 5.0},
            "500": {"u_centre": 10.0, "v_centre": 5.0},
        }
        result = compute_wind_shear(feats)
        assert result["wind_shear_925_700"] == pytest.approx(0.0)
        assert result["wind_shear_925_500"] == pytest.approx(0.0)

    def test_known_shear(self):
        feats = {
            "925": {"u_centre": 0.0, "v_centre": 0.0},
            "700": {"u_centre": 3.0, "v_centre": 4.0},
        }
        result = compute_wind_shear(feats)
        assert result["wind_shear_925_700"] == pytest.approx(5.0)

    def test_missing_level_skipped(self):
        feats = {
            "925": {"u_centre": 0.0, "v_centre": 0.0},
        }
        result = compute_wind_shear(feats)
        assert result == {}

    def test_both_pairs_computed(self):
        feats = {
            "925": {"u_centre": 0.0, "v_centre": 0.0},
            "700": {"u_centre": 1.0, "v_centre": 0.0},
            "500": {"u_centre": 2.0, "v_centre": 0.0},
        }
        result = compute_wind_shear(feats)
        assert "wind_shear_925_700" in result
        assert "wind_shear_925_500" in result
        assert result["wind_shear_925_700"] == pytest.approx(1.0)
        assert result["wind_shear_925_500"] == pytest.approx(2.0)


class TestComputeTemporalTendency:
    def test_computes_difference(self):
        curr = {"cape_centre": 1500.0, "cin_centre": -50.0, "t2m_centre": 300.0, "sp_centre": 101325.0}
        prev = {"cape_centre": 1000.0, "cin_centre": -30.0, "t2m_centre": 298.0, "sp_centre": 101300.0}
        result = compute_temporal_tendency(curr, prev)
        assert result["cape_centre_tendency"] == pytest.approx(500.0)
        assert result["cin_centre_tendency"] == pytest.approx(-20.0)
        assert result["t2m_centre_tendency"] == pytest.approx(2.0)
        assert result["sp_centre_tendency"] == pytest.approx(25.0)

    def test_missing_variable_skipped(self):
        curr = {"cape_centre": 1500.0}
        prev = {"cin_centre": -30.0}
        result = compute_temporal_tendency(curr, prev)
        assert result == {}

    def test_nan_in_current_skipped(self):
        curr = {"cape_centre": float("nan")}
        prev = {"cape_centre": 1000.0}
        result = compute_temporal_tendency(curr, prev)
        assert "cape_centre_tendency" not in result

    def test_nan_in_previous_skipped(self):
        curr = {"cape_centre": 1500.0}
        prev = {"cape_centre": float("nan")}
        result = compute_temporal_tendency(curr, prev)
        assert "cape_centre_tendency" not in result

    def test_zero_change(self):
        feats = {"cape_centre": 1000.0, "cin_centre": -50.0, "t2m_centre": 300.0, "sp_centre": 101325.0}
        result = compute_temporal_tendency(feats, feats)
        for key in result:
            assert result[key] == pytest.approx(0.0)


class TestEncodeTimeFeatures:
    def test_midnight_values(self):
        result = encode_time_features(datetime(2023, 1, 1, 0, 0))
        assert result["hour_sin"] == pytest.approx(0.0, abs=1e-10)
        assert result["hour_cos"] == pytest.approx(1.0)

    def test_noon_values(self):
        result = encode_time_features(datetime(2023, 1, 1, 12, 0))
        assert result["hour_sin"] == pytest.approx(0.0, abs=1e-10)
        assert result["hour_cos"] == pytest.approx(-1.0)

    def test_six_am(self):
        result = encode_time_features(datetime(2023, 1, 1, 6, 0))
        assert result["hour_sin"] == pytest.approx(1.0)
        assert result["hour_cos"] == pytest.approx(0.0, abs=1e-10)

    def test_all_four_keys(self):
        result = encode_time_features(datetime(2023, 7, 15, 14, 30))
        assert set(result.keys()) == {"hour_sin", "hour_cos", "doy_sin", "doy_cos"}

    def test_values_bounded(self):
        result = encode_time_features(datetime(2023, 7, 15, 14, 30))
        for val in result.values():
            assert -1.0 <= val <= 1.0

    def test_day_of_year_varies(self):
        spring = encode_time_features(datetime(2023, 4, 1, 12, 0))
        autumn = encode_time_features(datetime(2023, 10, 1, 12, 0))
        assert spring["doy_sin"] != pytest.approx(autumn["doy_sin"], abs=0.1)

    def test_half_hour_shifts_sin(self):
        on_hour = encode_time_features(datetime(2023, 1, 1, 3, 0))
        half_hour = encode_time_features(datetime(2023, 1, 1, 3, 30))
        assert on_hour["hour_sin"] != pytest.approx(half_hour["hour_sin"], abs=0.01)


class TestGenerateNegatives:
    @pytest.fixture()
    def events_df(self):
        return pd.DataFrame({
            "datetime": pd.to_datetime(["2023-07-15T14:00:00Z", "2023-07-20T10:00:00Z"], utc=True),
            "grid_lat": [47.0, 46.75],
            "grid_lon": [8.0, 7.5],
            "label": [1, 1],
        })

    def test_produces_negatives(self, events_df):
        negs = generate_negatives(events_df, n_per_positive=3)
        assert len(negs) > 0

    def test_all_labelled_zero(self, events_df):
        negs = generate_negatives(events_df, n_per_positive=3)
        assert (negs["label"] == 0).all()

    def test_has_required_columns(self, events_df):
        negs = generate_negatives(events_df, n_per_positive=3)
        assert set(negs.columns) >= {"datetime", "grid_lat", "grid_lon", "label"}

    def test_uses_event_grid_locations(self, events_df):
        negs = generate_negatives(events_df, n_per_positive=5)
        if not negs.empty:
            assert set(negs["grid_lat"].unique()).issubset({47.0, 46.75})
            assert set(negs["grid_lon"].unique()).issubset({8.0, 7.5})

    def test_deterministic_with_seed(self, events_df):
        negs1 = generate_negatives(events_df, n_per_positive=3)
        negs2 = generate_negatives(events_df, n_per_positive=3)
        pd.testing.assert_frame_equal(negs1, negs2)

    def test_no_negatives_within_six_hours_same_location(self, events_df):
        negs = generate_negatives(events_df, n_per_positive=10)
        for _, neg_row in negs.iterrows():
            for _, pos_row in events_df.iterrows():
                if neg_row["grid_lat"] == pos_row["grid_lat"] and neg_row["grid_lon"] == pos_row["grid_lon"]:
                    diff_seconds = abs((neg_row["datetime"] - pos_row["datetime"]).total_seconds())
                    assert diff_seconds >= 6 * 3600

    def test_empty_events(self):
        empty = pd.DataFrame({"datetime": [], "grid_lat": [], "grid_lon": [], "label": []})
        negs = generate_negatives(empty)
        assert len(negs) == 0


class TestLabelSevereStorms:
    def _make_strokes(self, n: int, lat: float = 47.0, lon: float = 8.0, base_time: str = "2023-07-15T14:00:00Z") -> pd.DataFrame:
        base = pd.Timestamp(base_time, tz="UTC")
        return pd.DataFrame({
            "datetime": [base + pd.Timedelta(seconds=i * 10) for i in range(n)],
            "latitude": [lat] * n,
            "longitude": [lon] * n,
        })

    def test_above_threshold_labelled_severe(self):
        strokes = self._make_strokes(150)
        result = label_severe_storms(strokes)
        assert len(result) == 1
        assert result.iloc[0]["label"] == 1

    def test_below_threshold_not_labelled(self):
        strokes = self._make_strokes(50)
        result = label_severe_storms(strokes)
        assert len(result) == 0

    def test_exactly_at_threshold(self):
        strokes = self._make_strokes(100)
        result = label_severe_storms(strokes)
        assert len(result) == 1

    def test_multiple_cells(self):
        cell_a = self._make_strokes(150, lat=47.0, lon=8.0)
        cell_b = self._make_strokes(30, lat=46.0, lon=7.0)
        strokes = pd.concat([cell_a, cell_b], ignore_index=True)
        result = label_severe_storms(strokes)
        assert len(result) == 1
        assert result.iloc[0]["grid_lat"] == 47.0

    def test_multiple_hours_same_cell(self):
        hour1 = self._make_strokes(120, base_time="2023-07-15T14:00:00Z")
        hour2 = self._make_strokes(120, base_time="2023-07-15T15:00:00Z")
        strokes = pd.concat([hour1, hour2], ignore_index=True)
        result = label_severe_storms(strokes)
        assert len(result) == 2

    def test_has_flash_count(self):
        strokes = self._make_strokes(200)
        result = label_severe_storms(strokes)
        assert result.iloc[0]["flash_count"] == 200

    def test_empty_input(self):
        empty = pd.DataFrame({"datetime": pd.Series([], dtype="datetime64[ns, UTC]"), "latitude": [], "longitude": []})
        result = label_severe_storms(empty)
        assert len(result) == 0

    def test_snaps_to_grid(self):
        strokes = self._make_strokes(150, lat=47.12, lon=8.07)
        result = label_severe_storms(strokes)
        assert result.iloc[0]["grid_lat"] == 47.0
        assert result.iloc[0]["grid_lon"] == 8.0
