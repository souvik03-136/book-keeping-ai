"""
Celery Tasks
============
All long-running work is offloaded here so the Flask API stays non-blocking.

Tasks
-----
run_forecast_task   — SARIMA + LSTM ensemble, generates plot, returns JSON
check_all_alerts    — Periodic task (Celery Beat) that scans alert configs and
                      sends notifications when predicted demand exceeds stock.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime

from celery import Celery
from celery.schedules import crontab

from forecasting import predict_demand, check_stock_and_alert
from bokeh_forecast import create_bokeh_plots
from utils import load_data

logger = logging.getLogger("demand_forecast.tasks")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
UPLOAD_FOLDER = Path(os.getenv("UPLOAD_FOLDER", "uploads"))

# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------

celery_app = Celery(
    "demand_forecast",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,           # Only ack after success/failure
    worker_prefetch_multiplier=1,  # Prevent memory overload on large models
    result_expires=86400,          # Keep results for 24 h
    # --------------- Scheduled tasks (Celery Beat) --------------------------
    beat_schedule={
        "check-all-alerts-daily": {
            "task": "tasks.check_all_alerts",
            "schedule": crontab(hour=8, minute=0),  # 08:00 UTC every day
        },
        "check-all-alerts-hourly-debug": {
            # Only active in non-production; remove or disable in prod
            "task": "tasks.check_all_alerts",
            "schedule": crontab(minute=0) if os.getenv("ENV") == "development" else None,
        },
    },
)


# ---------------------------------------------------------------------------
# Forecast task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="tasks.run_forecast_task",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=300,   # 5 min — raises SoftTimeLimitExceeded
    time_limit=360,        # 6 min hard kill
)
def run_forecast_task(self, data_path: str, item_id: str, horizon_months: int = 6):
    """
    Run the demand forecast pipeline for a single item.

    Progress states reported via Celery meta:
      0%  – started
      30% – data loaded and validated
      60% – SARIMA fitted
      90% – LSTM fitted, ensemble computed
      100% – plot generated

    Returns a JSON-serialisable dict on success.
    """
    try:
        self.update_state(state="STARTED", meta={"progress": 0, "item_id": item_id})
        logger.info("Forecast started | task_id=%s item_id=%s", self.request.id, item_id)

        # --- 1. Load & validate data ---
        df = load_data(data_path)
        if item_id not in df["item_id"].values:
            raise ValueError(f"Item ID '{item_id}' not found in dataset.")
        self.update_state(state="STARTED", meta={"progress": 30, "item_id": item_id})

        # --- 2. Run ensemble forecast ---
        future_dates, predicted_demand = predict_demand(df, item_id, horizon_months)
        self.update_state(state="STARTED", meta={"progress": 80, "item_id": item_id})

        if predicted_demand is None:
            raise ValueError(f"Could not generate a forecast for item '{item_id}'.")

        # --- 3. Evaluate alerts ---
        alerts = check_stock_and_alert(df, item_id, predicted_demand, future_dates)

        # --- 4. Generate plot ---
        plot_path = create_bokeh_plots(df, item_id, future_dates, predicted_demand, UPLOAD_FOLDER)
        self.update_state(state="STARTED", meta={"progress": 95, "item_id": item_id})

        result = {
            "item_id": item_id,
            "horizon_months": horizon_months,
            "future_months": [d.strftime("%Y-%m") for d in future_dates],
            "predicted_demand": [round(float(v), 2) for v in predicted_demand],
            "alerts": alerts,
            "plot_url": f"/api/v1/plots/{item_id}",
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        logger.info("Forecast complete | task_id=%s item_id=%s", self.request.id, item_id)
        return result

    except Exception as exc:
        logger.exception("Forecast failed | task_id=%s item_id=%s", self.request.id, item_id)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Scheduled alert checker (runs via Celery Beat)
# ---------------------------------------------------------------------------

@celery_app.task(
    name="tasks.check_all_alerts",
    soft_time_limit=600,
    time_limit=660,
)
def check_all_alerts():
    """
    Evaluate every saved alert configuration.

    For each configured item:
    1. Locate the most recently uploaded dataset for that item.
    2. Re-run forecast.
    3. If predicted demand exceeds the configured threshold + current stock,
       emit a structured alert (log + optional email stub).

    This task is scheduled daily via Celery Beat.
    """
    alert_file = UPLOAD_FOLDER / "alert_configs.json"
    if not alert_file.exists():
        logger.info("No alert configs found; skipping scheduled check.")
        return {"checked": 0}

    with open(alert_file) as f:
        configs = json.load(f)

    # Find the latest uploaded dataset
    csv_files = sorted(UPLOAD_FOLDER.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    xlsx_files = sorted(UPLOAD_FOLDER.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    all_uploads = csv_files + xlsx_files
    if not all_uploads:
        logger.warning("No uploaded dataset found; skipping alert evaluation.")
        return {"checked": 0, "reason": "no_dataset"}

    latest_dataset = all_uploads[0]
    df = load_data(str(latest_dataset))

    triggered = []
    for item_id, config in configs.items():
        try:
            if item_id not in df["item_id"].values:
                logger.warning("Alert item_id=%s not in dataset; skipping.", item_id)
                continue

            _, predicted_demand = predict_demand(df, item_id)
            alerts = check_stock_and_alert(
                df, item_id, predicted_demand, [],
                threshold=config.get("low_stock_threshold", 0.0),
            )
            reorder_alerts = [a for a in alerts if "reorder" in a.lower()]
            if reorder_alerts:
                _emit_alert(item_id, reorder_alerts, config.get("notify_email"))
                triggered.append(item_id)
        except Exception:
            logger.exception("Alert check failed for item_id=%s", item_id)

    logger.info("Scheduled alert check done | triggered=%s", triggered)
    return {"checked": len(configs), "triggered": triggered}


def _emit_alert(item_id: str, alerts: list, email: str | None):
    """
    Emit an alert. In production, swap the logger call for your
    notification provider (SendGrid, SNS, PagerDuty, Slack webhook, etc.)
    """
    for msg in alerts:
        logger.warning("[ALERT] item_id=%s | %s | notify=%s", item_id, msg, email or "none")

    if email:
        # TODO: integrate with your email provider
        # e.g. sendgrid_client.send(to=email, subject=..., body=...)
        logger.info("Email notification stub fired | to=%s item_id=%s", email, item_id)