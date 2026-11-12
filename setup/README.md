# Setup Files

This directory holds the Claude Code installer template served by the gateway. `/setup` (the HTML page) and `/dashboard/install-claude-code.bat` both require SSO login.

## Expected files

| File | Purpose |
|---|---|
| `install-claude-code.bat` | Claude Code installer template; the gateway substitutes `__USER_API_KEY__` per request when serving `GET /dashboard/install-claude-code.bat` |
| `install-claude-code-example.bat` | Committed example to copy from |

## Setup

1. Copy `install-claude-code-example.bat` to `install-claude-code.bat` and adjust it for your environment (gateway URL, proxy, Node.js source).
2. Users visit `https://your-gateway/setup` (after SSO login) and download their personalized installer.

## Turning it off

The page and the installer are for Windows users. A site whose users work on Linux sets `SETUP_PAGE_ENABLED=false` in `.env`: `/setup` and the installer download answer 404, and every link to them disappears (navbar, welcome page, dashboard). The Install Guide URL an admin sets under Admin → Settings → Site Links is then linked in the navbar, so it can point at that site's own guide.

## Notes

- `install-claude-code.bat` is deployment-specific and lives only on the deployed server; only the example is committed.
- It is served via `GET /dashboard/install-claude-code.bat` (auth-required), which replaces the `__USER_API_KEY__` placeholder with the requesting user's key. There is no generic file-download route under `/setup/`.
- The key is written to `ANTHROPIC_AUTH_TOKEN` only, and an `ANTHROPIC_API_KEY` left by an older installer is removed. A deployed `install-claude-code.bat` copied from an earlier example still writes both: change its two lines to match the example. After a user regenerates their key, the dashboard's *My API Key* window offers a script that updates that one variable.
