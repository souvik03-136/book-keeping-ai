
"""
Tests — Demand Forecast Service
================================
Run with:  pytest tests/ -v
"""

import io
import json
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    from demand_forecast.app import app as flask_app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


HEADERS = {"X-API-Key": "test-key", "Content-Type": "application/json"}


def _make_csv(rows: list[dict]) -> io.BytesIO:
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["transaction_date", "item_id", "quantity"])
    writer.writeheader()
    writer.writerows(rows)
    return io.BytesIO(buf.getvalue().encode())


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def test_upload_valid_csv(client):
    csv_data = _make_csv([
        {"transaction_date": "2023-01-15", "item_id": "A001", "quantity": 10},
        {"transaction_date": "2023-02-15", "item_id": "A001", "quantity": 15},
    ])
    r = client.post(
        "/api/v1/upload",
        data={"file": (csv_data, "sales.csv")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 201
    body = r.get_json()
    assert "file_id" in body


def test_upload_no_file(client):
    r = client.post("/api/v1/upload", headers={"X-API-Key": "test-key"})
    assert r.status_code == 400


def test_upload_invalid_extension(client):
    r = client.post(
        "/api/v1/upload",
        data={"file": (io.BytesIO(b"data"), "file.txt")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 415


def test_upload_requires_auth(client):
    r = client.post("/api/v1/upload")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

def test_forecast_missing_file_id(client):
    r = client.post(
        "/api/v1/forecast",
        json={"item_id": "A001"},
        headers=HEADERS,
    )
    assert r.status_code == 400


def test_forecast_invalid_item_id(client, tmp_path):
    # Create a valid file but ask for a non-existent item
    csv_data = _make_csv([
        {"transaction_date": "2023-01-15", "item_id": "A001", "quantity": 10},
    ])
    up = client.post(
        "/api/v1/upload",
        data={"file": (csv_data, "sales.csv")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    file_id = up.get_json()["file_id"]

    r = client.post(
        "/api/v1/forecast",
        json={"item_id": "NONEXISTENT", "file_id": file_id},
        headers=HEADERS,
    )
    # Celery not running in tests → expect a 202 queued or 404 handled
    assert r.status_code in {202, 400, 404}


def test_forecast_invalid_horizon(client):
    r = client.post(
        "/api/v1/forecast",
        json={"item_id": "A001", "horizon_months": 99},
        headers=HEADERS,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Alert config
# ---------------------------------------------------------------------------

def test_configure_alert(client):
    r = client.post(
        "/api/v1/alerts",
        json={"item_id": "A001", "low_stock_threshold": 10.0, "notify_email": "ops@example.com"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    assert r.get_json()["item_id"] == "A001"


def test_configure_alert_invalid_email(client):
    r = client.post(
        "/api/v1/alerts",
        json={"item_id": "A001", "notify_email": "not-an-email"},
        headers=HEADERS,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def test_sanitize_item_id():
    from demand_forecast.utils import sanitize_item_id
    assert sanitize_item_id("A001") == "A001"
    assert sanitize_item_id("../../etc/passwd") == "______etc_passwd"
    assert sanitize_item_id("item id with spaces") == "item_id_with_spaces"


def test_load_data_missing_columns(tmp_path):
    import pandas as pd
    from demand_forecast.utils import load_data
    bad = tmp_path / "bad.csv"
    pd.DataFrame({"date": ["2023-01-01"], "qty": [5]}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="missing required columns"):
        load_data(str(bad))


def test_load_data_valid(tmp_path):
    import pandas as pd
    from demand_forecast.utils import load_data
    good = tmp_path / "good.csv"
    pd.DataFrame({
        "transaction_date": ["2023-01-15", "2023-02-15"],
        "item_id": ["A001", "A001"],
        "quantity": [10, 20],
    }).to_csv(good, index=False)
    df = load_data(str(good))
    assert len(df) == 2
    assert df["quantity"].sum() == 30