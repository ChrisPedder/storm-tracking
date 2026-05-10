"""Query external weather APIs for thunderstorm risk across Switzerland.

Aggregates convective risk data from Open-Meteo, Visual Crossing,
Tomorrow.io, and OpenWeatherMap into a unified daily risk assessment.
"""

import io
import json
import logging
import os
import sys
from datetime import datetime, timezone

import boto3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("weather-alerts")

S3_BUCKET = os.environ["S3_BUCKET"]
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "alerts/")

VISUAL_CROSSING_KEY = os.environ.get("VISUAL_CROSSING_KEY", "")
TOMORROW_IO_KEY = os.environ.get("TOMORROW_IO_KEY", "")
OPENWEATHERMAP_KEY = os.environ.get("OPENWEATHERMAP_KEY", "")

# Switzerland representative points for point-based APIs
SWISS_LOCATIONS = [
    {"name": "Zurich", "lat": 47.37, "lon": 8.55},
    {"name": "Bern", "lat": 46.95, "lon": 7.45},
    {"name": "Geneva", "lat": 46.20, "lon": 6.15},
    {"name": "Lugano", "lat": 46.00, "lon": 8.95},
    {"name": "Basel", "lat": 47.56, "lon": 7.59},
    {"name": "Lucerne", "lat": 47.05, "lon": 8.31},
    {"name": "St. Gallen", "lat": 47.42, "lon": 9.37},
    {"name": "Sion", "lat": 46.23, "lon": 7.36},
]

BBOX_NORTH = 48.3
BBOX_SOUTH = 45.3
BBOX_WEST = 5.5
BBOX_EAST = 11.0

REQUEST_TIMEOUT = 30


# ── Open-Meteo (no key needed) ───────────────────────────────

def query_open_meteo() -> dict:
    """Query Open-Meteo DWD ICON for convective parameters over Switzerland grid."""
    url = "https://api.open-meteo.com/v1/dwd-icon"
    params = {
        "latitude": ",".join(str(loc["lat"]) for loc in SWISS_LOCATIONS),
        "longitude": ",".join(str(loc["lon"]) for loc in SWISS_LOCATIONS),
        "hourly": "cape,convective_inhibition,lifted_index",
        "forecast_days": 2,
        "timezone": "UTC",
    }

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Open-Meteo query failed: %s", exc)
        return {"source": "open_meteo", "status": "error", "error": str(exc)}

    locations = []
    results = data if isinstance(data, list) else [data]
    for i, result in enumerate(results):
        loc_name = SWISS_LOCATIONS[i]["name"] if i < len(SWISS_LOCATIONS) else f"loc_{i}"
        hourly = result.get("hourly", {})
        cape_values = hourly.get("cape", [])
        cin_values = hourly.get("convective_inhibition", [])
        li_values = hourly.get("lifted_index", [])
        times = hourly.get("time", [])

        valid_cape = [v for v in cape_values if v is not None]
        valid_cin = [v for v in cin_values if v is not None]
        valid_li = [v for v in li_values if v is not None]

        peak_cape = max(valid_cape) if valid_cape else 0
        peak_cape_time = None
        if valid_cape and peak_cape > 0:
            idx = cape_values.index(peak_cape)
            peak_cape_time = times[idx] if idx < len(times) else None

        locations.append({
            "name": loc_name,
            "lat": SWISS_LOCATIONS[i]["lat"],
            "lon": SWISS_LOCATIONS[i]["lon"],
            "peak_cape_jkg": round(peak_cape, 1),
            "peak_cape_time": peak_cape_time,
            "max_cin_jkg": round(min(valid_cin), 1) if valid_cin else 0,
            "min_lifted_index": round(min(valid_li), 1) if valid_li else None,
            "cape_hours_above_500": sum(1 for v in valid_cape if v >= 500),
            "cape_hours_above_1000": sum(1 for v in valid_cape if v >= 1000),
        })

    max_cape = max(loc["peak_cape_jkg"] for loc in locations) if locations else 0
    risk_level = "low"
    if max_cape >= 2000:
        risk_level = "high"
    elif max_cape >= 1000:
        risk_level = "moderate"
    elif max_cape >= 500:
        risk_level = "slight"

    return {
        "source": "open_meteo",
        "status": "ok",
        "model": "DWD ICON",
        "risk_level": risk_level,
        "max_cape_jkg": round(max_cape, 1),
        "locations": locations,
    }


# ── Visual Crossing ──────────────────────────────────────────

