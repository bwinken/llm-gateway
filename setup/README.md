# Setup Files

This directory is served by the gateway at `/setup/files/<name>` for users to download. Both `/setup` (the HTML page) and `/setup/files/<name>` require SSO login — nginx no longer bypasses oauth2-proxy for `/setup`.

The `/setup` HTML page instructs users to download the certificate and installation script, then run the script (no admin privileges required). It is meant for installing the gateway's CA cert so Claude desktop / Office trusts the HTTPS endpoint — **not** for installing Claude Code (that lives on the Dashboard).

## Expected files

| File | Whitelisted for download? | Purpose |
|---|---|---|
| `llm-gateway-ca.crt` | **yes** | Your internal CA certificate (PEM/DER format, `.crt` extension) |
| `install-cert.bat` | **yes** | Batch installer (offered to users) |
| `install-cert-user.ps1` | no | PowerShell equivalent — kept here for ops to run manually, but not downloadable from the UI |
| `install-claude-code.bat` | no (served via personalized endpoint) | Claude Code installer template; the gateway substitutes `__USER_API_KEY__` per request when serving `GET /dashboard/install-claude-code.bat` |

## Setup

1. Place your CA certificate here as `llm-gateway-ca.crt`.
2. The included `install-cert.bat` installs to `CurrentUser\Root` (no admin needed). `install-cert-user.ps1` is the PowerShell equivalent for ops.
3. Users visit `https://your-gateway/setup` (after SSO login) and follow the on-screen steps.

## Notes

- This entire directory is **gitignored** — the cert and scripts live only on the deployed server.
- Only files matching the whitelist in `app/routers/web_ui.py` (`_SETUP_ALLOWED`) are downloadable. The user-facing UI offers only `install-cert.bat`; the `.ps1` variant remains in this directory for ops use but is not exposed for download.
- `install-claude-code.bat` is served via `GET /dashboard/install-claude-code.bat` (auth-required) which replaces the `__USER_API_KEY__` placeholder with the requesting user's key.
