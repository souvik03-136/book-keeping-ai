
"""
Tests — Entity Detection Service
=================================
Run with:  pytest tests/ -v
"""

import pytest


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-key-for-tests")

    from entity_detection.app import app as flask_app
    flask_app.config["TESTING"] = True
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
    r = client.post("/api/v1/extract/entities", json={"text": "apples less than 50 rs"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.get_json()
    assert body["action"] == "less"
    assert body["object"] == "apples"
    assert body["range"] == "50"


def test_more_than(client):
    r = client.post("/api/v1/extract/entities", json={"text": "oranges more than 100"}, headers=HEADERS)
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
    r = client.post("/api/v1/extract/entities", json={"text": "hello world"}, headers=HEADERS)
    assert r.status_code == 422


def test_empty_text_rejected(client):
    r = client.post("/api/v1/extract/entities", json={"text": ""}, headers=HEADERS)
    assert r.status_code == 422


def test_too_long_text_rejected(client):
    r = client.post("/api/v1/extract/entities", json={"text": "x" * 501}, headers=HEADERS)
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
    # NLP may or may not get all fields — just check it doesn't 500
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