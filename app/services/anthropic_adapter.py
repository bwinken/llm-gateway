"""
Anthropic Messages API <-> OpenAI Chat Completions translation.

The gateway exposes Anthropic-compatible /v1/messages by translating requests
to OpenAI chat completions format (which downstream vLLM servers speak), then
translating the response back to Anthropic format.

Supports:
  - text + image content blocks (vlm)
  - tool_use / tool_result (function calling)
  - streaming SSE events (message_start, content_block_*, message_delta, message_stop)
  - usage accounting
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# stop_reason mapping
# ---------------------------------------------------------------------------

_OPENAI_TO_ANTHROPIC_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def _map_stop_reason(openai_finish: str | None) -> str | None:
    if openai_finish is None:
        return None
    return _OPENAI_TO_ANTHROPIC_STOP.get(openai_finish, "end_turn")


# ---------------------------------------------------------------------------
# Request: Anthropic -> OpenAI
# ---------------------------------------------------------------------------

def _convert_content_block_to_openai(block: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a single Anthropic content block to OpenAI format.

    Returns None when the block should be dropped from the OpenAI message
    (e.g. tool_use / tool_result, which are handled at the message level).
    """
    btype = block.get("type")
    if btype == "text":
        return {"type": "text", "text": block.get("text", "")}
    if btype == "image":
        source = block.get("source", {}) or {}
        stype = source.get("type")
        if stype == "base64":
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            url = f"data:{media_type};base64,{data}"
        elif stype == "url":
            url = source.get("url", "")
        else:
            return None
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _content_to_openai_text(content: Any) -> str:
    """Flatten Anthropic content to a plain string (used for system prompt)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def anthropic_to_openai_request(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic /v1/messages request body into an OpenAI
    /v1/chat/completions request body."""
    openai_messages: list[dict[str, Any]] = []

    # System prompt — Anthropic uses a top-level "system" field (string or list)
    system = body.get("system")
    if system:
        openai_messages.append(
            {"role": "system", "content": _content_to_openai_text(system)}
        )

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Simple string content -> direct mapping
        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        # Split content blocks: regular blocks vs tool_use/tool_result
        regular_blocks: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[tuple[str, str]] = []  # (tool_use_id, content)

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
            elif btype == "tool_result":
                tr_content = block.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = _content_to_openai_text(tr_content)
                elif not isinstance(tr_content, str):
                    tr_content = json.dumps(tr_content)
                tool_results.append((block.get("tool_use_id", ""), tr_content))
            else:
                converted = _convert_content_block_to_openai(block)
                if converted is not None:
                    regular_blocks.append(converted)

        # Emit tool result messages first (each tool_result becomes its own
        # OpenAI "tool" role message)
        for tool_use_id, tr_content in tool_results:
            openai_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": tr_content,
                }
            )

        # Emit the main message (skip if empty assistant tool-only message)
        if regular_blocks or tool_calls or not tool_results:
            msg_out: dict[str, Any] = {"role": role}
            if regular_blocks:
                # If single text block, use plain string for compatibility
                if len(regular_blocks) == 1 and regular_blocks[0]["type"] == "text":
                    msg_out["content"] = regular_blocks[0]["text"]
                else:
                    msg_out["content"] = regular_blocks
            else:
                msg_out["content"] = ""
            if tool_calls:
                msg_out["tool_calls"] = tool_calls
            # Skip empty placeholders left over from pure tool_result messages
            if msg_out["content"] == "" and not tool_calls and tool_results:
                continue
            openai_messages.append(msg_out)

    openai_body: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": openai_messages,
    }

    # Direct parameter pass-through
    if "max_tokens" in body:
        openai_body["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]
    if "stream" in body:
        openai_body["stream"] = body["stream"]
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]

    # Tools
    tools = body.get("tools")
    if tools:
        openai_tools = []
        for t in tools:
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
            )
        openai_body["tools"] = openai_tools

    tool_choice = body.get("tool_choice")
    if tool_choice:
        tc_type = tool_choice.get("type") if isinstance(tool_choice, dict) else None
        if tc_type == "auto":
            openai_body["tool_choice"] = "auto"
        elif tc_type == "any":
            openai_body["tool_choice"] = "required"
        elif tc_type == "tool" and tool_choice.get("name"):
            openai_body["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }

    return openai_body


# ---------------------------------------------------------------------------
# Response: OpenAI -> Anthropic (non-streaming)
# ---------------------------------------------------------------------------

def openai_to_anthropic_response(
    openai_resp: dict[str, Any],
    model_alias: str,
) -> dict[str, Any]:
    """Translate an OpenAI chat completion response to an Anthropic message."""
    choices = openai_resp.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message", {}) or {}

    content_blocks: list[dict[str, Any]] = []

    # Reasoning / chain-of-thought. vLLM's --enable-reasoning (and DeepSeek's
    # own OpenAI-compatible API) put thinking output into a separate field
    # on the message, alongside `content`. Field name varies by version /
    # reasoning parser: newer vLLM uses `reasoning_content`, some builds
    # (and OpenAI's o-series compatibility) use `reasoning`. Accept both.
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})

    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        # Some models return content as an array of parts
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text":
                content_blocks.append({"type": "text", "text": part.get("text", "")})

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        try:
            tool_input = json.loads(fn.get("arguments", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            tool_input = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": fn.get("name", ""),
                "input": tool_input,
            }
        )

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    usage = openai_resp.get("usage") or {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    return {
        "id": openai_resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_alias,
        "content": content_blocks,
        "stop_reason": _map_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Response: OpenAI -> Anthropic (streaming)
# ---------------------------------------------------------------------------

class AnthropicStreamTranslator:
    """Stateful translator that converts OpenAI SSE chunks into Anthropic SSE
    events.

    Usage:
        t = AnthropicStreamTranslator(model_alias)
        for event_str in t.start():
            yield event_str
        for chunk_dict in openai_chunks:
            for event_str in t.handle_chunk(chunk_dict):
                yield event_str
        for event_str in t.finish():
            yield event_str
    """

    def __init__(self, model_alias: str):
        self.model_alias = model_alias
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason: str | None = None
        # Active content block tracking
        self._current_block_index: int = -1
        self._current_block_type: str | None = None  # "thinking" | "text" | "tool_use"
        # Tool call state: map openai tool_call index -> our block index
        self._tool_index_to_block: dict[int, int] = {}
        self._started = False

    # -- Event formatting helpers ------------------------------------------------

    @staticmethod
    def _sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # -- Lifecycle ---------------------------------------------------------------

    def start(self) -> Iterator[str]:
        self._started = True
        yield self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model_alias,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    # Downstream vLLM only reports usage on the final chunk,
                    # so we don't know input_tokens up front. Emit zeros here
                    # with the full Anthropic usage shape; the real counts
                    # are delivered in `message_delta` at the end of the
                    # stream.
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            },
        )

    def handle_chunk(self, chunk: dict[str, Any]) -> Iterator[str]:
        # Capture usage when downstream reports it (typically on the final chunk)
        usage = chunk.get("usage")
        if usage:
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens) or self.input_tokens
            self.output_tokens = usage.get("completion_tokens", self.output_tokens) or self.output_tokens

        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        # Reasoning delta (vLLM `--enable-reasoning`, DeepSeek, etc. emit
        # chain-of-thought as a separate field). Field name varies by
        # version / parser: `reasoning_content` (newer vLLM) vs `reasoning`
        # (some builds). Accept both and map to an Anthropic `thinking`
        # content block so Claude Code renders it in its "Thought" panel.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            yield from self._ensure_thinking_block()
            yield self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current_block_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                },
            )

        # Text delta
        text = delta.get("content")
        if isinstance(text, str) and text:
            yield from self._ensure_text_block()
            yield self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current_block_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )

        # Tool call deltas
        for tc in delta.get("tool_calls") or []:
            tc_idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            name = fn.get("name")
            args_chunk = fn.get("arguments")

            if tc_idx not in self._tool_index_to_block:
                # Start a new tool_use block
                yield from self._close_current_block()
                self._current_block_index += 1
                self._current_block_type = "tool_use"
                self._tool_index_to_block[tc_idx] = self._current_block_index
                yield self._sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._current_block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": name or "",
                            "input": {},
                        },
                    },
                )

            block_idx = self._tool_index_to_block[tc_idx]
            if args_chunk:
                yield self._sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_chunk,
                        },
                    },
                )

        if finish_reason:
            self.stop_reason = _map_stop_reason(finish_reason)

    def finish(self) -> Iterator[str]:
        if not self._started:
            return
        yield from self._close_current_block()
        # Real Anthropic `message_delta` usage carries BOTH input_tokens and
        # output_tokens (plus the cache counters). Claude Code parses this
        # event to update its context-window indicator, so emitting only
        # output_tokens causes its context bar to reset to 0. Include the
        # full shape here for parity.
        yield self._sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": self.stop_reason or "end_turn",
                    "stop_sequence": None,
                },
                "usage": {
                    "input_tokens": self.input_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": self.output_tokens,
                },
            },
        )
        yield self._sse("message_stop", {"type": "message_stop"})

    # -- Internal helpers --------------------------------------------------------

    def _ensure_thinking_block(self) -> Iterator[str]:
        """Open (or keep open) a thinking content block at the current index.

        Anthropic's wire format expects `content_block_start` with
        ``{"type": "thinking", "thinking": ""}`` before any
        ``thinking_delta`` events. Subsequent reasoning deltas can stream into
        the same block.
        """
        if self._current_block_type == "thinking":
            return
        yield from self._close_current_block()
        self._current_block_index += 1
        self._current_block_type = "thinking"
        yield self._sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": self._current_block_index,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        )

    def _ensure_text_block(self) -> Iterator[str]:
        if self._current_block_type == "text":
            return
        yield from self._close_current_block()
        self._current_block_index += 1
        self._current_block_type = "text"
        yield self._sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": self._current_block_index,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def _close_current_block(self) -> Iterator[str]:
        if self._current_block_type is None:
            return
        # Real Anthropic emits a `signature_delta` (base64 crypto signature)
        # before closing a thinking block. vLLM doesn't sign — we send an
        # empty signature, which observation shows Claude Code accepts (the
        # collapsible "Thought" panel renders correctly). Omitting the
        # event entirely would violate the spec's "thinking block must end
        # with signature_delta then stop" rule and risks regressing some
        # clients, so keep the empty-signature compromise.
        if self._current_block_type == "thinking":
            yield self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._current_block_index,
                    "delta": {"type": "signature_delta", "signature": ""},
                },
            )
        yield self._sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": self._current_block_index},
        )
        self._current_block_type = None


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

def anthropic_error(message: str, err_type: str = "api_error") -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": err_type, "message": message},
    }
