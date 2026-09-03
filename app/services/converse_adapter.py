"""
OpenAI Chat Completions <-> AWS Bedrock Converse API translation.

The gateway keeps its OpenAI-shaped internal pivot; all Bedrock LLM/VLM
traffic goes through the Converse API (`/model/{modelId}/converse[-stream]`)
so every Bedrock model family (Anthropic, Nova, Llama, Mistral, ...) shares
one code path — the same "collapse to one downstream surface" decision the
Azure path made with the Responses API.

Translation chain for Bedrock traffic:

    client (OpenAI chat)
      → openai_chat_to_converse_request  ── here ──→ Bedrock /converse[-stream]
      ← converse_to_openai_chat_response ─ here ─ Bedrock response

    client (Anthropic Messages)
      → anthropic_to_openai_request → openai_chat_to_converse_request → Bedrock
      ← openai_to_anthropic_response ← converse_to_openai_chat_response ← Bedrock

For streaming, ConverseToChatStreamTranslator turns decoded event-stream
messages into synthetic OpenAI chat-completion chunks, which either go to
the client directly (chat completions surface) or through the existing
AnthropicStreamTranslator (messages surface) — mirroring the Azure
ResponsesToChatStreamTranslator design.

Converse constraints handled here:
  * Conversation roles must strictly alternate user/assistant and start
    with user — consecutive same-role messages are merged, a leading
    assistant turn gets a placeholder user message.
  * Tool results are user-side content (`toolResult` blocks inside a
    user message), tool calls are assistant-side `toolUse` blocks.
  * When any message carries toolUse/toolResult blocks Bedrock requires
    `toolConfig` to be present, so tools are never silently dropped.
  * `reasoning_content` on request history is dropped: Claude on Bedrock
    validates thinking-block signatures, and the gateway's pivot cannot
    produce a valid signature. (Response-direction reasoning IS
    translated back — only the replay of old reasoning is skipped.)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

from app.core.logger import logger
from app.services.reasoning_effort import canonical_effort


# ---------------------------------------------------------------------------
# Request: OpenAI chat completions -> Bedrock Converse
# ---------------------------------------------------------------------------

_DATA_URL_PREFIX = "data:image/"

# reasoning_effort -> Claude thinking budget_tokens (Bedrock
# additionalModelRequestFields). Buckets mirror the Anthropic adapter's
# budget->effort mapping so a Claude Code round trip lands near its origin.
# "minimal" maps to Anthropic's floor (1024); the top levels get a
# genuinely larger budget so a higher setting is felt rather than silently
# clamped. "none" is handled at the call site — it means thinking OFF, not
# a tiny budget. "max" and "xhigh" deliberately share the ceiling: Claude
# has no effort ladder on Converse, only a budget, and budget_tokens has to
# stay under the request's own max_tokens (Claude Code routinely sends
# 32000), so inventing a bigger number for "max" would buy 400s rather than
# more thinking. Every other backend keeps the two levels distinct.
_EFFORT_TO_BUDGET = {
    "minimal": 1024, "low": 2048, "medium": 8192,
    "high": 16384, "xhigh": 32768, "max": 32768,
}


def _image_part_to_converse(part: dict[str, Any]) -> dict[str, Any] | None:
    """OpenAI ``image_url`` part -> Converse ``image`` block.

    Only data URLs are supported (Bedrock wants the bytes inline; over the
    REST JSON surface the ``bytes`` field carries base64). Remote http(s)
    URLs are dropped with a warning — the gateway does not fetch client
    URLs on the model's behalf.
    """
    iu = part.get("image_url")
    url = iu.get("url") if isinstance(iu, dict) else iu
    if not isinstance(url, str):
        return None
    if url.startswith(_DATA_URL_PREFIX):
        try:
            meta, b64 = url.split(",", 1)
            fmt = meta[len(_DATA_URL_PREFIX):].split(";", 1)[0].lower()
            if fmt == "jpg":
                fmt = "jpeg"
            return {"image": {"format": fmt, "source": {"bytes": b64}}}
        except ValueError:
            return None
    logger.warning("Bedrock Converse: dropping non-data-URL image ({}...)", url[:40])
    return None


def _content_to_converse_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            t = part.get("text", "")
            if t:
                blocks.append({"text": t})
        elif ptype == "image_url":
            img = _image_part_to_converse(part)
            if img:
                blocks.append(img)
    return blocks


def _message_to_converse(msg: dict[str, Any]) -> dict[str, Any] | None:
    """One OpenAI message -> one Converse message (role + content blocks).

    Tool messages become user-role messages holding a ``toolResult`` block —
    Converse's required shape. Returns None for messages that translate to
    nothing (e.g. an empty assistant stub).
    """
    role = msg.get("role", "user")

    if role == "tool":
        content = msg.get("content", "")
        out = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        return {
            "role": "user",
            "content": [{
                "toolResult": {
                    "toolUseId": msg.get("tool_call_id", ""),
                    "content": [{"text": out}],
                },
            }],
        }

    blocks = _content_to_converse_blocks(msg.get("content", ""))

    if role == "assistant":
        # NOTE: reasoning_content is intentionally NOT replayed — see module
        # docstring (Bedrock validates thinking signatures we can't produce).
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            try:
                tool_input = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            if not isinstance(tool_input, dict):
                tool_input = {"value": tool_input}
            blocks.append({
                "toolUse": {
                    "toolUseId": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": tool_input,
                },
            })

    if not blocks:
        return None
    return {"role": "assistant" if role == "assistant" else "user", "content": blocks}


def _merge_alternating(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce Converse's strict role alternation.

    Consecutive same-role messages have their content lists concatenated;
    a conversation that would open with an assistant turn gets a minimal
    placeholder user message in front (mirrors the Azure probe placeholder).
    """
    merged: list[dict[str, Any]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append(m)
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"text": "."}]})
    return merged


