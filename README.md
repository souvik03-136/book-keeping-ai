<p align="center">
  <img width="380" src="https://user-images.githubusercontent.com/56252312/159312411-58410727-3933-4224-b43e-4e9b627838a3.png#gh-light-mode-only" alt="GDSC VIT"/>
</p>

<h2 align="center">Book-Keeping AI</h2>
<h4 align="center">Production-grade AI bookkeeping system — async demand forecasting, entity extraction, scheduled alerts.</h4>

<p align="center">
  <a href="https://dsc.community.dev/vellore-institute-of-technology/"><img src="https://img.shields.io/badge/Join%20Us-Developer%20Student%20Clubs-red" alt="DSC VIT"/></a>
  <a href="https://discord.gg/498KVdSKWR"><img src="https://img.shields.io/discord/760928671698649098.svg" alt="Discord"/></a>
  <img src="https://img.shields.io/badge/python-3.11-blue" alt="Python 3.11"/>
  <img src="https://img.shields.io/badge/Flask-3.0-lightgrey" alt="Flask"/>
  <img src="https://img.shields.io/badge/Celery-5.4-green" alt="Celery"/>
  <img src="https://img.shields.io/badge/Redis-7-red" alt="Redis"/>
  <img src="https://img.shields.io/badge/docker--compose-ready-blue" alt="Docker Compose"/>
