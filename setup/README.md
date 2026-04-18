# Setup Files

This directory is served publicly by the gateway at `/setup/files/<name>` for users to download.

The `/setup` HTML page instructs users to download the certificate and installation script, then run the script (no admin privileges required).

## Expected files

| File | Required | Purpose |
|---|---|---|
| `llm-gateway-ca.crt` | **yes** | Your internal CA certificate (PEM/DER format, `.crt` extension) |
| `install-cert-user.ps1` | yes | PowerShell installer (included) |
| `install-cert.bat` | optional | Batch installer fallback (included) |

## Setup

1. Place your CA certificate here as `llm-gateway-ca.crt`.
2. The included `install-cert-user.ps1` and `install-cert.bat` install to `CurrentUser\Root` (no admin needed).
3. Users visit `https://your-gateway/setup` and follow the on-screen steps.

## Notes

- This entire directory is **gitignored** — the cert and scripts live only on the deployed server.
- Only files matching the whitelist in `app/routers/web_ui.py` (`_SETUP_ALLOWED`) are downloadable.
- The `/setup` page is publicly accessible (no auth) because users cannot log in via SSO until they trust the certificate.
