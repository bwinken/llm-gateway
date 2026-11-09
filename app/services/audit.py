"""Audit trail of Web UI / admin actions (table ``audit_log``).

``AuditMiddleware`` (pure ASGI) records, after the response:

- every POST / PUT / PATCH / DELETE under ``/admin`` and ``/dashboard``
  (the SSO-cookie paths, ``browser_security.COOKIE_AUTH_PREFIXES``), except
  the read-only probes in ``_NOT_AUDITED``;
- the reads that hand out credentials or bulk data: a full API key
  (``GET /admin/users/{id}/api-key``) and every ``/admin/api/export/*``.

Being a middleware, it covers a new endpoint without anyone remembering to.
Each row has the actor (JWT ``sub``), method, route template, path, status
(500 when the handler raised), client address and a JSON ``detail``:

- ``target``: the account a ``/admin/users/{id}/...`` or
  ``/dashboard/app/{id}/...`` path names, looked up before the handler runs
  (so a deleted account is still named);
- ``request``: the submitted form / JSON fields, redacted and truncated
  (not for ``PUT /admin/api/config``: the whole config; its handler notes a
  diff instead);
- ``response``: a small JSON reply, redacted (``{"ok": true, "is_disabled":
  true}`` says what a toggle did);
- whatever the handler added with ``note(...)``.

Values of keys that look like credentials (``api_key``, ``secret``, ``token``,
``password``, ``authorization``) are replaced by ``<redacted>`` everywhere.

The write happens after the response, in a thread, shielded from client
disconnects, and never fails the request: a failed write logs an error with
the whole record, so the trail survives in the log file. Every record is also
logged as ``Audit | ...``.

Append-only: nothing in the app updates or deletes rows, and the migration
adds a PostgreSQL trigger refusing UPDATE / DELETE. There is no retention:
the table grows with admin activity only.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qsl

import anyio
from sqlmodel import Session, select

from app.core.browser_security import is_cookie_auth_path
from app.core.database import engine
from app.core.logger import logger
from app.models.schema import AuditLog, User

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Read-only POSTs: probes that change nothing.
_NOT_AUDITED: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/admin/api/test-model"),
    ("POST", "/admin/guardrail/test"),
})
_AUDITED_READS = re.compile(r"^/admin/(users/\d+/api-key|api/export/.+)$")
# The whole model config: its handler notes a diff instead of the body.
_BODY_NOT_RECORDED = frozenset({"/admin/api/config"})
_TARGET_RE = re.compile(r"^/(?:admin/users|dashboard/app)/(\d+)(?:/|$)")

_SECRET_KEY_RE = re.compile(r"api_?key|secret|token|password|authorization|cookie", re.I)
_MAX_BODY = 16 * 1024          # request bytes kept for parsing
_MAX_REPLY = 4 * 1024          # JSON reply bytes kept
_MAX_VALUE = 200               # characters per recorded value

_current: ContextVar[dict | None] = ContextVar("audit_note", default=None)


def is_audited(method: str, path: str) -> bool:
    method = method.upper()
    if method in _UNSAFE:
        return is_cookie_auth_path(path) and (method, path) not in _NOT_AUDITED
    return method == "GET" and bool(_AUDITED_READS.match(path))


def note(**fields: Any) -> None:
    """Add ``fields`` to the current request's audit record (no-op outside one).

    Works from sync handlers too: the middleware puts a dict in the context
    and the threadpool's copy of the context holds the same dict."""
    holder = _current.get()
    if holder is not None:
        holder.update(fields)


def redact(value: Any, depth: int = 0) -> Any:
    """Secrets out, long strings cut, deep structures summarised."""
    if depth > 6:
        return "<…>"
    if isinstance(value, dict):
        return {
            str(k): ("<redacted>" if _SECRET_KEY_RE.search(str(k)) and v not in ("", None) else redact(v, depth + 1))
            for k, v in list(value.items())[:50]
        }
    if isinstance(value, list):
        out = [redact(v, depth + 1) for v in value[:20]]
        if len(value) > 20:
            out.append(f"<+{len(value) - 20} more>")
        return out
    if isinstance(value, str) and len(value) > _MAX_VALUE:
        return value[:_MAX_VALUE] + f"…<{len(value)}>"
    return value


