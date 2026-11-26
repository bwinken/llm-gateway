#!/usr/bin/env python3
"""Ask Keycloak who is disabled, by hand.

The gateway asks by itself once ``OFFBOARDING_MODE`` is ``monitor`` or
``enforce`` (app/services/offboarding.py; no timer is needed). This script
is for trying the Keycloak client before switching that on, and for a run
right now.

Without ``--apply`` it reads Keycloak and prints, and writes nothing: it
works while ``OFFBOARDING_MODE`` is still ``off``. It needs the
``OFFBOARDING_KEYCLOAK_*`` settings in ``.env``.

Usage:
    uv run python scripts/offboarding_check.py            # print only
    uv run python scripts/offboarding_check.py --apply    # one run now, in OFFBOARDING_MODE
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.models.schema import User, service_account_clause  # noqa: E402
from app.services import offboarding  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask Keycloak which users are disabled.")
    parser.add_argument("--apply", action="store_true",
                        help="Run once as the job would, in OFFBOARDING_MODE (default: print only).")
    args = parser.parse_args()

    s = offboarding.settings()
    print(f"[offboarding] mode={s['mode']} realm={s['realm_url'] or '(not set)'} client={s['client_id'] or '(not set)'}")
    try:
        disabled = offboarding.read_disabled(s)
    except offboarding.OffboardingError as exc:
        print(f"[offboarding] Keycloak could not be read: {exc}")
        return 1
    print(f"[offboarding] Keycloak reports {len(disabled)} disabled user(s)")

    with Session(engine) as session:
        people = session.exec(select(User).where(~service_account_clause()).order_by(User.username)).all()
        matched = [p for p in people if p.username.strip().lower() in disabled]
        for p in matched:
            state = "already disabled here" if p.is_disabled else "STILL ACTIVE here"
            used = p.api_key_last_used_at.isoformat() + "Z" if p.api_key_last_used_at else "never"
            print(f"  {p.username:<24} {state:<22} key last used: {used}")
        active = sum(1 for p in matched if not p.is_disabled)
        print(f"[offboarding] {len(matched)} of them have an account here ({len(people)} people in all); "
              f"{active} still active")

        if not args.apply:
            print("[offboarding] print only: nothing written (use --apply for a run in OFFBOARDING_MODE)")
            return 0
        if s["mode"] == "off":
            print("[offboarding] OFFBOARDING_MODE is off: nothing to apply")
            return 1
        result = offboarding.reconcile(session, disabled, mode=s["mode"], max_per_run=s["max_per_run"])
        print("[offboarding] " + offboarding.summary(s["mode"], result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
