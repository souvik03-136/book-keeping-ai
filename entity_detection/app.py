# book-keeping-ai/entity_detection/app.py

"""
Entity Detection Service — Production
======================================
Unified API combining LLM-based extraction (Groq) and rule-based NLP (spaCy).

Strategy
--------
- /api/v1/extract          — Full transaction entity extraction (LLM + spaCy fallback)
- /api/v1/extract/entities — Query entity extraction (rule-based, deterministic)

Both endpoints validate input with marshmallow, apply rate limiting, and
return structured, consistent JSON responses.
"""

import logging
import os
import re
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from marshmallow import Schema, ValidationError, fields, validate

load_dotenv()

app = Flask(__name__)
_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
CORS(app, resources={r"/api/*": {"origins": _origins}})

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Use memory:// fallback so tests pass without a live Redis instance.
# In production the REDIS_URL env var points to the real broker.
_storage_uri = REDIS_URL if os.getenv("ENV") != "test" else "memory://"

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["300 per day", "60 per hour"],
    storage_uri=_storage_uri,
    on_breach=lambda: None,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("entity_detection")

# ---------------------------------------------------------------------------
# Lazy-load heavy deps (spaCy, Groq, word2number)
# ---------------------------------------------------------------------------

_nlp = None
_groq_client = None


def get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def get_groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set in environment.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ExtractRequestSchema(Schema):
    text = fields.Str(required=True, validate=validate.Length(min=3, max=2000))
    backend = fields.Str(
        load_default="auto",
        validate=validate.OneOf(["llm", "nlp", "auto"]),
    )


class EntityQuerySchema(Schema):
    text = fields.Str(required=True, validate=validate.Length(min=1, max=500))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = os.getenv("API_KEY")
        if expected and request.headers.get("X-API-Key") != expected:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _clean_price(raw: str) -> str:
    return re.sub(r"[^\d.]", "", raw)


def _word_to_num(word: str) -> str:
    try:
        from word2number import w2n
        return str(w2n.word_to_num(word))
    except (ValueError, ImportError):
        return word


def _validate_price_qty(price: str, quantity: str) -> tuple[str, str]:
    """Swap price/qty if the parser got them backwards (qty > price heuristic)."""
    if re.match(r"^\d+(\.\d{1,2})?$", price) and re.match(r"^\d+$", quantity):
        return price, quantity
    if re.match(r"^\d+$", price) and re.match(r"^\d+$", quantity):
        if int(quantity) > int(price):
            return quantity, price
    return price, quantity


def _extract_with_llm(text: str) -> dict | None:
    """Use Groq (llama3-8b) to extract transaction entities."""
    try:
        client = get_groq()
        response = client.chat.completions.create(
            messages=[{
                "role": "system",
                "content": (
                    "You are a precise entity extractor. "
                    "Extract CustomerName, Price, ItemName, and "
                    "ItemQuantity from the transaction text. "
                    "Respond ONLY in this exact format with no other text:\n"
                    "CustomerName: <value>\n"
                    "Price: <value>\n"
                    "ItemName: <value>\n"
                    "ItemQuantity: <value>\n"
                    "Use 'not_found' for missing values."
                ),
            }, {
                "role": "user",
                "content": text,
            }],
            model="llama3-8b-8192",
            temperature=0,
            max_tokens=150,
        )
        raw = response.choices[0].message.content
        return _parse_llm_response(raw)
    except Exception:
        logger.exception("LLM extraction failed; will fall back to NLP")
        return None


def _parse_llm_response(raw: str) -> dict:
    def _get(pattern: str) -> str:
        m = re.search(pattern, raw, re.IGNORECASE)
        if not m:
            return ""
        v = m.group(1).strip()
        return "" if v.lower() in {"not_found", "none", "not mentioned", ""} else v

    return {
        "CustomerName": _get(r"CustomerName:\s*(.*)"),
        "Price": _clean_price(_get(r"Price:\s*(.*)")),
        "ItemName": _get(r"ItemName:\s*(.*)"),
        "ItemQuantity": _word_to_num(_get(r"ItemQuantity:\s*(.*)")),
    }


