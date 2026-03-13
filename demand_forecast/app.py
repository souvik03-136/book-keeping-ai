"""
Demand Forecasting Service - Production Grade
=============================================
Async forecast pipeline with Celery + Redis, proper validation,
structured logging, and per-user file isolation.
"""

import os
import logging
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from marshmallow import Schema, fields, ValidationError
from celery.result import AsyncResult

from tasks import celery_app, run_forecast_task
from utils import allowed_file, get_upload_path, sanitize_item_id

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.getenv("ALLOWED_ORIGINS", "*").split(",")}})

UPLOAD_FOLDER = Path(os.getenv("UPLOAD_FOLDER", "uploads"))
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", 16)) * 1024 * 1024
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me-in-production")

# Rate limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=os.getenv("REDIS_URL", "redis://redis:6379/0"),
)

# Structured logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("demand_forecast")


# ---------------------------------------------------------------------------
# Request / response schemas (marshmallow)
# ---------------------------------------------------------------------------

class ForecastRequestSchema(Schema):
    item_id = fields.Str(required=True)
    horizon_months = fields.Int(load_default=6, validate=lambda n: 1 <= n <= 24)


class AlertConfigSchema(Schema):
    item_id = fields.Str(required=True)
    low_stock_threshold = fields.Float(load_default=0.0)
    notify_email = fields.Email(load_default=None, allow_none=True)


# ---------------------------------------------------------------------------
# Auth stub (replace with JWT / API-key middleware in production)
# ---------------------------------------------------------------------------

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key")
        expected = os.getenv("API_KEY")
        if expected and api_key != expected:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "demand-forecast"}), 200


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.route("/api/v1/upload", methods=["POST"])
@require_api_key
@limiter.limit("10 per minute")
def upload_file():
    """
    Upload a CSV or XLSX dataset.

    Stores the file under a stable, sanitised filename.
    Returns a ``file_id`` that must be passed to ``/forecast``.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Only .csv and .xlsx files are allowed"}), 415

    try:
        file_id, dest = get_upload_path(UPLOAD_FOLDER, file.filename)
        file.save(dest)
        logger.info("File uploaded | file_id=%s path=%s", file_id, dest)
        return jsonify({"message": "File uploaded successfully", "file_id": file_id}), 201
    except Exception:
        logger.exception("File upload failed")
        return jsonify({"error": "Failed to save file"}), 500


# ---------------------------------------------------------------------------
# Forecast (async via Celery)
# ---------------------------------------------------------------------------

@app.route("/api/v1/forecast", methods=["POST"])
@require_api_key
@limiter.limit("20 per hour")
def forecast():
    """
    Enqueue a forecast job.

    Returns a ``task_id`` immediately; poll ``/api/v1/tasks/<task_id>``
    for the result.
    """
    schema = ForecastRequestSchema()
    try:
        data = schema.load(request.get_json(force=True) or {})
    except ValidationError as err:
        return jsonify({"error": "Invalid request", "details": err.messages}), 422

    file_id = request.args.get("file_id") or (request.get_json(force=True) or {}).get("file_id")
    if not file_id:
        return jsonify({"error": "file_id is required (upload a file first)"}), 400

    upload_path = UPLOAD_FOLDER / f"{file_id}"
    if not upload_path.exists():
        # Try to find by prefix (extension may differ)
        matches = list(UPLOAD_FOLDER.glob(f"{file_id}*"))
        if not matches:
            return jsonify({"error": f"file_id '{file_id}' not found. Please upload first."}), 404
        upload_path = matches[0]

    item_id = sanitize_item_id(data["item_id"])
    horizon = data["horizon_months"]

    task = run_forecast_task.delay(str(upload_path), item_id, horizon)
    logger.info("Forecast enqueued | task_id=%s item_id=%s", task.id, item_id)

    return jsonify({
        "task_id": task.id,
        "status": "queued",
        "poll_url": f"/api/v1/tasks/{task.id}",
    }), 202


# ---------------------------------------------------------------------------
# Task status polling
# ---------------------------------------------------------------------------

@app.route("/api/v1/tasks/<task_id>", methods=["GET"])
@require_api_key
def task_status(task_id: str):
    """Poll the status / result of an async forecast task."""
    result: AsyncResult = celery_app.AsyncResult(task_id)

    if result.state == "PENDING":
        return jsonify({"task_id": task_id, "status": "pending"}), 202

    if result.state == "STARTED":
        meta = result.info or {}
        return jsonify({"task_id": task_id, "status": "running", "progress": meta.get("progress", 0)}), 202

    if result.state == "SUCCESS":
        return jsonify({"task_id": task_id, "status": "success", "result": result.result}), 200

    if result.state == "FAILURE":
        return jsonify({"task_id": task_id, "status": "failed", "error": str(result.result)}), 500

    return jsonify({"task_id": task_id, "status": result.state}), 200


# ---------------------------------------------------------------------------
# Plot retrieval
# ---------------------------------------------------------------------------

@app.route("/api/v1/plots/<item_id>", methods=["GET"])
@require_api_key
def get_plot(item_id: str):
    """Return the pre-generated Bokeh HTML chart for a given item."""
    item_id = sanitize_item_id(item_id)
    plot_path = UPLOAD_FOLDER / f"forecast_{item_id}.html"
    if not plot_path.exists():
        return jsonify({"error": "Plot not found. Run a forecast first."}), 404
    return send_file(plot_path, mimetype="text/html")


# ---------------------------------------------------------------------------
# Alert configuration (persisted to Redis / DB stub)
# ---------------------------------------------------------------------------

@app.route("/api/v1/alerts", methods=["POST"])
@require_api_key
@limiter.limit("30 per hour")
def configure_alert():
    """
    Persist a low-stock alert configuration for an item.

    The scheduler (Celery Beat) will evaluate these daily.
    """
    schema = AlertConfigSchema()
    try:
        data = schema.load(request.get_json(force=True) or {})
    except ValidationError as err:
        return jsonify({"error": "Invalid request", "details": err.messages}), 422

    # In a real system this would write to Postgres/Redis.
    # For now, persist to a simple JSON file as a reference implementation.
    import json
    alert_file = UPLOAD_FOLDER / "alert_configs.json"
    configs = {}
    if alert_file.exists():
        with open(alert_file) as f:
            configs = json.load(f)
    configs[data["item_id"]] = {
        "low_stock_threshold": data["low_stock_threshold"],
        "notify_email": data["notify_email"],
    }
    with open(alert_file, "w") as f:
        json.dump(configs, f, indent=2)

    logger.info("Alert configured | item_id=%s", data["item_id"])
    return jsonify({"message": "Alert configuration saved", "item_id": data["item_id"]}), 201


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def request_entity_too_large(_):
    mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"File too large. Maximum size is {mb} MB."}), 413


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"error": "Rate limit exceeded", "retry_after": str(e.description)}), 429


@app.errorhandler(500)
def internal_error(_):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)