def _convert_tools(openai_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI ``{type:function, function:{...}}`` -> Converse ``toolSpec``."""
    out: list[dict[str, Any]] = []
    for t in openai_tools or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", {}) or {}
        spec: dict[str, Any] = {
            "name": fn.get("name", ""),
            "inputSchema": {"json": fn.get("parameters") or {"type": "object"}},
        }
        if fn.get("description"):
            spec["description"] = fn["description"]
        out.append({"toolSpec": spec})
    return out


def _convert_tool_choice(tc: Any) -> dict[str, Any] | None:
    if tc == "auto":
        return {"auto": {}}
    if tc == "required":
        return {"any": {}}
    if isinstance(tc, dict) and tc.get("type") == "function":
        name = (tc.get("function") or {}).get("name")
        if name:
            return {"tool": {"name": name}}
    # "none" has no Converse equivalent; toolConfig must stay present when
    # the history contains toolUse blocks, so it degrades to auto (omitted).
    return None


def openai_chat_to_converse_request(
    body: dict[str, Any],
    model_id: str = "",
) -> dict[str, Any]:
    """Translate an OpenAI chat completions body to a Converse request body.

    The Bedrock model ID lives in the URL, not the body — ``model_id`` is
    only consulted for family-specific reasoning knobs (Claude thinking vs.
    gpt-oss reasoning_effort).

    Unlike the Azure path, sampling knobs (`temperature`, `top_p`, `stop`)
    ARE forwarded: Converse normalizes them across model families in
    ``inferenceConfig`` and ignores what a family doesn't support, so there
    is no per-deployment 400 risk to defend against. They are dropped only
    when Claude extended thinking is enabled (Anthropic rejects sampling
    overrides alongside thinking).
    """
    system_parts: list[str] = []
    converse_msgs: list[dict[str, Any]] = []
    for msg in body.get("messages") or []:
        if msg.get("role") in ("system", "developer"):
            sc = msg.get("content", "")
            if isinstance(sc, str) and sc:
                system_parts.append(sc)
            elif isinstance(sc, list):
                for blk in sc:
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                        system_parts.append(blk["text"])
            continue
        cm = _message_to_converse(msg)
        if cm:
            converse_msgs.append(cm)

    converse_msgs = _merge_alternating(converse_msgs)
    if not converse_msgs:
        # Empty after translation (system-only probe) — inject the minimal
        # placeholder so Bedrock returns 200 instead of a validation error.
        converse_msgs = [{"role": "user", "content": [{"text": "."}]}]

    out: dict[str, Any] = {"messages": converse_msgs}
    if system_parts:
        out["system"] = [{"text": "\n\n".join(system_parts)}]

    inference: dict[str, Any] = {}
    max_out = body.get("max_completion_tokens", body.get("max_tokens"))
    if max_out is not None:
        inference["maxTokens"] = max_out
    if body.get("temperature") is not None:
        inference["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        inference["topP"] = body["top_p"]
    stop = body.get("stop")
    if stop:
        inference["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)

    # Fold the xhigh spellings before the bucket lookup: an unrecognized
    # level lands on the medium budget below, so "extra-high" would buy
    # *less* thinking than "xhigh" on this path.
    effort = canonical_effort(body.get("reasoning_effort"))
    if effort:
        mid = (model_id or "").lower()
        if "anthropic" in mid:
            if effort == "none":
                # "none" = thinking off — omit the thinking block entirely
                # (sampling overrides stay usable without extended thinking).
                pass
            else:
                out["additionalModelRequestFields"] = {
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": _EFFORT_TO_BUDGET.get(effort, _EFFORT_TO_BUDGET["medium"]),
                    },
                }
                # Anthropic rejects sampling overrides alongside extended thinking.
                inference.pop("temperature", None)
                inference.pop("topP", None)
        elif "openai" in mid:
            # Verbatim pass-through — the model is the authority on which
            # effort levels it supports (gpt-oss knows low/medium/high;
            # newer OpenAI-family models add minimal/none/xhigh/max).
            out["additionalModelRequestFields"] = {"reasoning_effort": effort}
        # Other families: no portable reasoning knob — rely on model defaults.

    if inference:
        out["inferenceConfig"] = inference

    tools = _convert_tools(body.get("tools") or [])
    if tools:
        tool_config: dict[str, Any] = {"tools": tools}
        tc = _convert_tool_choice(body.get("tool_choice"))
        if tc is not None:
            tool_config["toolChoice"] = tc
        out["toolConfig"] = tool_config

    return out


# ---------------------------------------------------------------------------
# Response: Bedrock Converse -> OpenAI chat completion (non-streaming)
# ---------------------------------------------------------------------------

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "guardrail_intervened": "content_filter",
    "content_filtered": "content_filter",
}


def _finish_reason(stop_reason: str | None, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    return _STOP_REASON_MAP.get(stop_reason or "", "stop")


def _usage_to_openai(usage: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "prompt_tokens": usage.get("inputTokens", 0) or 0,
        "completion_tokens": usage.get("outputTokens", 0) or 0,
        "total_tokens": usage.get("totalTokens", 0) or 0,
    }
    cached = usage.get("cacheReadInputTokens") or 0
    if cached:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    return out


def cached_tokens_from_converse(usage: dict[str, Any]) -> int:
    """Prompt-cache hits, Converse shape (``usage.cacheReadInputTokens``)."""
    return usage.get("cacheReadInputTokens") or 0


def converse_to_openai_chat_response(
    data: dict[str, Any],
    model_alias: str,
) -> dict[str, Any]:
    content_blocks = (
        ((data.get("output") or {}).get("message") or {}).get("content") or []
    )
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("text"):
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"] or {}
            tool_calls.append({
                "id": tu.get("toolUseId") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tu.get("name", ""),
                    "arguments": json.dumps(tu.get("input") or {}, ensure_ascii=False),
                },
            })
        elif "reasoningContent" in block:
            rt = (block["reasoningContent"] or {}).get("reasoningText") or {}
            if rt.get("text"):
                reasoning_parts.append(rt["text"])

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_alias,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _finish_reason(data.get("stopReason"), bool(tool_calls)),
        }],
        "usage": _usage_to_openai(data.get("usage") or {}),
    }


# ---------------------------------------------------------------------------
# Response: Bedrock ConverseStream -> OpenAI chat completion chunks
# ---------------------------------------------------------------------------

# Exception :event-type values Bedrock can emit mid-stream (as event-stream
# messages with :message-type == "exception").
_RETRYABLE_EXCEPTIONS = ("throttling", "serviceunavailable", "modelnotready")


class ConverseToChatStreamTranslator:
    """Convert decoded ConverseStream events into OpenAI chat chunks.

    Mirrors ``ResponsesToChatStreamTranslator``'s surface (start / handle_event
    / finish, token counters, error derivation) so ``bedrock_proxy`` can be a
    near-copy of ``azure_proxy``'s pump loops.

    ``handle_event(event_type, payload)`` takes the event-stream
    ``:event-type`` header (or ``:exception-type`` for exceptions) plus the
    decoded JSON payload.
    """

    def __init__(self, model_alias: str):
        self.model_alias = model_alias
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.finish_reason: str | None = None
        self.emitted_text_chars = 0
        self.emitted_reasoning_chars = 0
        self._block_to_tool_index: dict[int, int] = {}
        self._tool_meta_sent: set[int] = set()
        self._next_tool_index = 0
        self._saw_tool_use = False
        self.event_type_counts: dict[str, int] = {}
        self.error_payloads: list[dict[str, Any]] = []

    def _chunk(
        self,
        delta: dict[str, Any] | None = None,
        finish_reason: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        choice = {"index": 0, "delta": delta or {}, "finish_reason": finish_reason}
        chunk: dict[str, Any] = {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model_alias,
            "choices": [choice],
        }
        if usage is not None:
            chunk["usage"] = usage
        return chunk

    def start(self) -> Iterator[dict[str, Any]]:
        yield self._chunk(delta={"role": "assistant", "content": ""})

    def handle_event(
        self, event_type: str, payload: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        self.event_type_counts[event_type] = self.event_type_counts.get(event_type, 0) + 1

        if event_type == "contentBlockDelta":
            delta = payload.get("delta") or {}
            text = delta.get("text")
            if text:
                self.emitted_text_chars += len(text)
                yield self._chunk(delta={"content": text})
                return
            reasoning = delta.get("reasoningContent") or {}
            rtext = reasoning.get("text")
            if rtext:
                self.emitted_reasoning_chars += len(rtext)
                yield self._chunk(delta={"reasoning_content": rtext})
                return
            tool = delta.get("toolUse") or {}
            frag = tool.get("input")
            block_idx = payload.get("contentBlockIndex")
            if frag and block_idx in self._block_to_tool_index:
                yield self._chunk(delta={
                    "tool_calls": [{
                        "index": self._block_to_tool_index[block_idx],
                        "function": {"arguments": frag},
                    }],
                })

        elif event_type == "contentBlockStart":
            start = payload.get("start") or {}
            tu = start.get("toolUse")
            if tu:
                self._saw_tool_use = True
                block_idx = payload.get("contentBlockIndex")
                if block_idx not in self._block_to_tool_index:
                    self._block_to_tool_index[block_idx] = self._next_tool_index
                    self._next_tool_index += 1
                if block_idx not in self._tool_meta_sent:
                    self._tool_meta_sent.add(block_idx)
                    yield self._chunk(delta={
                        "tool_calls": [{
                            "index": self._block_to_tool_index[block_idx],
                            "id": tu.get("toolUseId", ""),
                            "type": "function",
                            "function": {"name": tu.get("name", ""), "arguments": ""},
                        }],
                    })

        elif event_type == "messageStop":
            self.finish_reason = _finish_reason(
                payload.get("stopReason"), self._saw_tool_use,
            )

        elif event_type == "metadata":
            usage = payload.get("usage") or {}
            self.input_tokens = usage.get("inputTokens", 0) or 0
            self.output_tokens = usage.get("outputTokens", 0) or 0
            self.cached_tokens = cached_tokens_from_converse(usage)

        elif event_type.lower().endswith("exception"):
            # throttlingException / validationException / modelStreamError...
            self.error_payloads.append({"type": event_type, **(payload or {})})

        # messageStart / contentBlockStop: no chunk to emit.

    def finish(self) -> Iterator[dict[str, Any]]:
        usage_payload: dict[str, Any] = {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }
        if self.cached_tokens:
            usage_payload["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}
        yield self._chunk(
            delta={},
            finish_reason=self.finish_reason or "stop",
            usage=usage_payload,
        )

    def derive_error_message(self) -> str | None:
        for payload in self.error_payloads:
            msg = payload.get("message") or payload.get("Message")
            if msg:
                etype = payload.get("type", "")
                return f"{etype}: {msg}" if etype else str(msg)
            if payload.get("type"):
                return str(payload["type"])
        return None

    def derive_error_kind(self) -> str:
        """Map a Bedrock stream exception to an Anthropic-style error_type
        (same retry semantics as the Azure translator: overloaded_error is
        retried by Claude Code, invalid_request_error is surfaced as-is)."""
        for payload in self.error_payloads:
            etype = (payload.get("type") or "").lower()
            if any(s in etype for s in _RETRYABLE_EXCEPTIONS):
                return "overloaded_error"
            if "validation" in etype:
                return "invalid_request_error"
        message = (self.derive_error_message() or "").lower()
        if any(s in message for s in ("throttl", "rate", "too many request")):
            return "overloaded_error"
        if any(s in message for s in ("invalid", "validation", "malformed")):
            return "invalid_request_error"
        return "api_error"
