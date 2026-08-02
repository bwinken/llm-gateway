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


# OpenAI / Azure reasoning_effort accepts low | medium | high. Anthropic
# clients express reasoning depth two ways:
#   - a top-level `effort` string (low/medium/high, plus Claude Code's
#     extra-high / max which we clamp to high)
#   - `thinking: {"type": "enabled", "budget_tokens": N}` — a token budget
#     we bucket into the three effort levels.
_EFFORT_ALIASES: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra-high": "high",
    "extra_high": "high",
    "max": "high",
    "minimal": "low",
    "none": "low",
}


def _map_reasoning_effort(body: dict[str, Any]) -> str | None:
    """Derive an OpenAI `reasoning_effort` value from an Anthropic request.

    Returns None when the request expresses no reasoning preference, so the
    caller leaves `reasoning_effort` unset and the downstream uses its own
    default. Downstreams without a reasoning_effort knob (e.g. Qwen3) simply
    ignore the field — translating it here is harmless and future-proofs the
    Azure o-series / any model that does honour it.
    """
    effort = body.get("effort")
    if isinstance(effort, str) and effort.strip():
        return _EFFORT_ALIASES.get(effort.strip().lower(), "high")

    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens")
        if isinstance(budget, (int, float)) and budget > 0:
            # Anthropic's minimum budget is 1024; bucket conservatively.
            if budget <= 4096:
                return "low"
            if budget <= 16384:
                return "medium"
            return "high"
    return None


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


def _wrap_system_reminder(text: str) -> str:
    """Wrap mid-conversation system text the way Claude Code embeds it in user
    messages when talking to the official API. Text that already carries the
    tag is left untouched."""
    if "<system-reminder>" in text:
        return text
    return f"<system-reminder>\n{text}\n</system-reminder>"


def _merge_text_into_message(
    msg_out: dict[str, Any], text: str, prepend: bool = False
) -> None:
    """Splice plain text into an already-built OpenAI message's content,
    handling both the string and content-blocks shapes."""
    content = msg_out.get("content", "")
    if isinstance(content, list):
        block = {"type": "text", "text": text}
        if prepend:
            content.insert(0, block)
        else:
            content.append(block)
        return
    existing = content if isinstance(content, str) else ""
    if not existing:
        msg_out["content"] = text
    elif prepend:
        msg_out["content"] = f"{text}\n\n{existing}"
    else:
        msg_out["content"] = f"{existing}\n\n{text}"


