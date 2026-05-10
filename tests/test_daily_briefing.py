"""Tests for daily_briefing pure functions."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("boto3", MagicMock())
os.environ.setdefault("S3_BUCKET", "test-bucket")

_path = Path(__file__).resolve().parent.parent / "tasks" / "daily_briefing" / "main.py"
_spec = importlib.util.spec_from_file_location("daily_briefing_main", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

describe_feature = _mod.describe_feature
format_explanations = _mod.format_explanations
is_within_24h = _mod.is_within_24h
build_source_agreement = _mod.build_source_agreement
build_prompt = _mod.build_prompt
load_json_from_s3 = _mod.load_json_from_s3
find_latest_forecast = _mod.find_latest_forecast
find_latest_alerts = _mod.find_latest_alerts
generate_summary = _mod.generate_summary
send_sms = _mod.send_sms


class TestDescribeFeature:
    def test_cape_feature(self):
        assert "CAPE" in describe_feature("h1_cape_centre")

    def test_wind_shear(self):
        assert "wind shear" in describe_feature("h2_wind_shear_925_700")

    def test_v10_wind(self):
        assert "wind" in describe_feature("h3_v10_max")

    def test_boundary_layer(self):
        assert "boundary layer" in describe_feature("h1_blh_min")

    def test_temperature(self):
        assert "temperature" in describe_feature("h1_t2m_centre")

    def test_dewpoint(self):
        assert "dewpoint" in describe_feature("h2_d2m_mean")

    def test_seasonal_cycle(self):
        assert "seasonal" in describe_feature("doy_sin")
        assert "seasonal" in describe_feature("doy_cos")

    def test_unknown_feature_returns_raw(self):
        assert describe_feature("some_weird_var") == "some_weird_var"


class TestFormatExplanations:
    def test_empty_high_risk(self):
        assert format_explanations({"high_risk_cells": []}) == ""

    def test_no_high_risk_key(self):
        assert format_explanations({}) == ""

    def test_formats_drivers(self):
        forecast = {
            "high_risk_cells": [{
                "lat": 47.0,
                "lon": 8.0,
                "probability": 0.6,
                "top_contributors": [
                    {"feature": "h1_cape_centre", "contribution": 0.8},
                    {"feature": "h2_wind_shear_925_700", "contribution": 0.5},
                    {"feature": "h1_blh_min", "contribution": -0.3},
                ],
            }]
        }
        result = format_explanations(forecast)
        assert "high" in result
        assert "CAPE" in result

    def test_deduplicates_features(self):
        forecast = {
            "high_risk_cells": [{
                "lat": 47.0,
                "lon": 8.0,
                "probability": 0.6,
                "top_contributors": [
                    {"feature": "h1_cape_centre", "contribution": 0.8},
                    {"feature": "h2_cape_max", "contribution": 0.5},
                    {"feature": "h1_blh_min", "contribution": -0.3},
                ],
            }]
        }
        result = format_explanations(forecast)
        assert result.count("CAPE") == 1

    def test_negative_contribution_shows_low(self):
        forecast = {
            "high_risk_cells": [{
                "lat": 47.0,
                "lon": 8.0,
                "probability": 0.5,
                "top_contributors": [
                    {"feature": "h1_blh_min", "contribution": -0.5},
                ],
            }]
        }
        result = format_explanations(forecast)
        assert "low" in result


class TestIsWithin24h:
    def test_within_range(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert is_within_24h("2026-05-10T14:00:00+00:00", now)

    def test_outside_range(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert not is_within_24h("2026-05-12T14:00:00+00:00", now)

    def test_past_time(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert not is_within_24h("2026-05-09T10:00:00+00:00", now)

    def test_none_input(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert not is_within_24h(None, now)

    def test_empty_string(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert not is_within_24h("", now)

    def test_naive_datetime_string(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert is_within_24h("2026-05-10T20:00", now)

    def test_z_suffix(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert is_within_24h("2026-05-10T20:00:00Z", now)


class TestBuildSourceAgreement:
    def test_empty_when_no_risk(self):
        forecast = {"high_risk_cells": [], "max_probability": 0.1}
        alerts = {"sources": []}
        assert build_source_agreement(forecast, alerts) == ""

    def test_model_signal_included(self):
        forecast = {
            "high_risk_cells": [{"lat": 47.0, "lon": 8.0, "probability": 0.5}],
        }
        result = build_source_agreement(forecast, None)
        assert "Our model" in result
        assert "50%" in result

    def test_open_meteo_cape_signal(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")
        alerts = {
            "sources": [{
                "source": "open_meteo",
                "status": "ok",
                "locations": [{
                    "name": "Lugano",
                    "lat": 46.0,
                    "lon": 8.95,
                    "peak_cape_jkg": 1200,
                    "peak_cape_time": future,
                }],
            }]
        }
        result = build_source_agreement(None, alerts)
        assert "Open-Meteo" in result
        assert "Lugano" in result

    def test_tomorrow_io_signal(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:00Z")
        alerts = {
            "sources": [{
                "source": "tomorrow_io",
                "status": "ok",
                "locations": [{
                    "name": "Lucerne",
                    "lat": 47.05,
                    "lon": 8.31,
                    "peak_thunderstorm_prob_pct": 55,
                    "peak_time": future,
                }],
            }]
        }
        result = build_source_agreement(None, alerts)
        assert "Tomorrow.io" in result
        assert "55%" in result

    def test_skips_errors(self):
        alerts = {
            "sources": [{
                "source": "openweathermap",
                "status": "ok",
                "locations": [{"name": "Zurich", "error": "401 Unauthorized"}],
            }]
        }
        result = build_source_agreement(None, alerts)
        assert result == ""

    def test_multiple_sources_both_shown(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")
        forecast = {
            "high_risk_cells": [{"lat": 46.0, "lon": 8.95, "probability": 0.4}],
        }
        alerts = {
            "sources": [{
                "source": "open_meteo",
                "status": "ok",
                "locations": [{
                    "name": "Lugano",
                    "lat": 46.0,
                    "lon": 8.95,
                    "peak_cape_jkg": 800,
                    "peak_cape_time": future,
                }],
            }]
        }
        result = build_source_agreement(forecast, alerts)
        assert "Our model" in result
        assert "Open-Meteo" in result


class TestBuildPrompt:
    def test_includes_24h_constraint(self):
        prompt = build_prompt(None, None)
        assert "NEXT 24 HOURS" in prompt

    def test_no_data_generic_advisory(self):
        prompt = build_prompt(None, None)
        assert "generic advisory" in prompt

    def test_includes_model_explanation(self):
        forecast = {
            "max_probability": 0.5,
            "cells_above_30pct": 2,
            "cells_above_50pct": 1,
            "high_risk_cells": [{
                "lat": 47.0,
                "lon": 8.0,
                "probability": 0.5,
                "top_contributors": [
                    {"feature": "h1_cape_centre", "contribution": 0.8},
                ],
            }],
        }
        prompt = build_prompt(forecast, None)
        assert "CAPE" in prompt
        assert "max_probability=50" in prompt

    def test_includes_overall_risk(self):
        alerts = {
            "overall": {"overall_risk": "moderate", "confidence": "high", "sources_ok": 3, "sources_queried": 4},
            "sources": [],
        }
        prompt = build_prompt(None, alerts)
        assert "moderate" in prompt


class TestLoadJsonFromS3:
    def test_success(self):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"key": "value"}).encode()
        _mod.s3 = MagicMock()
        _mod.s3.get_object.return_value = {"Body": mock_body}
        result = load_json_from_s3("some/key.json")
        assert result == {"key": "value"}

    def test_returns_none_on_error(self):
        _mod.s3 = MagicMock()
        _mod.s3.get_object.side_effect = Exception("NoSuchKey")
        result = load_json_from_s3("missing/key.json")
        assert result is None


class TestFindLatestForecast:
    def test_returns_latest(self):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"max_probability": 0.5}).encode()
        _mod.s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [
                {"Key": "forecast/20260509/summary.json"},
                {"Key": "forecast/20260510/summary.json"},
            ]}
        ]
        _mod.s3.get_paginator.return_value = mock_paginator
        _mod.s3.get_object.return_value = {"Body": mock_body}
        result = find_latest_forecast()
        assert result == {"max_probability": 0.5}
        _mod.s3.get_object.assert_called_once()

    def test_returns_none_when_empty(self):
        _mod.s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        _mod.s3.get_paginator.return_value = mock_paginator
        result = find_latest_forecast()
        assert result is None


class TestFindLatestAlerts:
    def test_returns_latest(self):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"overall": {"overall_risk": "low"}}).encode()
        _mod.s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [
                {"Key": "alerts/20260510/risk_assessment.json"},
            ]}
        ]
        _mod.s3.get_paginator.return_value = mock_paginator
        _mod.s3.get_object.return_value = {"Body": mock_body}
        result = find_latest_alerts()
        assert result == {"overall": {"overall_risk": "low"}}

    def test_returns_none_when_empty(self):
        _mod.s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        _mod.s3.get_paginator.return_value = mock_paginator
        result = find_latest_alerts()
        assert result is None


class TestGenerateSummary:
    def test_calls_bedrock(self):
        _mod.bedrock = MagicMock()
        _mod.bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "Low risk today."}]}}
        }
        result = generate_summary("Test prompt")
        assert result == "Low risk today."
        _mod.bedrock.converse.assert_called_once()


class TestSendSms:
    def test_sends_to_phone(self):
        _mod.sns = MagicMock()
        _mod.sns.publish.return_value = {"MessageId": "abc123"}
        _mod.PHONE_NUMBER = "+41791234567"
        _mod.SNS_TOPIC_ARN = ""
        send_sms("Hello")
        _mod.sns.publish.assert_called_once()
        call_kwargs = _mod.sns.publish.call_args[1]
        assert call_kwargs["PhoneNumber"] == "+41791234567"
        assert call_kwargs["Message"] == "Hello"

    def test_sends_to_topic(self):
        _mod.sns = MagicMock()
        _mod.sns.publish.return_value = {"MessageId": "abc123"}
        _mod.PHONE_NUMBER = ""
        _mod.SNS_TOPIC_ARN = "arn:aws:sns:eu-central-1:123:topic"
        send_sms("Hello")
        call_kwargs = _mod.sns.publish.call_args[1]
        assert call_kwargs["TopicArn"] == "arn:aws:sns:eu-central-1:123:topic"

    def test_prints_when_no_destination(self, capsys):
        _mod.sns = MagicMock()
        _mod.PHONE_NUMBER = ""
        _mod.SNS_TOPIC_ARN = ""
        send_sms("Hello")
        _mod.sns.publish.assert_not_called()
        captured = capsys.readouterr()
        assert "Hello" in captured.out


class TestBuildSourceAgreementExtraBranches:
    def test_visual_crossing_signal(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:00+00:00")
        alerts = {
            "sources": [{
                "source": "visual_crossing",
                "status": "ok",
                "locations": [{
                    "name": "Bern",
                    "lat": 46.95,
                    "lon": 7.45,
                    "severerisk_max": 60,
                    "severerisk_peak_time": future,
                }],
            }]
        }
        result = build_source_agreement(None, alerts)
        assert "Visual Crossing" in result
        assert "Bern" in result

    def test_openweathermap_thunderstorm(self):
        alerts = {
            "sources": [{
                "source": "openweathermap",
                "status": "ok",
                "locations": [{
                    "name": "Geneva",
                    "lat": 46.2,
                    "lon": 6.15,
                    "has_thunderstorm": True,
                }],
            }]
        }
        result = build_source_agreement(None, alerts)
        assert "OpenWeatherMap" in result
        assert "Geneva" in result


class TestFormatExplanationsEdgeCases:
    def test_empty_contributors_skipped(self):
        forecast = {
            "high_risk_cells": [{
                "lat": 47.0,
                "lon": 8.0,
                "probability": 0.6,
                "top_contributors": [],
            }]
        }
        result = format_explanations(forecast)
        assert result == ""


class TestIsWithin24hEdgeCases:
    def test_invalid_format(self):
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        assert not is_within_24h("not-a-date", now)