def _parse_body(content_type: str, body: bytes, truncated: bool) -> Any:
    if not body:
        return None
    if truncated:
        return f"<{len(body)}+ bytes>"
    ct = content_type.split(";")[0].strip().lower()
    try:
        if ct == "application/x-www-form-urlencoded":
            return redact(dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)))
        if ct == "application/json":
            return redact(json.loads(body))
    except Exception:
        return f"<unparsable {ct}, {len(body)} bytes>"
    return f"<{ct or 'body'}, {len(body)} bytes>"


def config_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What a model-config save changed, by section: aliases added and
    removed, and per changed alias the names of the fields that differ
    (names only: values may be secrets)."""
    out: dict[str, Any] = {}
    for section in sorted(set(before) | set(after)):
        old, new = before.get(section), after.get(section)
        if new is None or old == new:
            continue
        if not isinstance(old, dict) or not isinstance(new, dict):
            out[section] = "changed"
            continue
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed: dict[str, list[str]] = {}
        for alias in sorted(set(old) & set(new)):
            a, b = old[alias], new[alias]
            if a == b:
                continue
            if isinstance(a, dict) and isinstance(b, dict):
                changed[alias] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            else:
                changed[alias] = ["value"]
        entry = {k: v for k, v in (("added", added), ("removed", removed), ("changed", changed)) if v}
        if entry:
            out[section] = entry
    return out


def _actor(headers: dict[str, str]) -> str:
    from app.core import auth  # module attribute: tests patch _decode_jwt

    value = headers.get("authorization", "")
    if not value.lower().startswith("bearer "):
        return ""
    try:
        payload = auth._decode_jwt(value[7:].strip())
    except Exception:
        return ""
    return str((payload or {}).get("sub") or "")


def _target(path: str) -> str | None:
    m = _TARGET_RE.match(path)
    if m is None:
        return None
    try:
        with Session(engine) as session:
            name = session.exec(select(User.username).where(User.id == int(m.group(1)))).first()
        return name or f"#{m.group(1)} (unknown)"
    except Exception:
        return f"#{m.group(1)}"


def _write(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        with Session(engine) as session:
            session.add(AuditLog(**{**record, "detail": json.dumps(record["detail"], ensure_ascii=False, default=str)}))
            session.commit()
    except Exception as exc:
        logger.error("Audit write failed | error={}: {} | record={}", type(exc).__name__, exc, line)
        return
    logger.info("Audit | {}", line)


class AuditMiddleware:
    """Pure ASGI: tees the request body as the app reads it (nothing is
    buffered ahead or replayed) and the start of a JSON reply."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if not is_audited(method, path):
            await self.app(scope, receive, send)
            return

        headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            try:
                headers[k.decode("latin-1").lower()] = v.decode("latin-1")
            except Exception:
                pass
        target = await anyio.to_thread.run_sync(_target, path) if _TARGET_RE.match(path) else None

        body = bytearray()
        body_truncated = False
        reply = bytearray()
        reply_json = False
        status = 500

        async def tee_receive():
            nonlocal body_truncated
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                if len(body) + len(chunk) <= _MAX_BODY:
                    body.extend(chunk)
                else:
                    body_truncated = True
            return message

        async def tee_send(message):
            nonlocal status, reply_json
            if message["type"] == "http.response.start":
                status = int(message["status"])
                for k, v in message.get("headers") or []:
                    if k.lower() == b"content-type":
                        reply_json = v.split(b";")[0].strip().lower() == b"application/json"
            elif message["type"] == "http.response.body" and reply_json and len(reply) <= _MAX_REPLY:
                reply.extend(message.get("body", b""))
            await send(message)

        holder: dict[str, Any] = {}
        token = _current.set(holder)
        try:
            await self.app(scope, tee_receive, tee_send)
        finally:
            _current.reset(token)
            detail: dict[str, Any] = {}
            if target is not None:
                detail["target"] = target
            if path not in _BODY_NOT_RECORDED:
                parsed = _parse_body(headers.get("content-type", ""), bytes(body), body_truncated)
                if parsed is not None:
                    detail["request"] = parsed
                if reply_json and reply and len(reply) <= _MAX_REPLY:
                    try:
                        detail["response"] = redact(json.loads(bytes(reply)))
                    except Exception:
                        pass
            detail.update(redact(holder))
            client = scope.get("client") or ("", 0)
            route = scope.get("route")
            record = {
                "actor": _actor(headers),
                "method": method,
                "route": getattr(route, "path", "") or path,
                "path": path,
                "status": status,
                "client": str(client[0] or ""),
                "detail": detail,
            }
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(_write, record)
                except Exception as exc:
                    logger.error("Audit write failed | error={}: {} | record={}", type(exc).__name__, exc, record)
