"""Structural summaries of request bodies for error logs.

The Azure and Bedrock error paths used to write the client's whole request
body into the gateway log on every 4xx/5xx. That body is the user's prompt:
source code, pasted customer data, whatever they were working on. It landed
unmasked in ``gateway_<pid>.log``, kept for ``LOG_RETENTION`` (14 days by
default), readable by anyone with host access — and it was on by default,
unlike ``LANGFUSE_CAPTURE_IO``, which is a deliberate governance decision.

What actually diagnoses a downstream 400 is the *shape* of the body, not its
prose: which fields were present, how the messages alternate, whether a
content block was text or an image, how long each part was. So the summary
keeps the schema and drops the content:

    {"model":"gpt-4o","messages":[{"role":"system","content":"<str:1204>"},
     {"role":"user","content":[{"type":"text","text":"<str:88>"},
     {"type":"image_url","image_url":{"url":"<str:41203>"}}]}],"stream":true}

Every string is replaced by its length unless its key is in `_SAFE_KEYS` —
a whitelist of schema-level fields (roles, types, model names, tool names,
call ids) that carry no user content. Numbers and booleans pass through:
`max_tokens`, `temperature` and friends are parameters, not prose. Object
keys are kept verbatim — in these payloads they are field names (schema),
including the parameter names inside a tool's JSON schema.

Set ``LOG_REQUEST_BODIES=true`` to restore raw bodies while chasing a
specific bug. It is an escape hatch, not a setting to leave on.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Keys whose string values are schema, not user content: enum-ish fields,
# identifiers used to diagnose pairing bugs, and configured names. A value
# is only echoed when it is also short (see `_MAX_SAFE_VALUE`), so a client
# stuffing a prompt into `name` still gets summarised.
_SAFE_KEYS: frozenset[str] = frozenset({
    "model", "role", "type", "name", "id", "call_id", "tool_call_id", "tool_use_id",
    "finish_reason", "stop_reason", "status", "object", "effort", "reasoning_effort",
    "service_tier", "api_version", "anthropic_version", "encoding_format",
    "tool_choice", "format", "detail", "media_type", "mime_type", "source_type",
})

_MAX_SAFE_VALUE = 64      # a "safe" key with a longer value is summarised anyway
_MAX_ITEMS = 20           # list elements rendered before collapsing the tail
_DEFAULT_LIMIT = 4000     # characters of rendered summary


def raw_bodies_enabled() -> bool:
    """True when LOG_REQUEST_BODIES asks for unredacted bodies (debug only)."""
    return os.getenv("LOG_REQUEST_BODIES", "").strip().lower() in {"1", "true", "yes", "on"}


def _summarize(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        if key in _SAFE_KEYS and len(value) <= _MAX_SAFE_VALUE:
            return value
        return f"<str:{len(value)}>"
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        return {str(k): _summarize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_summarize(v, key) for v in list(value)[:_MAX_ITEMS]]
        extra = len(value) - _MAX_ITEMS
        if extra > 0:
            items.append(f"<+{extra} more>")
        return items
    return f"<{type(value).__name__}>"


def summarize_body(body: Any, limit: int = _DEFAULT_LIMIT) -> str:
    """Render `body` with its structure intact and its content replaced.

    Honours LOG_REQUEST_BODIES, which dumps the body verbatim instead —
    for debugging only.
    """
    if raw_bodies_enabled():
        try:
            return json.dumps(body, ensure_ascii=False)[:limit * 2]
        except Exception:
            return repr(body)[:limit * 2]

    try:
        rendered = json.dumps(_summarize(body), ensure_ascii=False)
    except Exception:
        # Never let diagnostics raise from an error path.
        return f"<unsummarizable:{type(body).__name__}>"
    if len(rendered) > limit:
        return rendered[:limit] + f"…(+{len(rendered) - limit} chars)"
    return rendered