def query_visual_crossing() -> dict:
    """Query Visual Crossing for severerisk composite index."""
    if not VISUAL_CROSSING_KEY:
        return {"source": "visual_crossing", "status": "skipped", "reason": "no API key"}

    locations = []
    for loc in SWISS_LOCATIONS:
        url = (
            f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
            f"/{loc['lat']},{loc['lon']}/next2days"
        )
        params = {
            "unitGroup": "metric",
            "include": "hours",
            "key": VISUAL_CROSSING_KEY,
            "contentType": "json",
            "elements": "datetime,severerisk,cape",
        }

        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Visual Crossing failed for %s: %s", loc["name"], exc)
            locations.append({
                "name": loc["name"],
                "lat": loc["lat"],
                "lon": loc["lon"],
                "error": str(exc),
            })
            continue

        all_hours = []
        for day in data.get("days", []):
            all_hours.extend(day.get("hours", []))

        severe_risks = [h.get("severerisk", 0) or 0 for h in all_hours]
        peak_risk = max(severe_risks) if severe_risks else 0
        peak_idx = severe_risks.index(peak_risk) if peak_risk > 0 else 0
        peak_time = all_hours[peak_idx].get("datetime") if all_hours else None

        locations.append({
            "name": loc["name"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "peak_severerisk": round(peak_risk, 1),
            "peak_time": peak_time,
            "hours_above_30": sum(1 for r in severe_risks if r >= 30),
            "hours_above_60": sum(1 for r in severe_risks if r >= 60),
        })

    valid_locs = [l for l in locations if "peak_severerisk" in l]
    max_risk = max(l["peak_severerisk"] for l in valid_locs) if valid_locs else 0

    risk_level = "low"
    if max_risk >= 70:
        risk_level = "high"
    elif max_risk >= 40:
        risk_level = "moderate"
    elif max_risk >= 20:
        risk_level = "slight"

    return {
        "source": "visual_crossing",
        "status": "ok",
        "risk_level": risk_level,
        "max_severerisk": round(max_risk, 1),
        "locations": locations,
    }


# ── Tomorrow.io ──────────────────────────────────────────────

def query_tomorrow_io() -> dict:
    """Query Tomorrow.io for thunderstorm probability."""
    if not TOMORROW_IO_KEY:
        return {"source": "tomorrow_io", "status": "skipped", "reason": "no API key"}

    locations = []
    for loc in SWISS_LOCATIONS:
        url = "https://api.tomorrow.io/v4/timelines"
        params = {
            "location": f"{loc['lat']},{loc['lon']}",
            "fields": "thunderstormProbability,precipitationProbability",
            "timesteps": "1h",
            "startTime": "now",
            "endTime": "nowPlus48h",
            "apikey": TOMORROW_IO_KEY,
        }

        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Tomorrow.io failed for %s: %s", loc["name"], exc)
            locations.append({
                "name": loc["name"],
                "lat": loc["lat"],
                "lon": loc["lon"],
                "error": str(exc),
            })
            continue

        timelines = data.get("data", {}).get("timelines", [])
        intervals = timelines[0].get("intervals", []) if timelines else []

        thunder_probs = []
        for interval in intervals:
            vals = interval.get("values", {})
            tp = vals.get("thunderstormProbability")
            if tp is not None:
                thunder_probs.append({
                    "time": interval.get("startTime"),
                    "probability": tp,
                })

        peak_prob = max(t["probability"] for t in thunder_probs) if thunder_probs else 0
        peak_time = None
        if thunder_probs and peak_prob > 0:
            peak_entry = max(thunder_probs, key=lambda t: t["probability"])
            peak_time = peak_entry["time"]

        locations.append({
            "name": loc["name"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "peak_thunderstorm_prob_pct": round(peak_prob, 1),
            "peak_time": peak_time,
            "hours_above_30pct": sum(1 for t in thunder_probs if t["probability"] >= 30),
            "hours_above_60pct": sum(1 for t in thunder_probs if t["probability"] >= 60),
        })

    valid_locs = [l for l in locations if "peak_thunderstorm_prob_pct" in l]
    max_prob = max(l["peak_thunderstorm_prob_pct"] for l in valid_locs) if valid_locs else 0

    risk_level = "low"
    if max_prob >= 70:
        risk_level = "high"
    elif max_prob >= 40:
        risk_level = "moderate"
    elif max_prob >= 20:
        risk_level = "slight"

    return {
        "source": "tomorrow_io",
        "status": "ok",
        "risk_level": risk_level,
        "max_thunderstorm_prob_pct": round(max_prob, 1),
        "locations": locations,
    }


# ── OpenWeatherMap ───────────────────────────────────────────

def query_openweathermap() -> dict:
    """Query OpenWeatherMap One Call 3.0 for weather alerts and thunderstorm conditions."""
    if not OPENWEATHERMAP_KEY:
        return {"source": "openweathermap", "status": "skipped", "reason": "no API key"}

    locations = []
    all_alerts = []

    for loc in SWISS_LOCATIONS:
        url = "https://api.openweathermap.org/data/3.0/onecall"
        params = {
            "lat": loc["lat"],
            "lon": loc["lon"],
            "appid": OPENWEATHERMAP_KEY,
            "exclude": "minutely,daily",
            "units": "metric",
        }

        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("OpenWeatherMap failed for %s: %s", loc["name"], exc)
            locations.append({
                "name": loc["name"],
                "lat": loc["lat"],
                "lon": loc["lon"],
                "error": str(exc),
            })
            continue

        # Extract alerts
        alerts = data.get("alerts", [])
        thunderstorm_alerts = [
            a for a in alerts
            if "thunder" in a.get("event", "").lower()
            or "storm" in a.get("event", "").lower()
            or "gewitter" in a.get("event", "").lower()
        ]
        for alert in thunderstorm_alerts:
            all_alerts.append({
                "location": loc["name"],
                "event": alert.get("event"),
                "sender": alert.get("sender_name"),
                "start": alert.get("start"),
                "end": alert.get("end"),
                "description": alert.get("description", "")[:200],
            })

        # Check hourly for thunderstorm weather codes (2xx)
        hourly = data.get("hourly", [])
        thunder_hours = []
        for h in hourly:
            weather = h.get("weather", [{}])
            for w in weather:
                if 200 <= w.get("id", 0) < 300:
                    thunder_hours.append({
                        "time": datetime.fromtimestamp(h["dt"], tz=timezone.utc).isoformat(),
                        "description": w.get("description", ""),
                        "id": w["id"],
                    })
                    break

        locations.append({
            "name": loc["name"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "thunderstorm_hours": len(thunder_hours),
            "first_thunder_time": thunder_hours[0]["time"] if thunder_hours else None,
            "alerts": len(thunderstorm_alerts),
        })

    has_alerts = len(all_alerts) > 0
    max_thunder_hours = max((l.get("thunderstorm_hours", 0) for l in locations), default=0)

    risk_level = "low"
    if has_alerts:
        risk_level = "high"
    elif max_thunder_hours >= 6:
        risk_level = "moderate"
    elif max_thunder_hours >= 2:
        risk_level = "slight"

    return {
        "source": "openweathermap",
        "status": "ok",
        "risk_level": risk_level,
        "active_alerts": all_alerts,
        "locations": locations,
    }


# ── Risk aggregation ─────────────────────────────────────────

RISK_SCORES = {"low": 0, "slight": 1, "moderate": 2, "high": 3}


def aggregate_risk(sources: list[dict]) -> dict:
    """Combine multiple sources into an overall risk assessment."""
    active_sources = [s for s in sources if s.get("status") == "ok"]
    risk_levels = [s.get("risk_level", "low") for s in active_sources]
    risk_scores = [RISK_SCORES.get(r, 0) for r in risk_levels]

    if not risk_scores:
        return {"overall_risk": "unknown", "confidence": "none"}

    max_score = max(risk_scores)
    avg_score = sum(risk_scores) / len(risk_scores)

    # Overall risk: take the maximum but downgrade if only one source reports high
    if max_score == 3 and sum(1 for s in risk_scores if s >= 2) >= 2:
        overall = "high"
    elif max_score >= 2:
        overall = "moderate"
    elif avg_score >= 1:
        overall = "slight"
    else:
        overall = "low"

    agreement = sum(1 for s in risk_scores if s == max_score) / len(risk_scores)
    confidence = "high" if agreement >= 0.5 else "moderate" if len(active_sources) >= 3 else "low"

    return {
        "overall_risk": overall,
        "confidence": confidence,
        "sources_queried": len(sources),
        "sources_ok": len(active_sources),
        "source_risk_levels": {s["source"]: s.get("risk_level", "n/a") for s in sources},
    }


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    logger.info("Weather alerts starting")
    logger.info("  S3 bucket: %s", S3_BUCKET)
    logger.info("  Output prefix: %s", OUTPUT_PREFIX)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")

    logger.info("Querying Open-Meteo...")
    open_meteo = query_open_meteo()
    logger.info("  Open-Meteo: %s (max CAPE: %s J/kg)",
                open_meteo.get("risk_level", "error"),
                open_meteo.get("max_cape_jkg", "n/a"))

    logger.info("Querying Visual Crossing...")
    visual_crossing = query_visual_crossing()
    logger.info("  Visual Crossing: %s (max severerisk: %s)",
                visual_crossing.get("risk_level", visual_crossing.get("status")),
                visual_crossing.get("max_severerisk", "n/a"))

    logger.info("Querying Tomorrow.io...")
    tomorrow = query_tomorrow_io()
    logger.info("  Tomorrow.io: %s (max thunder prob: %s%%)",
                tomorrow.get("risk_level", tomorrow.get("status")),
                tomorrow.get("max_thunderstorm_prob_pct", "n/a"))

    logger.info("Querying OpenWeatherMap...")
    owm = query_openweathermap()
    logger.info("  OpenWeatherMap: %s (alerts: %d)",
                owm.get("risk_level", owm.get("status")),
                len(owm.get("active_alerts", [])))

    sources = [open_meteo, visual_crossing, tomorrow, owm]
    overall = aggregate_risk(sources)

    report = {
        "generated_at": now.isoformat(),
        "date": date_str,
        "region": "Switzerland",
        "overall": overall,
        "sources": sources,
    }

    s3 = boto3.client("s3")
    key = f"{OUTPUT_PREFIX}{date_str}/risk_assessment.json"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(report, indent=2).encode(),
        ContentType="application/json",
    )
    logger.info("Uploaded risk assessment to s3://%s/%s", S3_BUCKET, key)
    logger.info("Overall risk: %s (confidence: %s)",
                overall["overall_risk"], overall["confidence"])


if __name__ == "__main__":
    main()
