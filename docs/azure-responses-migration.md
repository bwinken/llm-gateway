# Migration: Azure path → Responses API

This document covers the operator-facing changes when upgrading to the
release that routes every Azure call through the v1 Responses API
(`/openai/v1/responses`). Pure docs / no required config edits, but a
few behaviors are worth knowing before the rollout.

For end-user client-configuration guidance, see the **Client configuration
recommendations** section in [README.md](../README.md).

---

## What changed

| | Before | After |
|---|---|---|
| Azure URL pattern | `{endpoint}/openai/deployments/{deployment}/{path}?api-version=...` | `{endpoint}/openai/v1/responses` |
| Body's `model` field | Stripped before forwarding | Set to Azure deployment name |
| Downstream response shape | OpenAI chat completions (returned as-is) | Responses API (translated back to chat completions / Anthropic Messages for the client) |
| `/azure/v1/embeddings` | Worked | **Route removed** (Responses API doesn't cover embeddings) |
| `/azure/v1/responses` | — | **New pass-through endpoint** |
| `/azure/responses` | — | **Alias of `/azure/v1/responses`** for clients whose base URL lacks `/v1` |
| Sampling params (`temperature`, `top_p`, `presence_penalty`, `frequency_penalty`) | Forwarded as-is | Stripped on `/azure/v1/chat/completions` and `/azure/v1/messages` (deployments vary in which they accept). NOT stripped on `/azure/v1/responses` — that path is pass-through |
| `reasoning_effort` | Forwarded | Still forwarded, translated to `reasoning.effort` |

## Config changes

None required. Existing `[azure_models.<alias>]` entries continue to
work as-is. Two notes:

- **`api_version` field**: still accepted by the loader for backward
  compatibility, but the v1 Responses surface does not take an
  `api-version` query parameter, so the value is now no-op. Safe to
  leave or remove.
- **`endpoint`**: a trailing `/openai` is stripped automatically. Both
  `https://x.cognitiveservices.azure.com` and
  `https://x.cognitiveservices.azure.com/openai` produce the correct
  downstream URL.

## New environment variable

- `AZURE_INSECURE=true` — disables TLS verification on the Azure-bound
  `httpx` client. Use only when a corporate TLS-inspecting proxy
  re-signs Azure's certificate with a CA that isn't in the system trust
  store. Equivalent to `curl --insecure`. Affects only the Azure
  client; vLLM traffic is unchanged.

The dedicated Azure client is now created whenever **either**
`AZURE_HTTP_PROXY` or `AZURE_INSECURE` is set. When neither is set the
shared client is used (unchanged from before).

## Behavior to know

These show up as `WARNING` log lines. None of them are fatal but
they indicate something about the request you'll want to understand.

### `Empty Responses input after translation — injecting probe placeholder`

Client sent only a system message (or all content collapsed to empty)
— typical Roo Code "validate connection" probe. The gateway injects a
minimal placeholder so Azure returns 200 instead of 400. Expected on
provider-validation pings; if it shows up for actual chat traffic the
client is misconfigured.

### `Dropping N orphan function_call(s) from Responses input`

Conversation history had a `tool_calls` from the assistant but no
matching `role: "tool"` reply (Roo Code "OpenAI Compatible" mode is the
typical trigger — it inlines the tool result as XML text in the next
user message). Azure Responses API strictly validates pairing, so
gateway drops the orphan to keep the conversation moving. The
underlying fix is to use a different Roo Code provider mode (see the
client configuration table in the README).

### `Empty assistant content from Azure stream`

Azure returned 200 + empty stream. Usually because Azure emitted
`response.failed` mid-stream (now surfaced as a proper error event to
the client), or the model truncated mid-reasoning. Look at
`event_types=...`, `error=...`, and `sent_input_summary=...` on the
same log line to diagnose.

### `Azure returned 4xx`

The gateway pre-flights every stream by checking the upstream status
before opening the SSE channel. On 4xx it logs the response body, the
sent body, the incoming body, and a compact `input_summary=` walking
each input item with type / role / call_id. The 4xx is returned to the
client as a structured JSON error (so streaming clients don't see an
empty-but-successful stream).

## Rollback

The change is in code, not config. To roll back, deploy a build from
before commit `74dc195` (`Route all Azure traffic through the Responses
API`). Existing `[azure_models.<alias>]` entries remain compatible with
the previous release — `api_version` re-activates, `/azure/v1/embeddings`
re-opens, `body.model` is stripped again. No data migration in either
direction.

## Smoke-test checklist

After deploying, hit each of the three Azure routes once to confirm
end-to-end:

```bash
GW=http://your-gateway
KEY=sk-your-api-key

# chat completions (most clients)
curl -sS -o /dev/null -w "chat=%{http_code}\n" -X POST \
  "$GW/azure/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"<your-azure-alias>","messages":[{"role":"user","content":"hi"}]}'

# anthropic messages (Claude Code, Roo Code Anthropic provider)
curl -sS -o /dev/null -w "messages=%{http_code}\n" -X POST \
  "$GW/azure/v1/messages" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"<your-azure-alias>","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'

# responses pass-through (OpenAI SDK 1.40+ Responses API)
curl -sS -o /dev/null -w "responses=%{http_code}\n" -X POST \
  "$GW/azure/v1/responses" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"<your-azure-alias>","input":"hi","max_output_tokens":16}'
```

All three should return `200`. A `400` on the first two with
`"input is required"` means the alias is misconfigured (no deployment
or endpoint); a `400` on the third with the same message means the
client sent an empty payload (sanity-check the curl body).
