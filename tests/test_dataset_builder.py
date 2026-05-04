"""Tests for dataset_builder pure functions.

The dataset_builder task reads S3_BUCKET from os.environ at import time,
so we patch that before importing the module.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

# Stub out boto3 and patch env before importing the module
sys.modules.setdefault("boto3", MagicMock())
os.environ.setdefault("S3_BUCKET", "test-bucket")

_db_path = Path(__file__).resolve().parent.parent / "tasks" / "dataset_builder" / "main.py"
_spec = importlib.util.spec_from_file_location("dataset_builder_main", _db_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

quality_filter = _mod.quality_filter
stratified_split = _mod.stratified_split
generate_manifest = _mod.generate_manifest
validate_dataset = _mod.validate_dataset


def _make_feature_df(n_rows: int, n_years: int = 5, missing_frac: float = 0.0) -> pd.DataFrame:
    """Build a synthetic feature DataFrame for testing."""
    rng = np.random.default_rng(42)
    years = list(range(2015, 2015 + n_years))
    rows_per_year = n_rows // n_years

    data = {
        "event_datetime": [],
        "grid_lat": rng.uniform(45.3, 48.3, n_rows).tolist(),
        "grid_lon": rng.uniform(5.5, 11.0, n_rows).tolist(),
        "label": rng.integers(0, 2, n_rows).tolist(),
    }
    for y in years:
        data["event_datetime"].extend([f"{y}-07-15T14:00:00"] * rows_per_year)
    remainder = n_rows - rows_per_year * n_years
    if remainder > 0:
        data["event_datetime"].extend([f"{years[-1]}-08-01T12:00:00"] * remainder)

    for i in range(20):
        col = f"h1_var{i}_centre"
        vals = rng.standard_normal(n_rows).tolist()
        if missing_frac > 0:
            for j in range(int(n_rows * missing_frac)):
                vals[j] = np.nan
        data[col] = vals

    data["hour_sin"] = rng.uniform(-1, 1, n_rows).tolist()
    data["hour_cos"] = rng.uniform(-1, 1, n_rows).tolist()
    data["doy_sin"] = rng.uniform(-1, 1, n_rows).tolist()
    data["doy_cos"] = rng.uniform(-1, 1, n_rows).tolist()

    return pd.DataFrame(data)


class TestQualityFilter:
    def test_keeps_complete_rows(self):
        df = _make_feature_df(100, missing_frac=0.0)
        result = quality_filter(df)
        assert len(result) == 100

    def test_drops_rows_with_too_many_missing(self):
        df = _make_feature_df(100, missing_frac=0.0)
        feature_cols = [c for c in df.columns if c.startswith("h")]
        df.loc[0, feature_cols] = np.nan
        result = quality_filter(df)
        assert len(result) == 99

    def test_drops_constant_columns(self):
        df = _make_feature_df(100, missing_frac=0.0)
        df["h1_constant_centre"] = 42.0
        result = quality_filter(df)
        assert "h1_constant_centre" not in result.columns

    def test_preserves_non_feature_columns(self):
        df = _make_feature_df(100)
        result = quality_filter(df)
        assert "event_datetime" in result.columns
        assert "label" in result.columns

    def test_no_feature_columns_returns_unchanged(self):
        df = pd.DataFrame({"event_datetime": ["2023-01-01"], "label": [1]})
        result = quality_filter(df)
        assert len(result) == 1

    def test_all_nan_column_dropped(self):
        df = _make_feature_df(50)
        df["h1_allnan_centre"] = np.nan
        result = quality_filter(df)
        assert "h1_allnan_centre" not in result.columns


class TestStratifiedSplit:
    def test_returns_three_frames(self):
        df = _make_feature_df(500, n_years=10)
        train, val, test = stratified_split(df)
        assert isinstance(train, pd.DataFrame)
        assert isinstance(val, pd.DataFrame)
        assert isinstance(test, pd.DataFrame)

    def test_no_row_loss(self):
        df = _make_feature_df(500, n_years=10)
        train, val, test = stratified_split(df)
        assert len(train) + len(val) + len(test) == len(df)

    def test_no_year_overlap(self):
        df = _make_feature_df(500, n_years=10)
        train, val, test = stratified_split(df)
        train_years = set(pd.to_datetime(train["event_datetime"]).dt.year)
        val_years = set(pd.to_datetime(val["event_datetime"]).dt.year)
        test_years = set(pd.to_datetime(test["event_datetime"]).dt.year)
        assert train_years.isdisjoint(val_years)
        assert train_years.isdisjoint(test_years)
        assert val_years.isdisjoint(test_years)

    def test_train_is_largest(self):
        df = _make_feature_df(500, n_years=10)
        train, val, test = stratified_split(df)
        assert len(train) >= len(val)
        assert len(train) >= len(test)

    def test_all_splits_nonempty(self):
        df = _make_feature_df(300, n_years=5)
        train, val, test = stratified_split(df)
        assert len(train) > 0
        assert len(val) > 0
        assert len(test) > 0

    def test_two_years_produces_train_and_test(self):
        df = _make_feature_df(100, n_years=2)
        train, val, test = stratified_split(df)
        assert len(train) + len(val) + len(test) == 100
        assert len(train) > 0
        assert len(test) > 0

    def test_removes_internal_year_column(self):
        df = _make_feature_df(200, n_years=5)
        train, val, test = stratified_split(df)
        assert "_year" not in train.columns
        assert "_year" not in val.columns
        assert "_year" not in test.columns


class TestGenerateManifest:
    def test_sample_counts(self):
        train = _make_feature_df(70, n_years=1)
        val = _make_feature_df(15, n_years=1)
        test = _make_feature_df(15, n_years=1)
        manifest = generate_manifest(train, val, test)
        assert manifest["n_samples"]["train"] == 70
        assert manifest["n_samples"]["val"] == 15
        assert manifest["n_samples"]["test"] == 15

    def test_feature_count(self):
        train = _make_feature_df(50, n_years=1)
        val = _make_feature_df(10, n_years=1)
        test = _make_feature_df(10, n_years=1)
        manifest = generate_manifest(train, val, test)
        assert manifest["n_features"] == 24  # 20 h1_* + 4 time features

    def test_label_distribution(self):
        df = _make_feature_df(100, n_years=1)
        manifest = generate_manifest(df, df.iloc[:0], df.iloc[:0])
        for key in manifest["label_distribution"]:
            assert isinstance(manifest["label_distribution"][key], int)

    def test_feature_stats_keys(self):
        train = _make_feature_df(50, n_years=1)
        manifest = generate_manifest(train, train.iloc[:0], train.iloc[:0])
        for col, stats in manifest["feature_stats"].items():
            assert "dtype" in stats
            assert "missing_frac" in stats
            assert "mean" in stats
            assert "std" in stats
            assert "min" in stats
            assert "max" in stats

    def test_meta_columns_listed(self):
        train = _make_feature_df(50, n_years=1)
        manifest = generate_manifest(train, train.iloc[:0], train.iloc[:0])
        assert "event_datetime" in manifest["meta_columns"]
        assert "label" in manifest["meta_columns"]


class TestValidateDataset:
    def _good_manifest(self) -> dict:
        return {
            "n_samples": {"train": 700, "val": 150, "test": 150},
            "n_features": 50,
            "label_distribution": {"1": 250, "0": 750},
            "feature_stats": {
                f"h1_var{i}": {"missing_frac": 0.05}
                for i in range(50)
            },
        }

    def test_passes_good_manifest(self):
        validate_dataset(self._good_manifest())

    def test_fails_too_few_samples(self):
        m = self._good_manifest()
        m["n_samples"] = {"train": 30, "val": 10, "test": 10}
        with pytest.raises(SystemExit):
            validate_dataset(m)

    def test_fails_too_few_features(self):
        m = self._good_manifest()
        m["n_features"] = 5
        with pytest.raises(SystemExit):
            validate_dataset(m)

    def test_fails_no_positives(self):
        m = self._good_manifest()
        m["label_distribution"] = {"0": 1000}
        with pytest.raises(SystemExit):
            validate_dataset(m)

    def test_fails_no_negatives(self):
        m = self._good_manifest()
        m["label_distribution"] = {"1": 1000}
        with pytest.raises(SystemExit):
            validate_dataset(m)

    def test_fails_extreme_class_ratio(self):
        m = self._good_manifest()
        m["label_distribution"] = {"1": 10, "0": 990}
        with pytest.raises(SystemExit):
            validate_dataset(m)

    def test_fails_high_missingness(self):
        m = self._good_manifest()
        m["feature_stats"]["h1_bad"] = {"missing_frac": 0.95}
        with pytest.raises(SystemExit):
            validate_dataset(m)

    def test_passes_moderate_ratio(self):
        m = self._good_manifest()
        m["label_distribution"] = {"1": 200, "0": 800}
        validate_dataset(m)
