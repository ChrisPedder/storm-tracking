"""Tests for weather_alerts pure functions."""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock


sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("requests", MagicMock())
os.environ.setdefault("S3_BUCKET", "test-bucket")

_path = Path(__file__).resolve().parent.parent / "tasks" / "weather_alerts" / "main.py"
_spec = importlib.util.spec_from_file_location("weather_alerts_main", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

aggregate_risk = _mod.aggregate_risk
RISK_SCORES = _mod.RISK_SCORES


class TestRiskScores:
    def test_ordering(self):
        assert RISK_SCORES["low"] < RISK_SCORES["slight"]
        assert RISK_SCORES["slight"] < RISK_SCORES["moderate"]
        assert RISK_SCORES["moderate"] < RISK_SCORES["high"]


class TestAggregateRisk:
    def test_all_low(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "low"},
            {"source": "b", "status": "ok", "risk_level": "low"},
            {"source": "c", "status": "ok", "risk_level": "low"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "low"

    def test_single_high_downgraded(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "high"},
            {"source": "b", "status": "ok", "risk_level": "low"},
            {"source": "c", "status": "ok", "risk_level": "low"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "moderate"

    def test_multiple_high_stays_high(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "high"},
            {"source": "b", "status": "ok", "risk_level": "moderate"},
            {"source": "c", "status": "ok", "risk_level": "high"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "high"

    def test_moderate_consensus(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "moderate"},
            {"source": "b", "status": "ok", "risk_level": "moderate"},
            {"source": "c", "status": "ok", "risk_level": "slight"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "moderate"

    def test_slight_when_all_slight(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "slight"},
            {"source": "b", "status": "ok", "risk_level": "slight"},
            {"source": "c", "status": "ok", "risk_level": "slight"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "slight"

    def test_low_when_slight_minority(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "slight"},
            {"source": "b", "status": "ok", "risk_level": "low"},
            {"source": "c", "status": "ok", "risk_level": "low"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "low"

    def test_empty_sources(self):
        result = aggregate_risk([])
        assert result["overall_risk"] == "unknown"
        assert result["confidence"] == "none"

    def test_skipped_sources_excluded(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "moderate"},
            {"source": "b", "status": "skipped", "risk_level": "low"},
        ]
        result = aggregate_risk(sources)
        assert result["sources_ok"] == 1
        assert result["sources_queried"] == 2

    def test_confidence_high_when_agreement(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "moderate"},
            {"source": "b", "status": "ok", "risk_level": "moderate"},
        ]
        result = aggregate_risk(sources)
        assert result["confidence"] == "high"

    def test_confidence_moderate_many_sources(self):
        sources = [
            {"source": "a", "status": "ok", "risk_level": "high"},
            {"source": "b", "status": "ok", "risk_level": "low"},
            {"source": "c", "status": "ok", "risk_level": "low"},
            {"source": "d", "status": "ok", "risk_level": "low"},
        ]
        result = aggregate_risk(sources)
        assert result["confidence"] == "moderate"

    def test_source_risk_levels_reported(self):
        sources = [
            {"source": "open_meteo", "status": "ok", "risk_level": "slight"},
            {"source": "tomorrow_io", "status": "ok", "risk_level": "moderate"},
        ]
        result = aggregate_risk(sources)
        assert result["source_risk_levels"]["open_meteo"] == "slight"
        assert result["source_risk_levels"]["tomorrow_io"] == "moderate"

    def test_all_error_sources(self):
        sources = [
            {"source": "a", "status": "error", "risk_level": "low"},
            {"source": "b", "status": "error", "risk_level": "low"},
        ]
        result = aggregate_risk(sources)
        assert result["overall_risk"] == "unknown"