def normalize_anthropic_messages(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw Anthropic request for the native pass-through path.

    The official Anthropic schema only allows ``user``/``assistant`` roles
    inside ``messages``, but some Claude Code builds inject IDE events / task
    reminders as ``role: "system"`` entries. Sent verbatim to a native
    downstream those either get rejected outright or rendered mid-template
    (strict templates like Qwen3.x raise "System message must be at the
    beginning"). This applies the same policy as the translation path, but in
    Anthropic shape:

      - system/developer entries BEFORE the first real turn join the
        top-level ``system`` field (appended as text blocks, preserving any
        cache_control blocks already there);
      - MID-conversation entries are merged into the adjacent user message as
        ``<system-reminder>`` text (appended to a preceding user message,
        otherwise prepended to the next one; a trailing orphan becomes its
        own user message) — the shape Claude Code itself uses against the
        official API, preserving temporal order and never touching the
        index-0 content (downstream prefix cache stays valid).

    Returns the body unchanged (same object) when ``messages`` is already
    clean, so the common case stays a pure pass-through. Never mutates the
    input; dirty requests get copied message dicts.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(m, dict) and m.get("role") in ("system", "developer")
        for m in messages
    ):
        return body

    def _as_blocks(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, list):
            return list(content)
        return [{"type": "text", "text": content if isinstance(content, str) else ""}]

    system_blocks: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        system_blocks.append({"type": "text", "text": system})
    elif isinstance(system, list):
        system_blocks.extend(system)

    out_messages: list[dict[str, Any]] = []
    pending: list[str] = []
    in_conversation = False

    for msg in messages:
        if not isinstance(msg, dict):
            out_messages.append(msg)
            continue
        role = msg.get("role")
        if role in ("system", "developer"):
            text = _content_to_openai_text(msg.get("content", ""))
            if not text:
                continue
            if not in_conversation:
                system_blocks.append({"type": "text", "text": text})
                continue
            reminder = _wrap_system_reminder(text)
            last = out_messages[-1] if out_messages else None
            if last is not None and last.get("role") == "user":
                blocks = _as_blocks(last.get("content", ""))
                blocks.append({"type": "text", "text": reminder})
                last["content"] = blocks
            else:
                pending.append(reminder)
            continue

        in_conversation = True
        new_msg = dict(msg)
        if role == "user" and pending:
            blocks = _as_blocks(new_msg.get("content", ""))
            for i, reminder in enumerate(pending):
                blocks.insert(i, {"type": "text", "text": reminder})
            pending.clear()
            new_msg["content"] = blocks
        out_messages.append(new_msg)

    if pending:
        out_messages.append({
            "role": "user",
            "content": [{"type": "text", "text": r} for r in pending],
        })

    out = dict(body)
    out["messages"] = out_messages
    if system_blocks:
        out["system"] = system_blocks
    return out


def anthropic_to_openai_request(
    body: dict[str, Any],
    is_reasoning: bool = False,
) -> dict[str, Any]:
    """Translate an Anthropic /v1/messages request body into an OpenAI
    /v1/chat/completions request body.

    `is_reasoning` should be the resolved model's `is_reasoning` metadata
    flag. It gates `reasoning_effort` injection: only models explicitly
    marked as reasoning models receive the parameter, so a non-reasoning
    downstream (e.g. a plain Azure gpt-4o deployment) never gets an
    `reasoning_effort` field it would reject with a 400.
    """
    openai_messages: list[dict[str, Any]] = []

    # Conversation-LEADING system content (the top-level "system" field plus
    # any system/developer entries that precede the first real turn) is
    # collected here and emitted as a SINGLE message at index 0 — strict
    # downstream chat templates (e.g. Qwen3.x) raise
    # "System message must be at the beginning" for anything else.
    #
    # MID-conversation system entries (newer Claude Code injects IDE events /
    # task reminders as `role: "system"` inside `messages`) are NOT hoisted to
    # the front: that would tear them out of their temporal position and, worse,
    # mutate the index-0 system message on every turn, invalidating the
    # downstream's prefix cache for the whole history. Instead they are merged
    # in place into the adjacent user message as <system-reminder> text — the
    # exact shape Claude Code itself uses against the official Anthropic API —
    # preserving ordering and strict role alternation.
    system_parts: list[str] = []

    # Mid-conversation reminders waiting for the next user message (used when
    # the preceding message isn't a user turn we can append to).
    pending_reminders: list[str] = []
    in_conversation = False

    # System prompt — Anthropic uses a top-level "system" field (string or list)
    system = body.get("system")
    if system:
        text = _content_to_openai_text(system)
        if text:
            system_parts.append(text)

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # System / developer role messages: leading ones join the single
        # index-0 system message; mid-conversation ones are merged into the
        # adjacent user message (see system_parts comment above).
        if role in ("system", "developer"):
            text = _content_to_openai_text(content)
            if not text:
                continue
            if not in_conversation:
                system_parts.append(text)
                continue
            reminder = _wrap_system_reminder(text)
            last = openai_messages[-1] if openai_messages else None
            if last is not None and last.get("role") == "user":
                _merge_text_into_message(last, reminder)
            else:
                pending_reminders.append(reminder)
            continue

        in_conversation = True

        # Simple string content -> direct mapping
        if isinstance(content, str):
            msg_out = {"role": role, "content": content}
            if role == "user" and pending_reminders:
                _merge_text_into_message(
                    msg_out, "\n\n".join(pending_reminders), prepend=True
                )
                pending_reminders.clear()
            openai_messages.append(msg_out)
            continue

        if not isinstance(content, list):
            continue

        # Split content blocks: regular blocks vs tool_use/tool_result/thinking
        regular_blocks: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[tuple[str, str]] = []  # (tool_use_id, content)
        thinking_parts: list[str] = []

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
            elif btype == "thinking":
                # Preserve an assistant turn's reasoning across multi-turn
                # conversations: carry it back as `reasoning_content` (the
                # field vLLM emits it in). Whether the downstream actually
                # re-injects it into the prompt depends on its chat template
                # (e.g. Qwen3's preserve_thinking, set at vLLM startup) —
                # the gateway just makes sure the data isn't lost.
                t = block.get("thinking", "")
                if isinstance(t, str) and t:
                    thinking_parts.append(t)
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
        if regular_blocks or tool_calls or thinking_parts or not tool_results:
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
            if thinking_parts:
                # Send BOTH field names: vLLM >= 0.20 only reads `reasoning`
                # on incoming assistant messages (it renamed the field and
                # silently drops `reasoning_content` — vllm-project/vllm
                # #38488), while older vLLM / DeepSeek-style downstreams read
                # `reasoning_content`. Emitting both keeps historical thinking
                # alive across every downstream; each ignores the name it
                # doesn't know.
                joined_thinking = "".join(thinking_parts)
                msg_out["reasoning_content"] = joined_thinking
                msg_out["reasoning"] = joined_thinking
            # Skip empty placeholders left over from pure tool_result messages
            if (msg_out["content"] == "" and not tool_calls
                    and not thinking_parts and tool_results):
                continue
            if role == "user" and pending_reminders:
                _merge_text_into_message(
                    msg_out, "\n\n".join(pending_reminders), prepend=True
                )
                pending_reminders.clear()
            openai_messages.append(msg_out)

    # Reminders with no user message left to attach to (e.g. a trailing system
    # entry) become their own user message so they are not silently dropped.
    if pending_reminders:
        openai_messages.append(
            {"role": "user", "content": "\n\n".join(pending_reminders)}
        )

    # Emit the consolidated leading system content as the single index-0 message.
    if system_parts:
        openai_messages.insert(
            0, {"role": "system", "content": "\n\n".join(system_parts)}
        )

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

    # Reasoning effort — Anthropic `effort` / `thinking.budget_tokens` mapped
    # to OpenAI's `reasoning_effort`. Only emitted for models flagged
    # `is_reasoning` in config so non-reasoning downstreams never receive a
    # parameter they might reject.
    if is_reasoning:
        effort = _map_reasoning_effort(body)
        if effort is not None:
            openai_body["reasoning_effort"] = effort

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
# Empty / near-empty turn diagnostics
# ---------------------------------------------------------------------------

def summarize_request_shape(body: dict[str, Any]) -> str:
    """Compact, log-friendly summary of an Anthropic request's message history.

    Surfaces the signals that distinguish the leading hypotheses for a
    silent empty turn (out<=1):
      - ``asst_thinking`` — assistant turns carrying a ``thinking`` block
        (how much reasoning history is being fed back, e.g. under
        ``preserve_thinking``)
      - ``asst_empty``    — assistant turns with NO visible text and NO
        tool_use (the degenerate "said nothing" turns that, once present,
        can re-demonstrate an empty answer to the model on the next turn)
    """
    messages = body.get("messages") or []
    n_assistant = 0
    n_thinking = 0
    n_empty = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        n_assistant += 1
        content = msg.get("content", "")
        has_text = False
        has_tool = False
        has_thinking = False
        if isinstance(content, str):
            has_text = bool(content.strip())
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    has_text = True
                elif btype == "tool_use":
                    has_tool = True
                elif btype == "thinking" and block.get("thinking", "").strip():
                    has_thinking = True
        if has_thinking:
            n_thinking += 1
        if not has_text and not has_tool:
            n_empty += 1
    return (
        f"msgs={len(messages)} asst={n_assistant} "
        f"asst_thinking={n_thinking} asst_empty={n_empty}"
    )


def empty_turn_warning(
    model_alias: str,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str | None,
    text_chars: int,
    thinking_chars: int,
    req_shape: str,
) -> str | None:
    """Build a diagnostic line for a clean-finish but empty turn, or None.

    Returns a message only when ``output_tokens <= 1`` — the "silent stop"
    Claude Code shows. ``text_chars`` / ``thinking_chars`` distinguish a
    truly-empty turn (both 0 → model emitted only EOS) from a thinking-only
    one; ``req_shape`` (see ``summarize_request_shape``) shows whether the
    request history was carrying empty / thinking assistant turns.
    """
    if output_tokens > 1:
        return None
    return (
        f"Empty/near-empty turn | model={model_alias} "
        f"in={input_tokens} out={output_tokens} stop={stop_reason} "
        f"text_chars={text_chars} thinking_chars={thinking_chars} {req_shape}"
    )


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
# Observability I/O capture (Anthropic-shape) — see observability.py / proxies
# ---------------------------------------------------------------------------

def anthropic_request_io(body: dict[str, Any]) -> Any:
    """Build the Anthropic-shape input payload for Langfuse I/O capture.

    The proxies translate the incoming Anthropic request to the gateway's
    internal OpenAI pivot before forwarding; recording that pivot would show an
    OpenAI conversation in the trace of an Anthropic endpoint. This returns the
    original Anthropic request's salient fields instead, so the trace is a
    faithful Anthropic record. Returns the bare ``messages`` list when there is
    no ``system`` / ``tools`` (renders as a conversation), otherwise a dict that
    keeps them alongside the messages.
    """
    messages = body.get("messages")
    system = body.get("system")
    tools = body.get("tools")
    if system is None and not tools:
        return messages
    payload: dict[str, Any] = {"messages": messages}
    if system is not None:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    return payload


def openai_message_to_anthropic(
    msg: dict[str, Any] | None, model_alias: str = "",
) -> dict[str, Any] | None:
    """Convert an accumulated OpenAI assistant message (from
    ``StreamingChatOutput.as_message()``) to an Anthropic-shape assistant
    message, so streamed-output I/O capture records the same Anthropic content
    blocks as the non-stream path. Reuses ``openai_to_anthropic_response`` for
    the block-building. Returns None when nothing was captured.
    """
    if not msg:
        return None
    anthropic = openai_to_anthropic_response({"choices": [{"message": msg}]}, model_alias)
    return {"role": "assistant", "content": anthropic["content"]}


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
        # Visible-output accounting — lets the empty-turn diagnostic tell a
        # truly-empty turn (model emitted only EOS) from a thinking-only one.
        self.text_chars = 0
        self.thinking_chars = 0
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
            self.thinking_chars += len(reasoning)
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
            self.text_chars += len(text)
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

    def fail(self, message: str, error_type: str = "overloaded_error") -> Iterator[str]:
        """Close any open content block and emit an Anthropic `error` event.

        Used when the downstream stream ended *without* a finish_reason — the
        connection dropped mid-generation. We must NOT emit a normal
        `message_stop` in that case: it tells the client the turn completed
        successfully, so a truncated answer is silently accepted.

        Defaults to `overloaded_error` because a mid-stream truncation is
        almost always downstream overload (queued vLLM batches, KV-cache
        pressure). Anthropic SDK clients — Claude Code included — retry
        `overloaded_error` with exponential backoff, so the user sees a brief
        pause and an automatic retry rather than a broken, half-finished turn.
        """
        if not self._started:
            return
        yield from self._close_current_block()
        yield self._sse(
            "error",
            {
                "type": "error",
                "error": {"type": error_type, "message": message},
            },
        )

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
        # before closing a thinking block, but vLLM-emitted reasoning has no
        # signature to forward. Empirically (cf. LiteLLM's adapter, which
        # only emits signature_delta when the upstream chunk carries a
        # non-empty signature string), strict Claude Code builds validate
        # that signature looks like real base64 — an empty value causes the
        # thinking block to be rejected and the stream to stall. We skip
        # the event when no signature exists and let `content_block_stop`
        # close the block on its own.
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
