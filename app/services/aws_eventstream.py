"""Minimal AWS event-stream (application/vnd.amazon.eventstream) decoder.

Bedrock's ConverseStream responses are NOT SSE — they use Amazon's binary
event-stream framing. Each message is:

    prelude:   4B total_length (BE u32) | 4B headers_length (BE u32)
    prelude_crc: 4B CRC32 of the 8 prelude bytes
    headers:   headers_length bytes of {1B name_len | name | 1B value_type | value}
    payload:   total_length - headers_length - 16 bytes
    message_crc: 4B CRC32 of everything before it

The interesting headers are ``:message-type`` ("event" | "exception"),
``:event-type`` (e.g. "contentBlockDelta") / ``:exception-type`` (e.g.
"throttlingException"), and ``:content-type`` (application/json). Payloads
for Bedrock are always JSON.

Implemented here (~100 lines) instead of pulling in botocore: the framing is
stable and fully specified, and the gateway's Bedrock auth is a plain Bearer
API key so nothing else needs the AWS SDK.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterator


class EventStreamError(Exception):
    """Corrupt frame (bad CRC / malformed headers / impossible lengths)."""


# Header value types per the event-stream spec.
_TYPE_BOOL_TRUE = 0
_TYPE_BOOL_FALSE = 1
_TYPE_BYTE = 2
_TYPE_SHORT = 3
_TYPE_INT = 4
_TYPE_LONG = 5
_TYPE_BYTES = 6
_TYPE_STRING = 7
_TYPE_TIMESTAMP = 8
_TYPE_UUID = 9

_PRELUDE_LEN = 12  # total_length + headers_length + prelude_crc
_CRC_LEN = 4
_MAX_MESSAGE_LEN = 16 * 1024 * 1024  # spec maximum (16 MiB) — sanity bound


@dataclass
class EventStreamMessage:
    headers: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""

    @property
    def message_type(self) -> str:
        return str(self.headers.get(":message-type", ""))

    @property
    def event_type(self) -> str:
        return str(self.headers.get(":event-type", ""))

    @property
    def exception_type(self) -> str:
        return str(self.headers.get(":exception-type", ""))


def _parse_headers(data: bytes) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    pos = 0
    end = len(data)
    while pos < end:
        name_len = data[pos]
        pos += 1
        if pos + name_len > end:
            raise EventStreamError("header name overruns headers block")
        name = data[pos:pos + name_len].decode("utf-8")
        pos += name_len
        if pos >= end:
            raise EventStreamError("missing header value type")
        vtype = data[pos]
        pos += 1
        if vtype == _TYPE_BOOL_TRUE:
            value: Any = True
        elif vtype == _TYPE_BOOL_FALSE:
            value = False
        elif vtype == _TYPE_BYTE:
            value = struct.unpack_from(">b", data, pos)[0]
            pos += 1
        elif vtype == _TYPE_SHORT:
            value = struct.unpack_from(">h", data, pos)[0]
            pos += 2
        elif vtype == _TYPE_INT:
            value = struct.unpack_from(">i", data, pos)[0]
            pos += 4
        elif vtype == _TYPE_LONG or vtype == _TYPE_TIMESTAMP:
            value = struct.unpack_from(">q", data, pos)[0]
            pos += 8
        elif vtype == _TYPE_BYTES or vtype == _TYPE_STRING:
            (vlen,) = struct.unpack_from(">H", data, pos)
            pos += 2
            if pos + vlen > end:
                raise EventStreamError("header value overruns headers block")
            raw = data[pos:pos + vlen]
            pos += vlen
            value = raw.decode("utf-8") if vtype == _TYPE_STRING else bytes(raw)
        elif vtype == _TYPE_UUID:
            value = bytes(data[pos:pos + 16]).hex()
            pos += 16
        else:
            raise EventStreamError(f"unknown header value type {vtype}")
        headers[name] = value
    return headers


class EventStreamDecoder:
    """Incremental decoder: feed() arbitrary byte chunks, get complete messages.

    Usage:
        dec = EventStreamDecoder()
        async for chunk in resp.aiter_bytes():
            for msg in dec.feed(chunk):
                ...
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[EventStreamMessage]:
        self._buf.extend(data)
        while True:
            msg = self._next_message()
            if msg is None:
                return
            yield msg

    def _next_message(self) -> EventStreamMessage | None:
        buf = self._buf
        if len(buf) < _PRELUDE_LEN:
            return None
        total_len, headers_len = struct.unpack_from(">II", buf, 0)
        if total_len < _PRELUDE_LEN + _CRC_LEN or total_len > _MAX_MESSAGE_LEN:
            raise EventStreamError(f"implausible frame length {total_len}")
        if headers_len > total_len - _PRELUDE_LEN - _CRC_LEN:
            raise EventStreamError("headers_length exceeds frame")
        (prelude_crc,) = struct.unpack_from(">I", buf, 8)
        if zlib.crc32(bytes(buf[:8])) & 0xFFFFFFFF != prelude_crc:
            raise EventStreamError("prelude CRC mismatch")
        if len(buf) < total_len:
            return None  # wait for more bytes

        frame = bytes(buf[:total_len])
        (message_crc,) = struct.unpack_from(">I", frame, total_len - _CRC_LEN)
        if zlib.crc32(frame[:total_len - _CRC_LEN]) & 0xFFFFFFFF != message_crc:
            raise EventStreamError("message CRC mismatch")

        headers = _parse_headers(frame[_PRELUDE_LEN:_PRELUDE_LEN + headers_len])
        payload = frame[_PRELUDE_LEN + headers_len:total_len - _CRC_LEN]
        del self._buf[:total_len]
        return EventStreamMessage(headers=headers, payload=payload)


def encode_event(
    headers: dict[str, Any],
    payload: bytes,
) -> bytes:
    """Encode one event-stream message (string/bool headers only).

    Used by tests to fabricate Bedrock stream responses; the production path
    only ever decodes.
    """
    hdr = bytearray()
    for name, value in headers.items():
        nb = name.encode("utf-8")
        hdr.append(len(nb))
        hdr.extend(nb)
        if isinstance(value, bool):
            hdr.append(_TYPE_BOOL_TRUE if value else _TYPE_BOOL_FALSE)
        elif isinstance(value, str):
            vb = value.encode("utf-8")
            hdr.append(_TYPE_STRING)
            hdr.extend(struct.pack(">H", len(vb)))
            hdr.extend(vb)
        else:
            raise ValueError(f"unsupported test header type: {type(value)}")
    total_len = _PRELUDE_LEN + len(hdr) + len(payload) + _CRC_LEN
    prelude = struct.pack(">II", total_len, len(hdr))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + prelude_crc + bytes(hdr) + payload
    message_crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return body + message_crc
