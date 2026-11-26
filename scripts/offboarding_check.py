#!/usr/bin/env python3
"""Ask Keycloak who is disabled, by hand.

The gateway asks by itself once ``OFFBOARDING_MODE`` is ``monitor`` or
``enforce`` (app/services/offboarding.py; no timer is needed). This script
is for trying the Keycloak client before switching that on, and for a run
right now.

Without ``--apply`` it reads Keycloak and prints, and writes nothing: it
works while ``OFFBOARDING_MODE`` is still ``off``. It needs the
``OFFBOARDING_KEYCLOAK_*`` settings in ``.env``.

``--everyone`` lists every user of the realm instead of the disabled ones
only, and then also prints:

- the accounts here that Keycloak does not know by that username. The check
  can never find those when they leave, and after a switch of sign-in to
  Keycloak each of them would sign in to a new, empty account;
- which user attributes exist (names and how many users have each, never a
  value), to find the one that holds the department;
- with ``OFFBOARDING_KEYCLOAK_ORG_ATTR`` set, whose department differs.

Usage:
    uv run python scripts/offboarding_check.py              # print only
    uv run python scripts/offboarding_check.py --everyone   # ... and usernames, attributes, departments
    uv run python scripts/offboarding_check.py --apply      # one run now, in OFFBOARDING_MODE
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
    parser.add_argument("--everyone", action="store_true",
                        help="List every user: also print unknown usernames, attribute names and departments.")
    args = parser.parse_args()

    s = offboarding.settings()
    print(f"[offboarding] mode={s['mode']} realm={s['realm_url'] or '(not set)'} client={s['client_id'] or '(not set)'}")
    everyone = args.everyone or bool(s["org_attr"])
    try:
        reading = offboarding.read(s, everyone=everyone)
    except offboarding.OffboardingError as exc:
        print(f"[offboarding] Keycloak could not be read: {exc}")
        return 1
    disabled = reading.disabled
    print(f"[offboarding] Keycloak reports {len(disabled)} disabled user(s)"
          + (f" of {len(reading.usernames)}" if everyone else ""))

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

        if everyone:
            unknown = [p.username for p in people if p.username.strip().lower() not in reading.usernames]
            print(f"[offboarding] {len(unknown)} account(s) here have no Keycloak user of that name"
                  + (":" if unknown else ""))
            for name in unknown[:50]:
                print(f"  {name}")
            if len(unknown) > 50:
                print(f"  ... and {len(unknown) - 50} more")
            print("[offboarding] user attributes at Keycloak (name: users that have it):")
            for name, count in sorted(reading.attributes.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {name}: {count}")
            if not reading.attributes:
                print("  (none)")
        if reading.departments is not None:
            differ = [
                (p.username, p.org_code or "(none)", reading.departments[p.username.strip().lower()])
                for p in people
                if reading.departments.get(p.username.strip().lower())
                and not offboarding._same_department(p.org_code, reading.departments[p.username.strip().lower()])
            ]
            print(f"[offboarding] attribute '{s['org_attr']}': {len(reading.departments)} user(s) have it; "
                  f"{len(differ)} account(s) here have another department"
                  + (f" (above OFFBOARDING_MAX_PER_RUN={s['max_per_run']}: a run would change none)"
                     if len(differ) > s["max_per_run"] else ""))
            for name, old, new in differ[:50]:
                print(f"  {name:<24} {old} -> {new}")
        elif everyone:
            print("[offboarding] OFFBOARDING_KEYCLOAK_ORG_ATTR is not set: departments are not compared")

        if not args.apply:
            print("[offboarding] print only: nothing written (use --apply for a run in OFFBOARDING_MODE)")
            return 0
        if s["mode"] == "off":
            print("[offboarding] OFFBOARDING_MODE is off: nothing to apply")
            return 1
        result = offboarding.reconcile(session, disabled, mode=s["mode"], max_per_run=s["max_per_run"])
        departments = None
        if reading.departments:
            departments = offboarding.reconcile_departments(
                session, reading.departments, max_per_run=s["max_per_run"],
            )
        print("[offboarding] " + offboarding.summary(s["mode"], result, departments))
    return 0


if __name__ == "__main__":
    sys.exit(main())
