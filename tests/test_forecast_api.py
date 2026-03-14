# book-keeping-ai/tests/test_forecast_api.py

"""
Tests — Demand Forecast Service
================================
Run with:  pytest tests/ -v
"""

import csv
import importlib.util
import io
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Set ENV=test so demand_forecast/app.py uses memory:// rate-limit storage
    # instead of trying to connect to Redis, which isn't running in CI.
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    # Evict any previously cached module to avoid cross-contamination with
    # entity_detection/app.py, which has the same flat module name "app".
    for mod in list(sys.modules.keys()):
        if mod in {"app", "forecast_app", "demand_forecast.app"}:
            del sys.modules[mod]

    _root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "forecast_app",
        _root / "demand_forecast" / "app.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flask_app = module.app

    flask_app.config["TESTING"] = True
    flask_app.config["RATELIMIT_ENABLED"] = False  # belt-and-suspenders
    with flask_app.test_client() as c:
        yield c


HEADERS = {"X-API-Key": "test-key", "Content-Type": "application/json"}


def _make_csv(rows: list[dict]) -> io.BytesIO:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["transaction_date", "item_id", "quantity"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return io.BytesIO(buf.getvalue().encode())


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def test_upload_valid_csv(client, tmp_path):
    data = _make_csv([
        {"transaction_date": "2023-01-15", "item_id": "A001", "quantity": 10},
        {"transaction_date": "2023-02-15", "item_id": "A001", "quantity": 20},
    ])
    r = client.post(
        "/api/v1/upload",
        data={"file": (data, "sales.csv")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 201
    assert "file_id" in r.get_json()


def test_upload_no_file(client):
    r = client.post(
        "/api/v1/upload",
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 400


def test_upload_invalid_extension(client):
    r = client.post(
        "/api/v1/upload",
        data={"file": (io.BytesIO(b"data"), "bad.txt")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 400


def test_upload_requires_auth(client):
    data = _make_csv([
        {"transaction_date": "2023-01-15", "item_id": "A001", "quantity": 5},
    ])
    r = client.post(
        "/api/v1/upload",
        data={"file": (data, "sales.csv")},
        content_type="multipart/form-data",
    )
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
    """
    Upload a valid CSV then request a forecast for a non-existent item.
    The task enqueue itself may raise (→ 500) or the schema may reject
    before enqueue — any 2xx/4xx response is acceptable.
    """
    data = _make_csv([
        {"transaction_date": "2023-01-15", "item_id": "A001", "quantity": 10},
    ])
    up = client.post(
        "/api/v1/upload",
        data={"file": (data, "sales.csv")},
        content_type="multipart/form-data",
        headers={"X-API-Key": "test-key"},
    )
    file_id = up.get_json()["file_id"]

    r = client.post(
        "/api/v1/forecast",
        json={"item_id": "NONEXISTENT", "file_id": file_id},
        headers=HEADERS,
    )
    # 202 queued; 400/404 pre-enqueue validation; 422 schema rejection
    assert r.status_code in {202, 400, 404, 422}


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
        json={
            "item_id": "A001",
            "low_stock_threshold": 10.0,
            "notify_email": "ops@example.com",
        },
        headers=HEADERS,
    )
    assert r.status_code == 201


def test_configure_alert_invalid_email(client):
    r = client.post(
        "/api/v1/alerts",
        json={"item_id": "A001", "notify_email": "not-an-email"},
        headers=HEADERS,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Utils (import directly from the service directory via conftest sys.path)
# ---------------------------------------------------------------------------

def test_sanitize_item_id():
    from utils import sanitize_item_id
    assert sanitize_item_id("A001") == "A001"
    assert sanitize_item_id("../../etc/passwd") == "______etc_passwd"
    assert sanitize_item_id("item id with spaces") == "item_id_with_spaces"


def test_load_data_missing_columns(tmp_path):
    import pandas as pd
    from utils import load_data
    bad = tmp_path / "bad.csv"
    pd.DataFrame({"date": ["2023-01-01"], "qty": [5]}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="missing required columns"):
        load_data(str(bad))


def test_load_data_valid(tmp_path):
    import pandas as pd
    from utils import load_data
    good = tmp_path / "good.csv"
    pd.DataFrame({
        "transaction_date": ["2023-01-15", "2023-02-15"],
        "item_id": ["A001", "A001"],
        "quantity": [10, 20],
    }).to_csv(good, index=False)
    df = load_data(str(good))
    assert len(df) == 2
    assert df["quantity"].sum() == 30