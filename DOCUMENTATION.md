# Book-Keeping AI — Technical Documentation

## Contents

1. [System Architecture](#1-system-architecture)
2. [Data Flow Diagrams](#2-data-flow-diagrams)
3. [Forecasting Engine](#3-forecasting-engine)
4. [Entity Detection Engine](#4-entity-detection-engine)
5. [Scheduler & Alert System](#5-scheduler--alert-system)
6. [API Error Codes](#6-api-error-codes)
7. [Database / Storage Schema](#7-database--storage-schema)
8. [Security Model](#8-security-model)
9. [Extending the System](#9-extending-the-system)

---

## 1. System Architecture

### High-level component diagram

```mermaid
graph TD
    Client["Client / Frontend"]
    Nginx["Nginx\n(Reverse Proxy + TLS)"]
    ForecastAPI["Forecast API\nFlask :5000"]
    EntityAPI["Entity API\nFlask :5001"]
    Redis["Redis :6379\nBroker · Backend · Rate-limit"]
    Worker["Celery Worker\n(forecast jobs)"]
    Beat["Celery Beat\n(daily scheduler)"]
    FS["File System\n/uploads"]

    Client --> Nginx
    Nginx -->|"/api/v1/upload\n/api/v1/forecast\n/api/v1/alerts"| ForecastAPI
    Nginx -->|"/api/v1/extract\n/api/v1/extract/entities"| EntityAPI
    ForecastAPI -->|"enqueue task"| Redis
    ForecastAPI -->|"read/write"| FS
    EntityAPI -->|"rate-limit store"| Redis
    Redis -->|"consume"| Worker
    Redis -->|"trigger"| Beat
    Beat -->|"enqueue check_all_alerts"| Redis
    Worker -->|"read"| FS
    Worker -->|"write forecast HTML"| FS
```

### Service responsibilities

| Service | Owns | Stateless? |
|---|---|---|
| `forecast-api` | HTTP interface, input validation, auth, file IDs | Yes (uploads on shared volume) |
| `entity-api` | HTTP interface, NLP/LLM extraction | Yes |
| `forecast-worker` | Model training + inference, plot generation | Yes |
| `forecast-beat` | Schedule definition, periodic task dispatch | No (needs `celerybeat-schedule` file) |
| `redis` | Task queue, result storage, rate-limit counters | No (persisted to volume) |

---

## 2. Data Flow Diagrams

### Upload + Forecast flow

```mermaid
sequenceDiagram
    participant C as Client
    participant N as Nginx
    participant F as Forecast API
    participant R as Redis
    participant W as Celery Worker

    C->>N: POST /api/v1/upload (CSV)
    N->>F: forward
    F->>F: validate extension & size
    F->>F: write to /uploads/{file_id}.csv
    F-->>C: 201 { file_id }

    C->>N: POST /api/v1/forecast { item_id, file_id }
    N->>F: forward
    F->>F: validate item_id exists in file
    F->>R: LPUSH celery queue (task payload)
    F-->>C: 202 { task_id, poll_url }

    loop Poll every 3s
        C->>N: GET /api/v1/tasks/{task_id}
        N->>F: forward
        F->>R: GET result:{task_id}
        R-->>F: PENDING / STARTED {progress} / SUCCESS {result}
        F-->>C: 202 pending | 200 result
    end

    W->>R: BRPOP (blocking dequeue)
    R-->>W: task payload
    W->>W: load_data(file_id)
    W->>W: predict_demand (SARIMA + LSTM)
    W->>W: check_stock_and_alert
    W->>W: create_bokeh_plots → /uploads/forecast_{item_id}.html
    W->>R: SET result:{task_id} {json result}
```

### Entity extraction flow (auto backend)

```mermaid
sequenceDiagram
    participant C as Client
    participant E as Entity API
    participant G as Groq LLM
    participant S as spaCy NLP

    C->>E: POST /api/v1/extract { text, backend: "auto" }
    E->>E: validate schema (marshmallow)
    E->>G: chat.completions (llama3-8b)
    G-->>E: "CustomerName: John Doe\n..."
    E->>E: parse + validate completeness
    alt all 4 entities found
        E-->>C: 200 { CustomerName, ItemName, ItemQuantity, Price }
    else missing entities
        E->>S: nlp(text) NER + regex fallback
        S-->>E: entity annotations
        E->>E: merge + validate
        alt still incomplete
            E-->>C: 422 { error, partial }
        else complete
            E-->>C: 200 { ... , _backend_used: "nlp" }
        end
    end
```

### Scheduled alert flow

```mermaid
sequenceDiagram
    participant B as Celery Beat
    participant R as Redis
    participant W as Celery Worker
    participant FS as File System
    participant N as Notifier (stub)

    Note over B: 08:00 UTC daily
    B->>R: enqueue check_all_alerts
    R-->>W: dequeue task
    W->>FS: read alert_configs.json
    W->>FS: find latest *.csv / *.xlsx in /uploads
    loop for each item_id in configs
        W->>W: predict_demand(item_id, horizon=6)
        W->>W: check_stock_and_alert
        alt demand > stock + threshold
            W->>N: _emit_alert(item_id, msgs, email)
            Note over N: log [ALERT]\n+ email stub
        end
    end
    W->>R: SET result
```

---

## 3. Forecasting Engine

### Model pipeline

```mermaid
flowchart TD
    A["Raw DataFrame"] --> B["_prepare_monthly\nGroup by year_month, sum quantity"]
    B --> C["_make_stationary\nADF test → seasonal diff if needed"]
    C --> D1["_fit_sarima\nGrid search (p,d,q)×(P,D,Q,12)\nMin AIC selection"]
    C --> D2["_fit_lstm\nWindow=6, LSTM(128)→LSTM(64)→Dense(1)\nEarlyStopping patience=10"]
    D1 --> E["_ensemble\nDynamic weighting via hold-out MAPE"]
    D2 --> E
    E --> F["predicted_demand array"]
    F --> G["check_stock_and_alert\nCompare vs current stock"]
    F --> H["create_bokeh_plots\nHTML chart → /uploads/forecast_{item_id}.html"]
```

### Ensemble weight calculation

The ensemble weight for SARIMA and LSTM is computed from their Mean Absolute Percentage Error on the last `horizon_months` of known data:

```
sarima_weight = lstm_mape  / (sarima_mape + lstm_mape)
lstm_weight   = sarima_mape / (sarima_mape + lstm_mape)
```

The model with **lower MAPE** receives a **higher weight**. If fewer than `horizon_months` samples exist, a fixed 60/40 (SARIMA/LSTM) split is used.

### Minimum data requirements

| Model | Minimum monthly observations |
|---|---|
| SARIMA | 4 (after stationarity transform) |
| LSTM | `WINDOW_SIZE + 4 = 10` |
| Ensemble | 10 (LSTM threshold is the binding constraint) |

If LSTM cannot run (insufficient data), the forecast falls back to SARIMA only.

---

## 4. Entity Detection Engine

### Extraction priority (auto backend)

```mermaid
flowchart LR
    T["Input text"] --> V["marshmallow\nvalidation"]
    V --> L["Groq LLM\n(llama3-8b-8192)"]
    L --> C{All 4 entities\nfound?}
    C -->|Yes| R["Return 200"]
    C -->|No / LLM error| S["spaCy NER\n+ regex fallback"]
    S --> C2{All 4 entities\nfound?}
    C2 -->|Yes| R2["Return 200\n_backend_used: nlp"]
    C2 -->|No| E["Return 422\npartial result"]
```

### Entities extracted

| Field | Source (LLM) | Source (NLP) |
|---|---|---|
| `CustomerName` | `PERSON` entity in LLM response | spaCy `PERSON` label |
| `ItemName` | `ItemName` field from LLM | spaCy `PRODUCT` + regex `(\d+) (\w+) for` |
| `ItemQuantity` | `ItemQuantity` field from LLM | regex group 1, word-to-number normalised |
| `Price` | `Price` field from LLM | spaCy `MONEY` + regex `for \$?(\d+)` |

### Query entity grammar

```mermaid
flowchart TD
    Q["Query text (lowercased)"] --> S1["Strip currency/unit suffix\n(rs, usd, inr, units, pcs)"]
    S1 --> M1{range pattern:\nWORD more than N less than M}
    M1 -->|match| R1["{action: range, min: N, max: M}"]
    M1 -->|no match| M2{less_than pattern:\nWORD? less than N}
    M2 -->|match| R2["{action: less, range: N}"]
    M2 -->|no match| M3{more_than pattern:\nWORD? more than N}
    M3 -->|match| R3["{action: more, range: N}"]
    M3 -->|no match| E["422 Unrecognised format"]
```

---

## 5. Scheduler & Alert System

### Celery Beat schedule

| Task | Schedule | Configurable? |
|---|---|---|
| `check_all_alerts` (production) | Daily, 08:00 UTC | Change `crontab(hour=8, minute=0)` in `tasks.py` |
| `check_all_alerts` (development) | Hourly | Enabled when `ENV=development` |

### Alert config storage

Alert configurations are stored in `/uploads/alert_configs.json`:

```json
{
  "A001": {
    "low_stock_threshold": 20.0,
    "notify_email": "ops@company.com"
  },
  "B007": {
    "low_stock_threshold": 0.0,
    "notify_email": null
  }
}
```

**Production note:** For multi-worker deployments, migrate this to a Postgres table or Redis hash so all workers share the same config without file-locking issues.

### Integrating a real notification provider

Replace the `_emit_alert()` stub in `tasks.py`:

```python
# SendGrid example
def _emit_alert(item_id, alerts, email):
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    if not email:
        return
    message = Mail(
        from_email="alerts@yourcompany.com",
        to_emails=email,
        subject=f"Low stock alert — Item {item_id}",
        plain_text_content="\n".join(a["message"] for a in alerts),
    )
    SendGridAPIClient(os.environ["SENDGRID_API_KEY"]).send(message)
```

---

## 6. API Error Codes

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `missing_file_id` | `file_id` not provided in forecast request |
| 401 | `unauthorized` | Missing or wrong `X-API-Key` header |
| 404 | `file_not_found` | `file_id` does not match any uploaded file |
| 415 | `unsupported_media` | File extension not `.csv` or `.xlsx` |
| 422 | `validation_error` | Request body failed marshmallow schema |
| 429 | `rate_limit` | Too many requests; includes `retry_after` |
| 500 | `internal_error` | Unhandled server error (check worker logs) |
| 202 | `queued` / `running` | Task still in progress; keep polling |

---

## 7. Database / Storage Schema

The current implementation uses the file system. The layout under `UPLOAD_FOLDER`:

```
/uploads/
  {file_id}.csv          ← uploaded dataset (CSV)
  {file_id}.xlsx         ← uploaded dataset (XLSX)
  forecast_{item_id}.html  ← Bokeh chart (generated by worker)
  alert_configs.json     ← alert configurations
  celerybeat-schedule    ← Celery Beat internal state
```

### Migrating to Postgres (recommended for production)

Add two tables:

```sql
CREATE TABLE uploads (
    file_id     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE alert_configs (
    item_id              TEXT PRIMARY KEY,
    low_stock_threshold  NUMERIC NOT NULL DEFAULT 0,
    notify_email         TEXT,
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 8. Security Model

### Current

- **API Key**: `X-API-Key` header checked on every endpoint. Key set via `API_KEY` env var.
- **Rate limiting**: per-IP limits backed by Redis. Default: 200/day, 50/hour globally; tighter limits on write endpoints.
- **Input sanitisation**: `sanitize_item_id()` strips path-traversal characters. marshmallow validates all request bodies.
- **File validation**: extension whitelist + max size enforced before writing to disk.
- **Non-root container**: all services run as UID 1000.

### Recommended additions for production

- Replace shared API key with **JWT** (short-lived tokens, per-user identity).
- Add **request signing** for webhook callbacks from the Celery worker.
- Enable **Redis AUTH** (`requirepass` in redis.conf).
- Store secrets in **HashiCorp Vault** or **AWS Secrets Manager**, not `.env` files.

---

## 9. Extending the System

### Add a new forecasting model

1. Implement a function in `forecasting.py`:
   ```python
   def _fit_prophet(series, horizon) -> np.ndarray | None:
       ...
   ```
2. Call it alongside SARIMA and LSTM in `predict_demand()`.
3. Include its MAPE in `_ensemble()` and extend the weight calculation to N models.

### Add a new entity type

1. Add the field to the marshmallow schema in `entity_detection/app.py`.
2. Add a regex/NER extractor in `_extract_with_nlp()`.
3. Add a prompt instruction in `_extract_with_llm()`.
4. Update `_is_complete()` to check the new field.

### Add a new notification channel (e.g. Slack)

Replace or extend `_emit_alert()` in `tasks.py`:

```python
def _emit_alert(item_id, alerts, email):
    _slack_alert(item_id, alerts)
    if email:
        _email_alert(item_id, alerts, email)

def _slack_alert(item_id, alerts):
    import httpx
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    text = "\n".join(f"• {a['message']}" for a in alerts if a["level"] == "warning")
    httpx.post(webhook, json={"text": f"*Stock alert — {item_id}*\n{text}"})
```