</p>

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Services](#services)
- [Quick Start](#quick-start)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Running Tests](#running-tests)
- [Deployment](#deployment)
- [Contributors](#contributors)

---

## Overview

Book-Keeping AI automates two core operations for small and medium businesses:

1. **Transaction Entity Extraction** — parse natural-language transaction descriptions (e.g., *"John Doe bought 2 apples for $5"*) into structured records (customer, item, quantity, price).
2. **Demand Forecasting** — upload historical sales data and receive an AI-generated 6-month demand forecast with low-stock alerts, delivered asynchronously via a Celery task queue.

### What changed in this production rewrite

| Area | Before | After |
|---|---|---|
| Forecast execution | Synchronous — request blocks until SARIMA+LSTM finishes (minutes) | Async Celery task; API returns `202 Accepted` + `task_id` immediately |
| Alerts | All fire at once on-demand | Celery Beat scheduler runs daily at 08:00 UTC; per-item email config |
| Global state | `data_file_path` global variable (thread-unsafe) | File-ID system, each upload gets a unique hash-based ID |
| Input validation | `data.get(...)` with no schema | marshmallow schemas on every endpoint, typed error messages |
| Rate limiting | None | flask-limiter backed by Redis |
| Auth | None | `X-API-Key` header middleware (swap for JWT in production) |
| Entity detection | Two separate v1/v2 apps | Single unified service, `backend` param selects LLM / NLP / auto |
| Tests | None | pytest suite covering upload, forecast, alert config, entity extraction |
| Logging | `print` / basic Flask logger | Structured `%(asctime)s [%(levelname)s]` to stdout (JSON-ready) |
| Model selection | Fixed 70/30 SARIMA/LSTM weights | Dynamic weighting via hold-out MAPE — better model gets more influence |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client / Frontend                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP
                     ┌──────▼──────┐
                     │    Nginx    │  Port 80 / 443
                     │  (reverse   │  TLS termination
                     │   proxy)    │  Rate limiting (L7)
                     └──┬───────┬──┘
                        │       │
            ┌───────────▼─┐   ┌─▼─────────────┐
            │ Forecast API│   │  Entity API   │
            │  Flask:5000 │   │  Flask:5001   │
            └──────┬──────┘   └───────────────┘
                   │ enqueue
            ┌──────▼──────┐
            │    Redis    │  Broker + Result backend
            │   :6379     │  Rate-limit storage
            └──┬───────┬──┘
               │       │
     ┌─────────▼─┐   ┌─▼──────────┐
     │  Celery   │   │  Celery    │
     │  Worker   │   │  Beat      │
     │ (forecast │   │ (scheduler │
     │   jobs)   │   │  08:00 UTC)│
     └───────────┘   └────────────┘
```

### Request lifecycle — Forecast

```
Client
  │
  ├─ POST /api/v1/upload  (multipart CSV/XLSX)
  │    └─ returns { file_id }
  │
  ├─ POST /api/v1/forecast  { item_id, file_id, horizon_months }
  │    └─ enqueues Celery task → returns { task_id, poll_url }
  │
  └─ GET  /api/v1/tasks/{task_id}   (poll until status == "success")
       └─ returns { future_months[], predicted_demand[], alerts[], plot_url }
```

### Scheduled alert lifecycle

```
Celery Beat (08:00 UTC daily)
  └─ check_all_alerts task
       ├─ loads alert_configs.json
       ├─ finds most recent uploaded dataset
       ├─ runs predict_demand for each configured item
       └─ for each item where demand > stock + threshold:
            ├─ logs [ALERT] warning
            └─ (stub) sends email via provider integration
```

---

## Services

| Service | Port | Purpose |
|---|---|---|
| `forecast-api` | 5000 | Demand forecasting REST API |
| `entity-api` | 5001 | Entity extraction REST API |
| `forecast-worker` | — | Celery worker (forecast jobs) |
| `forecast-beat` | — | Celery Beat (daily alert scheduler) |
| `redis` | 6379 | Broker, result backend, rate-limit store |
| `nginx` | 80/443 | Reverse proxy, TLS |

---

## Quick Start

### Prerequisites

- Docker ≥ 24 and Docker Compose ≥ 2.20
- A [Groq API key](https://console.groq.com/) for LLM-based entity extraction

### 1. Clone and configure

```bash
git clone https://github.com/GDSC-VIT/book-keeping-ai.git
cd book-keeping-ai
cp .env.example .env
# Fill in API_KEY, SECRET_KEY, and GROQ_API_KEY in .env
```

### 2. Start all services

```bash
docker compose up --build -d
```

### 3. Verify everything is healthy

```bash
docker compose ps
curl http://localhost:5000/health    # {"status":"ok","service":"demand-forecast"}
curl http://localhost:5001/health    # {"status":"ok","service":"entity-detection"}
```

---

## API Reference

All endpoints require the `X-API-Key` header.

### Demand Forecast API  (`localhost:5000`)

#### Upload a dataset

```http
POST /api/v1/upload
Content-Type: multipart/form-data
X-API-Key: <your-key>

file: <CSV or XLSX>
```

Your file **must** contain these columns:

| Column | Type | Example |
|---|---|---|
| `transaction_date` | ISO date | `2023-06-15` |
| `item_id` | string | `A001` |
| `quantity` | number | `42` |

**Response `201`**
```json
{ "message": "File uploaded successfully", "file_id": "a3f9c2e10b4d" }
```

---

#### Start a forecast

```http
POST /api/v1/forecast
Content-Type: application/json
X-API-Key: <your-key>

{
  "item_id": "A001",
  "file_id": "a3f9c2e10b4d",
  "horizon_months": 6
}
```

**Response `202`**
```json
{
  "task_id": "d3b07384-d9a1-...",
  "status": "queued",
  "poll_url": "/api/v1/tasks/d3b07384-d9a1-..."
}
```

---

#### Poll task status

```http
GET /api/v1/tasks/{task_id}
X-API-Key: <your-key>
```

**Response `200` (complete)**
```json
{
  "task_id": "d3b07384-d9a1-...",
  "status": "success",
  "result": {
    "item_id": "A001",
    "horizon_months": 6,
    "future_months": ["2024-07", "2024-08", "2024-09", "2024-10", "2024-11", "2024-12"],
    "predicted_demand": [120.5, 133.2, 128.7, 141.0, 156.3, 149.8],
    "alerts": [
      {
        "level": "warning",
        "period": "2024-09",
        "message": "Reorder 28 units of item 'A001' by 2024-09 ..."
      }
    ],
    "plot_url": "/api/v1/plots/A001",
    "generated_at": "2024-06-15T08:00:00Z"
  }
}
```

---

#### Configure a low-stock alert

```http
POST /api/v1/alerts
Content-Type: application/json
X-API-Key: <your-key>

{
  "item_id": "A001",
  "low_stock_threshold": 20.0,
  "notify_email": "ops@yourcompany.com"
}
```

**Response `201`**
```json
{ "message": "Alert configuration saved", "item_id": "A001" }
```

Alerts are evaluated daily at 08:00 UTC by the Celery Beat scheduler.

---

### Entity Detection API  (`localhost:5001`)

#### Extract transaction entities

```http
POST /api/v1/extract
Content-Type: application/json
X-API-Key: <your-key>

{
  "text": "John Doe bought 2 apples for $5",
  "backend": "auto"
}
```

`backend` options: `"auto"` (default — LLM, falls back to NLP), `"llm"`, `"nlp"`.

**Response `200`**
```json
{
  "CustomerName": "John Doe",
  "ItemName": "apples",
  "ItemQuantity": "2",
  "Price": "5",
  "_backend_used": "llm"
}
```

---

#### Parse an inventory query

```http
POST /api/v1/extract/entities
Content-Type: application/json
X-API-Key: <your-key>

{ "text": "apples less than 50 rs" }
```

**Response `200`**
```json
{ "object": "apples", "action": "less", "range": "50" }
```

Supported formats:

| Input | Response |
|---|---|
| `"item less than N"` | `{ action: "less", range: "N" }` |
| `"item more than N"` | `{ action: "more", range: "N" }` |
| `"item more than N less than M"` | `{ action: "range", min: "N", max: "M" }` |

---

## Configuration

All config is via environment variables. Copy `.env.example` to `.env`.

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `changeme` | Shared API key for all endpoints |
| `SECRET_KEY` | `changeme-secret` | Flask session secret |
| `GROQ_API_KEY` | — | Required for LLM entity extraction |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `UPLOAD_FOLDER` | `uploads` | Path for uploaded datasets |
| `MAX_UPLOAD_MB` | `32` | Maximum upload size in MB |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENV` | `production` | `development` enables hourly beat schedule |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins |

---

## Running Tests

```bash
# Install test deps
pip install pytest

# Run the full test suite
pytest tests/ -v

# Run only entity detection tests
pytest tests/test_entity_detection.py -v
```

---

## Deployment

### Production checklist

- [ ] Set `API_KEY`, `SECRET_KEY`, and `GROQ_API_KEY` to real secrets (use Docker secrets or a vault)
- [ ] Set `ALLOWED_ORIGINS` to your frontend domain
- [ ] Add TLS certificates to `nginx/certs/` and configure `nginx/nginx.conf`
- [ ] Set `ENV=production` to disable the development hourly beat schedule
- [ ] Point `notify_email` alert configs to a real email address and integrate a mail provider (SendGrid, SES, etc.) in `tasks._emit_alert()`
- [ ] Use a persistent volume for `redis-data` and `forecast-uploads`
- [ ] Consider adding a Postgres database to replace the `alert_configs.json` file for multi-node deployments

### Scaling workers

```bash
# Run more Celery workers for parallel forecast jobs
docker compose scale forecast-worker=4
```

---

## Contributors

<table>
  <tr align="center">
    <td>
      Souvik Mahanta
      <p align="center">
        <a href="https://github.com/souvik03-136">
          <img src="http://www.iconninja.com/files/241/825/211/round-collaboration-social-github-code-circle-network-icon.svg" width="36" height="36" alt="GitHub"/>
        </a>
        <a href="https://www.linkedin.com/in/souvik-mahanta/">
          <img src="http://www.iconninja.com/files/863/607/751/network-linkedin-social-connection-circular-circle-media-icon.svg" width="36" height="36" alt="LinkedIn"/>
        </a>
      </p>
    </td>
  </tr>
</table>

<p align="center">Made with ❤️ by Souvik Mahanta</p>