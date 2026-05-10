"""Generate a daily severe weather briefing and send via SMS.

Reads model forecast and multi-source weather alerts from S3,
summarizes with Amazon Nova Micro (Bedrock) for cycling safety, sends via AWS SNS.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("daily-briefing")

S3_BUCKET = os.environ["S3_BUCKET"]
FORECAST_PREFIX = os.environ.get("FORECAST_PREFIX", "forecast/")
ALERTS_PREFIX = os.environ.get("ALERTS_PREFIX", "alerts/")
PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0")

s3 = boto3.client("s3")
sns = boto3.client("sns")
bedrock = boto3.client("bedrock-runtime")


def load_json_from_s3(key: str) -> dict | None:
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(resp["Body"].read())
    except Exception as exc:
        logger.warning("Could not load s3://%s/%s: %s", S3_BUCKET, key, exc)
        return None


def find_latest_forecast() -> dict | None:
    """Find and load the most recent forecast summary."""
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=FORECAST_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/summary.json"):
                keys.append(obj["Key"])
    if not keys:
        return None
    latest_key = sorted(keys)[-1]
    logger.info("Loading forecast from %s", latest_key)
    return load_json_from_s3(latest_key)


def find_latest_alerts() -> dict | None:
    """Find and load the most recent risk assessment."""
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=ALERTS_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/risk_assessment.json"):
                keys.append(obj["Key"])
    if not keys:
        return None
    latest_key = sorted(keys)[-1]
    logger.info("Loading alerts from %s", latest_key)
    return load_json_from_s3(latest_key)


FEATURE_DESCRIPTIONS = {
    "cape": "instability energy (CAPE)",
    "cin": "convective inhibition",
    "blh": "boundary layer height",
    "u10": "east-west wind at 10m",
    "v10": "north-south wind at 10m",
    "t2m": "surface temperature",
    "d2m": "dewpoint",
    "sp": "surface pressure",
    "tp": "precipitation",
    "tcc": "cloud cover",
    "tcwv": "atmospheric moisture",
    "wind_shear": "wind shear",
    "doy_sin": "seasonal cycle",
    "doy_cos": "seasonal cycle",
    "hour_sin": "time of day",
    "hour_cos": "time of day",
}


def describe_feature(feature_name: str) -> str:
    """Convert model feature names to human-readable descriptions."""
    lower = feature_name.lower()
    for key, desc in FEATURE_DESCRIPTIONS.items():
        if key in lower:
            return desc
    return feature_name


def format_explanations(forecast: dict) -> str:
    """Format SHAP-based feature explanations into readable text."""
    high_risk = forecast.get("high_risk_cells", [])
    if not high_risk:
        return ""

    lines = []
    for cell in high_risk[:3]:
        contributors = cell.get("top_contributors", [])
        if not contributors:
            continue
        drivers = []
        seen = set()
        for c in contributors:
            desc = describe_feature(c["feature"])
            if desc not in seen:
                sign = "high" if c["contribution"] > 0 else "low"
                drivers.append(f"{sign} {desc}")
                seen.add(desc)
            if len(drivers) >= 3:
                break
        if drivers:
            lines.append(f"  Risk drivers: {', '.join(drivers)}")

    return "\n".join(lines)


def is_within_24h(time_str: str, now: datetime) -> bool:
    """Check if a time string falls within the next 24 hours."""
    if not time_str:
        return False
    try:
        t = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return now <= t <= now + timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def build_source_agreement(forecast: dict | None, alerts: dict | None) -> str:
    """Build a per-location summary showing which sources flag risk."""
    now = datetime.now(timezone.utc)
    location_signals: dict[str, list[str]] = {}

    if forecast:
        for cell in forecast.get("high_risk_cells", []):
            label = f"lat {cell['lat']}, lon {cell['lon']}"
            prob = cell.get("probability", 0)
            if prob >= 0.2:
                signal = f"Our model ({prob:.0%} storm probability)"
                location_signals.setdefault(label, []).append(signal)

    if alerts:
        for source_data in alerts.get("sources", []):
            source_name = source_data.get("source", "unknown")
            for loc in source_data.get("locations", []):
                if "error" in loc:
                    continue
                name = loc.get("name", "?")
                flagged = False
                peak_time = None

                if source_name == "open_meteo":
                    cape = loc.get("peak_cape_jkg", 0) or 0
                    peak_time = loc.get("peak_cape_time")
                    if cape >= 500 and is_within_24h(peak_time, now):
                        signal = f"Open-Meteo (CAPE {int(cape)} J/kg at {peak_time})"
                        flagged = True

                elif source_name == "visual_crossing":
                    risk = loc.get("severerisk_max", 0) or 0
                    peak_time = loc.get("severerisk_peak_time")
                    if risk >= 30 and is_within_24h(peak_time, now):
                        signal = f"Visual Crossing (severe risk {int(risk)}/100 at {peak_time})"
                        flagged = True

                elif source_name == "tomorrow_io":
                    prob = loc.get("peak_thunderstorm_prob_pct", 0) or 0
                    peak_time = loc.get("peak_time")
                    if prob >= 30 and is_within_24h(peak_time, now):
                        signal = f"Tomorrow.io ({int(prob)}% thunderstorm at {peak_time})"
                        flagged = True

                elif source_name == "openweathermap":
                    if loc.get("has_thunderstorm"):
                        signal = "OpenWeatherMap (thunderstorm conditions)"
                        flagged = True

                if flagged:
                    location_signals.setdefault(name, []).append(signal)

    if not location_signals:
        return ""

    lines = []
    for location, signals in sorted(location_signals.items(), key=lambda x: -len(x[1])):
        agreement = f"{len(signals)}/{len(signals)} sources" if len(signals) > 1 else "1 source"
        lines.append(f"  {location} ({agreement}): {'; '.join(signals)}")

    return "\n".join(lines)


def build_prompt(forecast: dict | None, alerts: dict | None) -> str:
    """Build the LLM prompt with all available data."""
    now = datetime.now(timezone.utc)
    parts = []
    parts.append(
        "You are a concise weather briefing assistant for a cyclist in Switzerland. "
        "Summarize the severe weather risk for the NEXT 24 HOURS ONLY "
        f"(from {now.strftime('%H:%M UTC %d %b')} to {(now + timedelta(hours=24)).strftime('%H:%M UTC %d %b')}). "
        "For each at-risk location, state: the city, the time window, "
        "and NAME the specific sources that agree (e.g. 'Our model + Open-Meteo + Tomorrow.io agree'). "
        "Briefly mention WHY (physical drivers). "
        "End with a clear ride/don't-ride recommendation. "
        "Keep under 500 characters. Use plain language, no jargon."
    )

    source_agreement = build_source_agreement(forecast, alerts)
    if source_agreement:
        parts.append(f"\n\nSOURCE AGREEMENT (locations where multiple sources flag risk, next 24h):\n{source_agreement}")

    if forecast:
        explanations = format_explanations(forecast)
        if explanations:
            parts.append(f"\n\nMODEL EXPLANATION (physical drivers of elevated risk):\n{explanations}")
        parts.append(f"\n\nFORECAST SUMMARY: max_probability={forecast.get('max_probability', 0):.1%}, "
                     f"cells_above_30pct={forecast.get('cells_above_30pct', 0)}, "
                     f"cells_above_50pct={forecast.get('cells_above_50pct', 0)}")

    if alerts:
        overall = alerts.get("overall", {})
        parts.append(f"\n\nOVERALL EXTERNAL RISK: {overall.get('overall_risk', '?')} "
                     f"(confidence: {overall.get('confidence', '?')}, "
                     f"sources: {overall.get('sources_ok', 0)}/{overall.get('sources_queried', 0)})")

    if not forecast and not alerts:
        parts.append("\n\nNo forecast or alert data available. Give a generic advisory.")

    return "\n".join(parts)


def generate_summary(prompt: str) -> str:
    """Call Amazon Nova Micro via Bedrock to generate the SMS summary."""
    response = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"]


def send_sms(message: str) -> None:
    """Send SMS via SNS."""
    if PHONE_NUMBER:
        resp = sns.publish(
            PhoneNumber=PHONE_NUMBER,
            Message=message,
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {
                    "DataType": "String",
                    "StringValue": "Transactional",
                },
            },
        )
        logger.info("SMS sent to %s (MessageId: %s)", PHONE_NUMBER, resp["MessageId"])
    elif SNS_TOPIC_ARN:
        resp = sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=message,
            Subject="Storm Briefing",
        )
        logger.info("Published to SNS topic (MessageId: %s)", resp["MessageId"])
    else:
        logger.warning("No PHONE_NUMBER or SNS_TOPIC_ARN set - printing summary only")
        print(f"\n{'='*50}\nDAILY BRIEFING:\n{message}\n{'='*50}")


def main() -> None:
    logger.info("Daily briefing starting")
    logger.info("  S3 bucket: %s", S3_BUCKET)
    logger.info("  Phone: %s", PHONE_NUMBER[:6] + "***" if PHONE_NUMBER else "not set")

    forecast = find_latest_forecast()
    alerts = find_latest_alerts()

    if forecast:
        logger.info("Forecast loaded: max_prob=%.2f, %d high-risk cells",
                    forecast.get("max_probability", 0),
                    len(forecast.get("high_risk_cells", [])))
    else:
        logger.warning("No forecast data found")

    if alerts:
        overall = alerts.get("overall", {})
        logger.info("Alerts loaded: overall_risk=%s, sources=%d/%d",
                    overall.get("overall_risk", "?"),
                    overall.get("sources_ok", 0),
                    overall.get("sources_queried", 0))
    else:
        logger.warning("No alerts data found")

    prompt = build_prompt(forecast, alerts)
    logger.info("Generating summary with Bedrock (%s)...", BEDROCK_MODEL_ID)
    summary = generate_summary(prompt)
    logger.info("Summary: %s", summary)

    # Save briefing to S3
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    briefing = {
        "generated_at": now.isoformat(),
        "summary": summary,
        "forecast_available": forecast is not None,
        "alerts_available": alerts is not None,
    }
    key = f"briefings/{date_str}/briefing.json"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(briefing, indent=2).encode(),
        ContentType="application/json",
    )
    logger.info("Saved briefing to s3://%s/%s", S3_BUCKET, key)

    send_sms(summary)
    logger.info("Daily briefing complete")


if __name__ == "__main__":
    main()
