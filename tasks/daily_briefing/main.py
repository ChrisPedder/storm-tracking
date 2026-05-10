"""Generate a daily severe weather briefing and send via SMS.

Reads model forecast and multi-source weather alerts from S3,
summarizes with Claude Haiku for cycling safety, sends via AWS SNS.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import anthropic
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

s3 = boto3.client("s3")
sns = boto3.client("sns")


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


def build_prompt(forecast: dict | None, alerts: dict | None) -> str:
    """Build the LLM prompt with all available data."""
    parts = []
    parts.append(
        "You are a concise weather briefing assistant for a cyclist in Switzerland. "
        "Summarize the severe weather risk for today and tomorrow. "
        "Be specific about WHERE (regions/cities) and WHEN (time windows) thunderstorms are likely. "
        "Give a clear recommendation: safe to ride, avoid certain times/areas, or stay home. "
        "Keep it under 300 characters for SMS. Use plain language, no jargon."
    )

    if forecast:
        parts.append(f"\n\nMODEL FORECAST (our trained severe storm model):\n{json.dumps(forecast, indent=2)}")

    if alerts:
        parts.append(f"\n\nEXTERNAL WEATHER APIS:\n{json.dumps(alerts, indent=2)}")

    if not forecast and not alerts:
        parts.append("\n\nNo forecast or alert data available. Give a generic advisory.")

    return "\n".join(parts)


def generate_summary(prompt: str) -> str:
    """Call Claude Haiku to generate the SMS summary."""
    if not ANTHROPIC_API_KEY:
        logger.error("No ANTHROPIC_API_KEY set")
        return "Weather briefing unavailable - no API key configured."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


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
    logger.info("Generating summary with Claude Haiku...")
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
