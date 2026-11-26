#!/usr/bin/env python3
"""Check everything a switch of sign-in to Keycloak depends on, before it.

Run it on the gateway host, with the ``KEYCLOAK_*`` settings already in
``.env`` and ``SSO_PROVIDER`` still what it is: nothing here changes how
people sign in, and nothing is written (not the database, not Keycloak, not
a setting). It reads:

- the settings, and Keycloak's certificate: whether this host trusts it,
  with ``KEYCLOAK_CA_FILE`` when one is set, and if not, which CA signed it;
- the realm: its issuer (the tokens' ``iss``) and its signing keys (JWKS);
- with the leaver check's client (``OFFBOARDING_KEYCLOAK_*``): which accounts
  here Keycloak does not know, and, when that client may also read clients
  and the realm (``view-clients``, ``view-realm``), the sign-in client's
  roles, redirect URI, org-code mapper and the access token's lifespan;
- ``deploy/.env``: what oauth2-proxy must be told at the switch;
- with ``--token``: one person's access token, by the gateway's own check.

Usage:
    uv run python scripts/keycloak_preflight.py
    uv run python scripts/keycloak_preflight.py --token token.txt   # or: --token -   (read from stdin)
    uv run python scripts/keycloak_preflight.py --save-ca keycloak-ca.pem

``--token`` takes a file holding an access token, or the JSON that the admin
console shows under Clients → Client scopes → Evaluate → Generated access
token. Give it as a file or on stdin, never as an argument: a token on the
command line stays in the shell's history.

``--save-ca`` writes the CA certificates the server presents to a PEM file.
They are **not verified**: the script prints each one's SHA-256, which must
be confirmed with whoever runs the CA before the file is trusted.

Exit status: 0 when nothing failed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.core import keycloak  # noqa: E402
from app.core.database import engine  # noqa: E402
from app.services import sso_preflight as preflight  # noqa: E402

_MARK = {
    preflight.OK: "  ok  ", preflight.WARN: " WARN ", preflight.FAIL: " FAIL ",
    preflight.SKIP: " skip ", preflight.TODO: " todo ",
}
_AREAS = {
    "settings": "Settings (.env)",
    "certificate": "Keycloak's certificate",
    "realm": "The realm",
    "oauth2-proxy": "oauth2-proxy (deploy/.env)",
    "accounts": "Accounts",
    "client": "The sign-in client",
    "token": "A real token",
}


def _save_ca(path: str) -> int:
    issuer = keycloak.settings()["issuer"]
    if not issuer:
        print("KEYCLOAK_ISSUER_URL is not set.")
        return 1
    try:
        certs = preflight.ca_certificates_presented(issuer)
    except Exception as exc:
        print(f"The server's certificates could not be read: {type(exc).__name__}: {exc}")
        return 1
    if not certs:
        print("The server sends only its own certificate, no CA certificate: ask whoever runs the CA for it.")
        return 1
    target = Path(path)
    if target.exists():
        print(f"{target} exists: not overwritten.")
        return 1
    target.write_bytes(preflight.pem_bundle(certs))
    print(f"Wrote {len(certs)} CA certificate(s) to {target}. They were NOT verified.")
    print("Confirm each SHA-256 with whoever runs the CA before setting KEYCLOAK_CA_FILE to this file:")
    for cert in certs:
        print(f"  {preflight._cert_name(cert)}")
        print(f"    SHA-256 {preflight._fingerprint(cert)}   expires {preflight._expiry(cert).date()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight for signing in through Keycloak. Reads only.")
    parser.add_argument("--token", metavar="FILE",
                        help="A file with one person's access token, or the JSON payload from Evaluate ('-' = stdin).")
    parser.add_argument("--save-ca", metavar="FILE",
                        help="Write the CA certificates the server presents to FILE (unverified) and stop.")
    args = parser.parse_args()

    if args.save_ca:
        return _save_ca(args.save_ca)

    token_text = ""
    if args.token:
        try:
            token_text = sys.stdin.read() if args.token == "-" else Path(args.token).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"--token: {exc}")
            return 1

    try:
        with Session(engine) as session:
            checks = preflight.run(session, token_text=token_text, repo_root=ROOT)
    except SQLAlchemyError as exc:  # the database is not reachable from here: the rest still says a lot
        print(f"(the gateway's database was not read: {type(exc).__name__}; account checks are left out)\n")
        checks = preflight.run(None, token_text=token_text, repo_root=ROOT)

    area = None
    for check in checks:
        if check.area != area:
            area = check.area
            print(f"\n{_AREAS.get(area, area)}")
        print(f"  [{_MARK[check.status]}] {check.name}: {check.detail}")
        if check.fix:
            print(f"           → {check.fix}")

    counts = {status: sum(1 for c in checks if c.status == status) for status in _MARK}
    print(f"\n{counts[preflight.FAIL]} failed, {counts[preflight.WARN]} to look at, "
          f"{counts[preflight.TODO]} to do at the switch, {counts[preflight.SKIP]} not checked, {counts[preflight.OK]} ok")
    if counts[preflight.FAIL]:
        print("Not ready: fix what failed and run it again.")
        return 1
    if counts[preflight.SKIP]:
        print("Nothing failed, but not everything was checked: read the skipped lines.")
    else:
        print("Nothing failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
