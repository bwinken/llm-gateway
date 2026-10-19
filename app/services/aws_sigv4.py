"""
AWS Signature Version 4 request signing, self-contained (no botocore).

Used by the Bedrock proxy when an entry carries IAM credentials
(``aws_access_key_id`` / ``aws_secret_access_key``) instead of a Bedrock API
key. The secret never leaves the process: each request carries the access
key ID plus an HMAC over the method, path, query, signed headers, timestamp
and the SHA-256 of the exact body bytes, so the body must be sent byte for
byte as signed (send ``content=``, never ``json=``).

Details that are easy to get wrong:
  * The canonical URI is the request path URI-encoded *again* (every
    service except S3). Bedrock model IDs contain ``:``, which is already
    ``%3A`` in the URL, so it appears as ``%253A`` in the canonical request.
  * The signed ``host`` value must equal the Host header httpx sends: the
    URL's host, plus the port only when it is not the scheme's default.
  * A signature is valid for about 5 minutes, so sign right before sending.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlsplit

_ALGORITHM = "AWS4-HMAC-SHA256"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _host_header(scheme: str, host: str, port: int | None) -> str:
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return host
    return f"{host}:{port}"


def _canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = [(quote(k, safe="-_.~"), quote(v, safe="-_.~")) for k, v in pairs]
    return "&".join(f"{k}={v}" for k, v in sorted(encoded))


def sign_request(
    method: str,
    url: str,
    body: bytes,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the headers (``Authorization``, ``X-Amz-Date``) that sign this
    request. Every other header is left out of the signature, so callers may
    add Content-Type / Accept freely."""
    parts = urlsplit(url)
    host = _host_header(parts.scheme, (parts.hostname or "").lower(), parts.port)
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    signed_headers = "host;x-amz-date"
    canonical_request = "\n".join([
        method.upper(),
        quote(parts.path or "/", safe="/~"),
        _canonical_query(parts.query),
        f"host:{host}\nx-amz-date:{amz_date}\n",
        signed_headers,
        hashlib.sha256(body).hexdigest(),
    ])

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "Authorization": (
            f"{_ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "X-Amz-Date": amz_date,
    }
