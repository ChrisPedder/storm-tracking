"""Tests for storm_forecast pure functions."""

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("cfgrib", MagicMock())
sys.modules.setdefault("eccodes", MagicMock())
sys.modules.setdefault("gribapi", MagicMock())
sys.modules.setdefault("xarray", MagicMock())
sys.modules.setdefault("lightgbm", MagicMock())
sys.modules.setdefault("ecmwf", MagicMock())
sys.modules.setdefault("ecmwf.opendata", MagicMock())
os.environ.setdefault("S3_BUCKET", "test-bucket")

_path = Path(__file__).resolve().parent.parent / "tasks" / "storm_forecast" / "main.py"
_spec = importlib.util.spec_from_file_location("storm_forecast_main", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

snap_to_grid = _mod.snap_to_grid
grid_patch = _mod.grid_patch
build_grid = _mod.build_grid
find_nearest_idx = _mod.find_nearest_idx
encode_time_features = _mod.encode_time_features
required_steps = _mod.required_steps
compute_wind_shear_vec = _mod.compute_wind_shear_vec
compute_tendency_vec = _mod.compute_tendency_vec


class TestSnapToGrid:
    def test_exact(self):
        assert snap_to_grid(47.0, 8.0) == (47.0, 8.0)

    def test_rounds_nearest(self):
        assert snap_to_grid(46.83, 7.42) == (46.75, 7.5)

    def test_rounds_down(self):
        assert snap_to_grid(46.1, 7.1) == (46.0, 7.0)

    def test_rounds_up(self):
        assert snap_to_grid(46.88, 7.88) == (47.0, 8.0)


class TestGridPatch:
    def test_nine_points(self):
        assert len(grid_patch(47.0, 8.0)) == 9

    def test_centre_included(self):
        assert (47.0, 8.0) in grid_patch(47.0, 8.0)

    def test_correct_offsets(self):
        points = grid_patch(47.0, 8.0)
        lats = sorted(set(lat for lat, _ in points))
        lons = sorted(set(lon for _, lon in points))
        assert lats == [46.75, 47.0, 47.25]
        assert lons == [7.75, 8.0, 8.25]


class TestBuildGrid:
    def test_covers_switzerland(self):
        grid = build_grid()
        lats = [p[0] for p in grid]
        lons = [p[1] for p in grid]
        assert min(lats) >= 45.25
        assert max(lats) <= 48.5
        assert min(lons) >= 5.25
        assert max(lons) <= 11.25

    def test_reasonable_count(self):
        grid = build_grid()
        assert 200 <= len(grid) <= 500

    def test_all_on_grid(self):
        grid = build_grid()
        for lat, lon in grid:
            assert abs(lat % 0.25) < 1e-8 or abs(lat % 0.25 - 0.25) < 1e-8
            assert abs(lon % 0.25) < 1e-8 or abs(lon % 0.25 - 0.25) < 1e-8


class TestFindNearestIdx:
    def test_exact_match(self):
        arr = np.array([45.0, 45.5, 46.0, 46.5, 47.0])
        assert find_nearest_idx(arr, 46.0) == 2

    def test_nearest_above(self):
        arr = np.array([45.0, 46.0, 47.0])
        assert find_nearest_idx(arr, 46.6) == 2

    def test_nearest_below(self):
        arr = np.array([45.0, 46.0, 47.0])
        assert find_nearest_idx(arr, 45.3) == 0


class TestEncodeTimeFeatures:
    def test_midnight(self):
        result = encode_time_features(datetime(2023, 1, 1, 0, 0))
        assert result["hour_sin"] == pytest.approx(0.0, abs=1e-10)
        assert result["hour_cos"] == pytest.approx(1.0)

    def test_noon(self):
        result = encode_time_features(datetime(2023, 1, 1, 12, 0))
        assert result["hour_sin"] == pytest.approx(0.0, abs=1e-10)
        assert result["hour_cos"] == pytest.approx(-1.0)

    def test_six_am(self):
        result = encode_time_features(datetime(2023, 1, 1, 6, 0))
        assert result["hour_sin"] == pytest.approx(1.0)

    def test_all_keys(self):
        result = encode_time_features(datetime(2023, 7, 15, 14, 30))
        assert set(result.keys()) == {"hour_sin", "hour_cos", "doy_sin", "doy_cos"}

    def test_bounded(self):
        result = encode_time_features(datetime(2023, 7, 15, 14, 30))
        for val in result.values():
            assert -1.0 <= val <= 1.0


class TestRequiredSteps:
    def test_includes_target_steps(self):
        steps = required_steps()
        for s in [9, 12, 15, 18, 21, 24]:
            assert s in steps

    def test_includes_lead_offsets(self):
        steps = required_steps()
        # Target 9 needs steps 9-3=6, 9-6=3, 9-9=0
        assert 0 in steps
        assert 3 in steps
        assert 6 in steps

    def test_sorted(self):
        steps = required_steps()
        assert steps == sorted(steps)

    def test_no_negatives(self):
        steps = required_steps()
        assert all(s >= 0 for s in steps)


class TestComputeWindShearVec:
    def test_zero_shear(self):
        n = 3
        pres_feats = {
            925: {"u": np.zeros(n), "v": np.zeros(n)},
            700: {"u": np.zeros(n), "v": np.zeros(n)},
        }
        result = compute_wind_shear_vec(pres_feats, n)
        assert "wind_shear_925_700" in result
        assert np.allclose(result["wind_shear_925_700"], 0.0)

    def test_known_shear(self):
        n = 2
        pres_feats = {
            925: {"u": np.array([0.0, 0.0]), "v": np.array([0.0, 0.0])},
            700: {"u": np.array([3.0, 0.0]), "v": np.array([4.0, 5.0])},
        }
        result = compute_wind_shear_vec(pres_feats, n)
        assert result["wind_shear_925_700"][0] == pytest.approx(5.0)
        assert result["wind_shear_925_700"][1] == pytest.approx(5.0)

    def test_missing_level(self):
        n = 2
        pres_feats = {925: {"u": np.zeros(n), "v": np.zeros(n)}}
        result = compute_wind_shear_vec(pres_feats, n)
        assert result == {}

    def test_both_pairs(self):
        n = 1
        pres_feats = {
            925: {"u": np.array([0.0]), "v": np.array([0.0])},
            700: {"u": np.array([1.0]), "v": np.array([0.0])},
            500: {"u": np.array([2.0]), "v": np.array([0.0])},
        }
        result = compute_wind_shear_vec(pres_feats, n)
        assert "wind_shear_925_700" in result
        assert "wind_shear_925_500" in result


class TestComputeTendencyVec:
    def test_computes_difference(self):
        n = 2
        curr = {"cape_centre": np.array([1500.0, 1000.0])}
        prev = {"cape_centre": np.array([1000.0, 800.0])}
        result = compute_tendency_vec(curr, prev, n)
        assert "cape_centre_tendency" in result
        assert result["cape_centre_tendency"][0] == pytest.approx(500.0)
        assert result["cape_centre_tendency"][1] == pytest.approx(200.0)

    def test_missing_variable(self):
        n = 2
        curr = {"cape_centre": np.array([1500.0, 1000.0])}
        prev = {"t2m_centre": np.array([300.0, 300.0])}
        result = compute_tendency_vec(curr, prev, n)
        assert result == {}

    def test_multiple_vars(self):
        n = 1
        curr = {"cape_centre": np.array([1500.0]), "t2m_centre": np.array([300.0]), "sp_centre": np.array([101300.0])}
        prev = {"cape_centre": np.array([1000.0]), "t2m_centre": np.array([298.0]), "sp_centre": np.array([101000.0])}
        result = compute_tendency_vec(curr, prev, n)
        assert len(result) == 3
