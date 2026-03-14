# book-keeping-ai/tests/test_entity_detection.py

"""
Tests — Entity Detection Service
=================================
Run with:  pytest tests/ -v
"""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def client(monkeypatch):
    # Set ENV=test so entity_detection/app.py uses memory:// storage instead
    # of connecting to Redis, which prevents @limiter.limit from breaking routes.
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-key-for-tests")

    # Evict any previously cached module so demand_forecast/app.py (which
    # shares the flat name "app") never bleeds into this fixture.
    for mod in list(sys.modules.keys()):
        if mod in {"app", "entity_app", "entity_detection.app"}:
            del sys.modules[mod]

    _root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "entity_app",
        _root / "entity_detection" / "app.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flask_app = module.app

    flask_app.config["TESTING"] = True
    flask_app.config["RATELIMIT_ENABLED"] = False  # belt-and-suspenders
    with flask_app.test_client() as c:
        yield c


HEADERS = {"X-API-Key": "test-key", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /extract/entities (rule-based — no LLM dependency)
# ---------------------------------------------------------------------------

def test_less_than(client):
    r = client.post(
        "/api/v1/extract/entities",
        json={"text": "apples less than 50 rs"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["action"] == "less"
    assert body["object"] == "apples"
    assert body["range"] == "50"


def test_more_than(client):
    r = client.post(
        "/api/v1/extract/entities",
        json={"text": "oranges more than 100"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["action"] == "more"
    assert body["object"] == "oranges"
    assert body["range"] == "100"


def test_range_query(client):
    r = client.post(
        "/api/v1/extract/entities",
        json={"text": "mangoes more than 10 less than 80"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["action"] == "range"
    assert body["min"] == "10"
    assert body["max"] == "80"


def test_invalid_query(client):
    r = client.post(
        "/api/v1/extract/entities",
        json={"text": "hello world"},
        headers=HEADERS,
    )
    assert r.status_code == 422


def test_empty_text_rejected(client):
    r = client.post(
        "/api/v1/extract/entities",
        json={"text": ""},
        headers=HEADERS,
    )
    assert r.status_code == 422


def test_too_long_text_rejected(client):
    r = client.post(
        "/api/v1/extract/entities",
        json={"text": "x" * 501},
        headers=HEADERS,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /extract (NLP backend — avoids LLM network call)
# ---------------------------------------------------------------------------

def test_nlp_extraction(client):
    r = client.post(
        "/api/v1/extract",
        json={"text": "John Doe bought 2 apples for $5", "backend": "nlp"},
        headers=HEADERS,
    )
    # NLP may or may not find all fields — just confirm no crash
    assert r.status_code in {200, 422}


def test_requires_auth(client):
    r = client.post("/api/v1/extract", json={"text": "test"})
    assert r.status_code == 401


def test_invalid_backend(client):
    r = client.post(
        "/api/v1/extract",
        json={"text": "John Doe bought 2 apples for $5", "backend": "gpt99"},
        headers=HEADERS,
    )
    assert r.status_code == 422