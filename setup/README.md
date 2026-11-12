# Setup Files

This directory holds the Claude Code installer template served by the gateway, and `gw`, the terminal script. `/setup` (the HTML page) and `/dashboard/install-claude-code.bat` both require SSO login.

## Expected files

| File | Purpose |
|---|---|
| `install-claude-code.bat` | Claude Code installer template; the gateway substitutes `__USER_API_KEY__` per request when serving `GET /dashboard/install-claude-code.bat` |
| `install-claude-code-example.bat` | Committed example to copy from |
| `gw` | Shell script that shows a user's budget, the models' status and their usage in a terminal. Served by `GET /v1/cli` (API key) with the gateway's address written in place of its one placeholder; holds no key |

## Setup

1. Copy `install-claude-code-example.bat` to `install-claude-code.bat` and adjust it for your environment (gateway URL, proxy, Node.js source).
2. Users visit `https://your-gateway/setup` (after SSO login) and download their personalized installer.

## `gw`

Users download it themselves (`curl -fsS https://your-gateway/v1/cli -H "Authorization: Bearer sk-your-api-key" -o ~/.local/bin/gw`), or an administrator installs this file for everyone on a shared host: copy it to `/usr/local/bin/gw` and set `LLM_GATEWAY_URL=https://your-gateway` (for example in `/etc/profile.d/`). It needs `curl`, reads each user's own key from their Claude Code settings or `LLM_GATEWAY_API_KEY`, and passes it to `curl` on standard input so it never shows in `ps`. Commands and behavior: `docs/client-guide.md` → *From a terminal*; design: `docs/architecture.md` → *Account endpoints and terminal views*.

## Notes

- `install-claude-code.bat` is deployment-specific and lives only on the deployed server; only the example is committed.
- It is served via `GET /dashboard/install-claude-code.bat` (auth-required), which replaces the `__USER_API_KEY__` placeholder with the requesting user's key. There is no generic file-download route under `/setup/`.
- The key is written to `ANTHROPIC_AUTH_TOKEN` only, and an `ANTHROPIC_API_KEY` left by an older installer is removed. A deployed `install-claude-code.bat` copied from an earlier example still writes both: change its two lines to match the example. After a user regenerates their key, the dashboard's *My API Key* window offers a script that updates that one variable.
