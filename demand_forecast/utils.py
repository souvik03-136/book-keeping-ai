"""
Utility helpers — validation, file handling, data loading.
"""

import hashlib
from pathlib import Path
import re
import uuid

import pandas as pd

ALLOWED_EXTENSIONS = {"csv", "xlsx"}
REQUIRED_COLUMNS = {"transaction_date", "item_id", "quantity"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def sanitize_item_id(raw: str) -> str:
    """Strip characters that could be used for path traversal or injection."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(raw))[:64]


def get_upload_path(upload_folder: Path, original_filename: str) -> tuple[str, Path]:
    """
    Return a (file_id, destination_path) pair.

    The file_id is a short deterministic hash of the original filename + a
    random salt so uploads of the same filename don't collide.
    """
    ext = original_filename.rsplit(".", 1)[1].lower()
    salt = uuid.uuid4().hex[:8]
    raw = f"{original_filename}:{salt}"
    file_id = hashlib.sha1(raw.encode()).hexdigest()[:12]
    dest = upload_folder / f"{file_id}.{ext}"
    return file_id, dest


def load_data(data_path: str) -> pd.DataFrame:
    """
    Load a CSV or XLSX file, validate required columns, and parse dates.

    Raises
    ------
    ValueError    if required columns are missing
    RuntimeError  if the file type is unsupported
    """
    path = Path(data_path)
    ext = path.suffix.lower()

    if ext == ".csv":
        df = pd.read_csv(path)
    elif ext in {".xlsx", ".xls"}:
        df = pd.read_excel(path)
    else:
        raise RuntimeError(f"Unsupported file type: {ext}")

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Dataset is missing required columns: {missing}. "
            f"Found: {list(df.columns)}"
        )

    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    invalid_dates = df["transaction_date"].isna().sum()
    if invalid_dates > 0:
        raise ValueError(
            f"{invalid_dates} rows have unparseable transaction_date values."
        )

    df["quantity"] = (
        pd.to_numeric(df["quantity"], errors="coerce").fillna(0).clip(lower=0)
    )
    return df