"""
OpenAI Chat Completions <-> Azure OpenAI Responses API translation.

The gateway speaks OpenAI chat completions / Anthropic Messages on its
public surface, but newer Azure models (gpt-5 series, o-series pro
variants) only accept the Responses API. This adapter lets the existing
proxy keep its OpenAI-shaped internal pivot while translating to/from
Responses for the actual downstream call.

Translation chain for Azure traffic:

    client (OpenAI chat)
      → openai_chat_to_responses_request  ── here ──→ Azure /openai/v1/responses
      ← responses_to_openai_chat_response ─ here ─ Azure response

    client (Anthropic Messages)
      → anthropic_to_openai_request → openai_chat_to_responses_request → Azure
      ← openai_to_anthropic_response ← responses_to_openai_chat_response ← Azure

For streaming, the chain hands synthetic chat-completion chunk dicts up to
the existing AnthropicStreamTranslator (for /azure/v1/messages) so we don't
need a second Anthropic-aware translator on the Responses side.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Request: OpenAI chat completions -> Azure Responses API
# ---------------------------------------------------------------------------

def _content_to_responses_parts(content: Any, role: str) -> list[dict[str, Any]]:
    """Convert OpenAI chat content into Responses content parts.

    User/system text uses `input_text`; assistant text uses `output_text`.
    Image blocks are only meaningful on user messages.
    """
    text_type = "output_text" if role == "assistant" else "input_text"

    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []

    if not isinstance(content, list):
        return []

    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append({"type": text_type, "text": text})
        elif btype == "image_url" and role != "assistant":
            iu = block.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts


def _message_to_responses_items(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """One OpenAI chat message can produce several Responses input items
    (e.g. assistant message + N function_call items).
    """
    role = msg.get("role", "user")
    content = msg.get("content", "")
    items: list[dict[str, Any]] = []

    if role == "tool":
        out = content if isinstance(content, str) else json.dumps(content)
        items.append({
            "type": "function_call_output",
            "call_id": msg.get("tool_call_id", ""),
            "output": out,
        })
        return items

    # Carry prior reasoning back so multi-turn reasoning is preserved.
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning and role == "assistant":
        items.append({
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        items.append({
            "type": "function_call",
            "call_id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "") or "",
        })

    parts = _content_to_responses_parts(content, role)
    if parts:
        items.append({"role": role, "content": parts})

    return items


def _convert_tools(openai_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI `{type:function, function:{name,parameters}}` -> Responses
    flat form `{type:function, name, parameters}`."""
    out: list[dict[str, Any]] = []
    for t in openai_tools or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", {}) or {}
        out.append({
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return out


def _convert_tool_choice(tc: Any) -> Any:
    if isinstance(tc, str):
        return tc  # "auto" | "none" | "required" — same on both surfaces
    if isinstance(tc, dict) and tc.get("type") == "function":
        name = (tc.get("function") or {}).get("name")
        if name:
            return {"type": "function", "name": name}
    return None


def openai_chat_to_responses_request(
    body: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    """Translate an OpenAI chat completions body to a Responses API body.

    `model` overrides body["model"] when provided (proxy uses this to set
    the Azure deployment name independent of the client-facing alias).
    """
    messages = body.get("messages") or []

    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            sc = msg.get("content", "")
            if isinstance(sc, str) and sc:
                instructions_parts.append(sc)
            elif isinstance(sc, list):
                for blk in sc:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        t = blk.get("text", "")
                        if t:
                            instructions_parts.append(t)
            continue
        input_items.extend(_message_to_responses_items(msg))

    out: dict[str, Any] = {
        "model": model if model is not None else body.get("model", ""),
        "input": input_items,
    }
    if instructions_parts:
        out["instructions"] = "\n\n".join(instructions_parts)

    # Chat completions: `max_tokens` (legacy) or `max_completion_tokens` (reasoning).
    # Responses API: `max_output_tokens`.
    max_out = body.get("max_completion_tokens", body.get("max_tokens"))
    if max_out is not None:
        out["max_output_tokens"] = max_out

    for k in ("temperature", "top_p", "stream", "user"):
        if k in body:
            out[k] = body[k]

    if "stop" in body:
        out["stop"] = body["stop"]

    effort = body.get("reasoning_effort")
    if effort:
        out["reasoning"] = {"effort": effort}

    tools = body.get("tools")
    if tools:
        responses_tools = _convert_tools(tools)
        if responses_tools:
            out["tools"] = responses_tools

    tc = body.get("tool_choice")
    if tc is not None:
        converted_tc = _convert_tool_choice(tc)
        if converted_tc is not None:
            out["tool_choice"] = converted_tc

    return out


# ---------------------------------------------------------------------------
# Response: Azure Responses API -> OpenAI chat completion (non-streaming)
# ---------------------------------------------------------------------------

def _extract_text_and_calls(output_items: list[Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for item in output_items or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    t = part.get("text") or ""
                    if t:
                        text_parts.append(t)
        elif itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "",
                },
            })
        elif itype == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict):
                    t = s.get("text") or ""
                    if t:
                        reasoning_parts.append(t)
            for s in item.get("content") or []:
                if isinstance(s, dict):
                    t = s.get("text") or ""
                    if t:
                        reasoning_parts.append(t)

    return text_parts, reasoning_parts, tool_calls


def _responses_finish_reason(
    data: dict[str, Any],
    has_tool_calls: bool,
) -> str:
    status = data.get("status") or "completed"
    if has_tool_calls:
        return "tool_calls"
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        return "length" if reason == "max_output_tokens" else "stop"
    return "stop"


def responses_to_openai_chat_response(
    data: dict[str, Any],
    model_alias: str,
) -> dict[str, Any]:
    text_parts, reasoning_parts, tool_calls = _extract_text_and_calls(data.get("output") or [])
    finish_reason = _responses_finish_reason(data, bool(tool_calls))

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_in = data.get("usage") or {}
    usage_out: dict[str, Any] = {
        "prompt_tokens": usage_in.get("input_tokens", 0) or 0,
        "completion_tokens": usage_in.get("output_tokens", 0) or 0,
        "total_tokens": usage_in.get("total_tokens", 0) or 0,
    }
    cached = (usage_in.get("input_tokens_details") or {}).get("cached_tokens") or 0
    if cached:
        usage_out["prompt_tokens_details"] = {"cached_tokens": cached}

    return {
        "id": data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": data.get("created_at") or 0,
        "model": model_alias,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage_out,
    }


# ---------------------------------------------------------------------------
# Response: Azure Responses API -> OpenAI chat completion (streaming)
# ---------------------------------------------------------------------------

class ResponsesToChatStreamTranslator:
    """Convert Azure Responses SSE events into OpenAI chat completion chunks.

    The translator is stateful: it tracks the running token counts and the
    mapping from Responses item_id -> tool_calls index so that argument
    deltas attach to the same call shell.

    Usage:
        t = ResponsesToChatStreamTranslator(alias)
        for chunk in t.start():
            yield chunk
        async for resp_event in azure_events:
            for chunk in t.handle_event(resp_event):
                yield chunk
        for chunk in t.finish():
            yield chunk
    """

    def __init__(self, model_alias: str):
        self.model_alias = model_alias
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.finish_reason: str | None = None
        self.saw_completed = False
        self._tool_item_to_index: dict[str, int] = {}
        self._tool_meta_sent: set[str] = set()
        self._next_tool_index = 0
        self._saw_function_call = False

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
            "model": self.model_alias,
            "choices": [choice],
        }
        if usage is not None:
            chunk["usage"] = usage
        return chunk

    def start(self) -> Iterator[dict[str, Any]]:
        yield self._chunk(delta={"role": "assistant", "content": ""})

    def handle_event(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        etype = event.get("type", "")

        if etype == "response.output_text.delta":
            delta = event.get("delta") or ""
            if delta:
                yield self._chunk(delta={"content": delta})

        elif etype in (
            "response.reasoning_summary_text.delta",
            "response.reasoning.delta",
        ):
            delta = event.get("delta") or ""
            if delta:
                yield self._chunk(delta={"reasoning_content": delta})

        elif etype == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self._saw_function_call = True
                item_id = item.get("id") or item.get("call_id") or f"item_{self._next_tool_index}"
                if item_id not in self._tool_item_to_index:
                    self._tool_item_to_index[item_id] = self._next_tool_index
                    self._next_tool_index += 1
                if item_id not in self._tool_meta_sent:
                    self._tool_meta_sent.add(item_id)
                    yield self._chunk(delta={
                        "tool_calls": [{
                            "index": self._tool_item_to_index[item_id],
                            "id": item.get("call_id") or item.get("id") or "",
                            "type": "function",
                            "function": {"name": item.get("name", ""), "arguments": ""},
                        }],
                    })

        elif etype == "response.function_call_arguments.delta":
            item_id = event.get("item_id") or ""
            delta = event.get("delta") or ""
            if item_id and delta and item_id in self._tool_item_to_index:
                yield self._chunk(delta={
                    "tool_calls": [{
                        "index": self._tool_item_to_index[item_id],
                        "function": {"arguments": delta},
                    }],
                })

        elif etype == "response.completed":
            self.saw_completed = True
            resp = event.get("response") or {}
            usage = resp.get("usage") or {}
            self.input_tokens = usage.get("input_tokens", 0) or 0
            self.output_tokens = usage.get("output_tokens", 0) or 0
            self.cached_tokens = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            status = resp.get("status", "completed")
            if self._saw_function_call:
                self.finish_reason = "tool_calls"
            elif status == "incomplete":
                reason = (resp.get("incomplete_details") or {}).get("reason")
                self.finish_reason = "length" if reason == "max_output_tokens" else "stop"
            else:
                self.finish_reason = "stop"

        elif etype in ("response.failed", "response.error", "error"):
            self.saw_completed = True
            self.finish_reason = "stop"

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
