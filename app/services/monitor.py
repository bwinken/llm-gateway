"""
Per-user request monitoring.

Stores full request/response payloads as JSONL files under monitor/{username}/.
Designed for short-term debugging — state lives in memory (cleared on restart).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.logger import logger

_MONITOR_DIR = Path(os.getenv("MONITOR_DIR", Path(__file__).resolve().parent.parent.parent / "monitor"))

# In-memory set of monitored user IDs
_monitored: dict[int, str] = {}  # user_id -> username


def add_monitor(user_id: int, username: str) -> None:
    _monitored[user_id] = username


def remove_monitor(user_id: int) -> None:
    _monitored.pop(user_id, None)


def is_monitored(user_id: int) -> bool:
    return user_id in _monitored


# Only monitor these model types (exclude vision variants)
_ALLOWED_TYPES = {"llm", "embedding", "reranker"}

# Warn threshold: 100 MB per user
_WARN_SIZE_BYTES = 100 * 1024 * 1024


def list_monitored() -> list[dict]:
    """Return monitored users with per-type file sizes and total disk usage."""
    result = []
    for user_id, username in _monitored.items():
        user_dir = _MONITOR_DIR / username
        types: dict[str, float] = {}
        total_bytes = 0
        if user_dir.is_dir():
            for f in user_dir.iterdir():
                if f.suffix == ".jsonl" and "_" in f.stem:
                    parts = f.stem.split("_", 1)
                    try:
                        fsize = f.stat().st_size
                    except OSError:
                        continue
                    total_bytes += fsize
                    if len(parts) == 2:
                        req_type = parts[1]
                        types[req_type] = round(types.get(req_type, 0) + fsize / (1024 * 1024), 1)
        size_mb = round(total_bytes / (1024 * 1024), 1)
        result.append({
            "user_id": user_id,
            "username": username,
            "types": types,
            "size_mb": size_mb,
            "warning": size_mb >= _WARN_SIZE_BYTES / (1024 * 1024),
        })
    return result


def _endpoint_to_type(endpoint: str) -> str:
    """Map endpoint label to short type name for filename."""
    mapping = {
        "/v1/chat/completions": "chat",
        "/v1/embeddings": "embedding",
        "/v1/rerank": "rerank",
        "/v1/score": "score",
        "/responses": "chat",
    }
    for key, val in mapping.items():
        if key in endpoint:
            return val
    return "other"


def _write_sync(
    username: str,
    filename_type: str,
    record: dict,
) -> None:
    """Synchronous file write — runs in a thread via run_in_executor."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    user_dir = _MONITOR_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    filepath = user_dir / f"{date_str}_{filename_type}.jsonl"
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.error("Monitor write failed for user={}: {}", username, exc)


def log_monitor(
    user_id: int,
    request_body: Any,
    response_body: Any,
    model: str,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    model_type: str = "",
) -> None:
    """Fire-and-forget: append a request/response record in a background thread."""
    if user_id not in _monitored:
        return
    if model_type and model_type not in _ALLOWED_TYPES:
        return

    import asyncio

    username = _monitored[user_id]
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "endpoint": endpoint,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "request": request_body,
        "response": response_body,
    }

    file_type = _endpoint_to_type(endpoint)
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _write_sync, username, file_type, record)
    except RuntimeError:
        _write_sync(username, file_type, record)


def log_monitor_error(
    user_id: int,
    request_body: Any,
    error: str,
    status_code: int,
    model: str,
    endpoint: str,
    model_type: str = "",
) -> None:
    """Fire-and-forget: log a failed request to {date}_error.jsonl."""
    if user_id not in _monitored:
        return
    if model_type and model_type not in _ALLOWED_TYPES:
        return

    import asyncio

    username = _monitored[user_id]
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "endpoint": endpoint,
        "status_code": status_code,
        "error": error,
        "request": request_body,
    }

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _write_sync, username, "error", record)
    except RuntimeError:
        _write_sync(username, "error", record)