def _extract_with_nlp(text: str) -> dict:
    """spaCy NER + regex fallback extraction."""
    nlp = get_nlp()
    doc = nlp(text)

    customer = price = item = qty = ""

    for ent in doc.ents:
        if ent.label_ == "PERSON" and not customer:
            customer = ent.text
        elif ent.label_ in {"MONEY", "CURRENCY"} and not price:
            price = _clean_price(ent.text)
        elif ent.label_ == "PRODUCT" and not item:
            item = ent.text

    qty_match = re.search(r"(\d+)\s+([a-zA-Z]+)\s+for", text, re.IGNORECASE)
    if qty_match:
        if not qty:
            qty = qty_match.group(1)
        if not item:
            item = qty_match.group(2)

    price_match = re.search(r"for\s*\$?(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
    if price_match and not price:
        price = _clean_price(price_match.group(1))

    qty = _word_to_num(qty)
    price, qty = _validate_price_qty(price, qty)

    return {
        "CustomerName": customer,
        "Price": price,
        "ItemName": item,
        "ItemQuantity": qty,
    }


def _is_complete(entities: dict) -> bool:
    keys = ["CustomerName", "Price", "ItemName", "ItemQuantity"]
    return all(entities.get(k) for k in keys)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "entity-detection"}), 200


@app.route("/api/v1/extract", methods=["POST"])
@require_api_key
@limiter.limit("60 per minute")
def extract():
    """
    Extract transaction entities from natural language text.

    Backends:
    - ``llm``  : Groq LLM only
    - ``nlp``  : spaCy only
    - ``auto`` : LLM first, fall back to NLP if incomplete (default)
    """
    schema = ExtractRequestSchema()
    try:
        data = schema.load(request.get_json(force=True) or {})
    except ValidationError as err:
        return jsonify({"error": "Invalid request", "details": err.messages}), 422

    text = data["text"]
    backend = data["backend"]
    entities = None
    backend_used = None

    if backend in {"llm", "auto"}:
        entities = _extract_with_llm(text)
        backend_used = "llm"

    if backend == "auto" and (entities is None or not _is_complete(entities)):
        entities = _extract_with_nlp(text)
        backend_used = "nlp"

    if backend == "nlp":
        entities = _extract_with_nlp(text)
        backend_used = "nlp"

    if not _is_complete(entities):
        return jsonify({
            "error": (
                "Could not extract all required entities. "
                "Provide text like: \"John Doe bought 2 apples for $5\"."
            ),
            "partial": entities,
        }), 422

    entities["_backend_used"] = backend_used
    logger.info(
        "Extracted entities | backend=%s text_len=%d", backend_used, len(text)
    )
    return jsonify(entities), 200


@app.route("/api/v1/extract/entities", methods=["POST"])
@require_api_key
@limiter.limit("120 per minute")
def extract_entities():
    """
    Parse a natural-language inventory query into a structured filter.

    Supports::

        "apples less than 50"              → {object, action: "less", range}
        "oranges more than 100"            → {object, action: "more", range}
        "apples more than 20 less than 80" → {object, action: "range", min, max}
    """
    schema = EntityQuerySchema()
    try:
        data = schema.load(request.get_json(force=True) or {})
    except ValidationError as err:
        return jsonify({"error": "Invalid request", "details": err.messages}), 422

    text = data["text"].lower().strip()

    # Strip common currency/unit suffixes so "50 rs" → "50"
    text = re.sub(r"\s+(rs|usd|inr|eur|gbp|units?|pcs?|items?)$", "", text)

    range_re = re.compile(
        r"(\w+)\s+more\s+than\s+(\d+)\s+(?:and\s+)?less\s+than\s+(\d+)"
    )
    less_re = re.compile(r"(?:(\w+)\s+)?less\s+than\s+(\d+)")
    more_re = re.compile(r"(?:(\w+)\s+)?more\s+than\s+(\d+)")

    m = range_re.search(text)
    if m:
        return jsonify({
            "object": m.group(1) or "*",
            "action": "range",
            "min": m.group(2),
            "max": m.group(3),
        }), 200

    m = less_re.search(text)
    if m:
        return jsonify({
            "object": m.group(1) or "*",
            "action": "less",
            "range": m.group(2),
        }), 200

    m = more_re.search(text)
    if m:
        return jsonify({
            "object": m.group(1) or "*",
            "action": "more",
            "range": m.group(2),
        }), 200

    return jsonify({
        "error": (
            "Unrecognised query format. "
            "Try: \"apples less than 50\" or \"oranges more than 100\"."
        ),
    }), 422


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Rate limit exceeded",
        "retry_after": str(e.description),
    }), 429


@app.errorhandler(500)
def internal_error(_):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)