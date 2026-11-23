"""
The API versions this gateway serves and how far along each is: what
``GET /v1/version`` answers.

A frontend or a terminal tool is built against one version of the API and
lives in its own repository, so it is released apart from the gateway. It
asks here, before anything else, whether the version it was built for is
still served:

    stable      served, and changed by addition only. Build against it.
    deprecated  still served, until ``sunset``; move to ``successor``.
    nightly     served for trying out. It may change or go without notice,
                so nothing released is built against it.
    (absent)    not served: the client is too old, or too new, for this
                gateway.

The list is code, like the client checks: a version's status is a promise
about the code in this repository, so it changes in the commit that changes
the code, never by an admin setting. ``v1`` is everything the gateway serves
today: the model API (``/v1/*`` inference, ``/azure/v1/*``, ``/aws/v1/*``),
the account endpoints (``/v1/me``, ``/v1/status``, ...), ``/auth/api-key``
and the dashboard's ``/dashboard/api/*``.

Adding a version: append it as ``nightly`` while it is being built, make it
``stable`` when it is released, and in that same change mark the one before
it ``deprecated`` with the day it stops (``sunset``) and its ``successor``.
Delete an entry in the change that removes its routes, never earlier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The gateway's own release, shown in /docs too (``app.main``).
GATEWAY_VERSION = "1.0.0"

STABLE = "stable"
DEPRECATED = "deprecated"
NIGHTLY = "nightly"
STATUSES: tuple[str, ...] = (STABLE, DEPRECATED, NIGHTLY)


@dataclass(frozen=True)
class ApiVersion:
    version: str
    status: str
    # Deprecated only: the last day it is served (ISO date) and what to move to.
    sunset: str = ""
    successor: str = ""
    # One line for the people reading the answer; optional.
    note: str = ""


VERSIONS: tuple[ApiVersion, ...] = (
    ApiVersion("v1", STABLE),
)


def check(versions: tuple[ApiVersion, ...] = VERSIONS) -> None:
    """Raise ``ValueError`` on a list a client could not rely on. Run at
    import, so a wrong edit stops the start instead of misleading clients."""
    names = [v.version for v in versions]
    if len(set(names)) != len(names):
        raise ValueError("API versions: a version is listed twice.")
    if sum(1 for v in versions if v.status == STABLE) != 1:
        raise ValueError("API versions: exactly one version is stable, the one clients build against.")
    for v in versions:
        if v.status not in STATUSES:
            raise ValueError(f"API version {v.version}: unknown status {v.status!r}.")
        if v.status == DEPRECATED:
            try:
                date.fromisoformat(v.sunset)
            except ValueError:
                raise ValueError(f"API version {v.version}: a deprecated version needs its sunset date (YYYY-MM-DD).")
            if v.successor not in names or v.successor == v.version:
                raise ValueError(f"API version {v.version}: a deprecated version names the version to move to.")
        elif v.sunset or v.successor:
            raise ValueError(f"API version {v.version}: only a deprecated version has a sunset or a successor.")


check()


def stable(versions: tuple[ApiVersion, ...] = VERSIONS) -> str:
    """The version a client should be built against."""
    return next(v.version for v in versions if v.status == STABLE)


def payload(versions: tuple[ApiVersion, ...] = VERSIONS) -> dict:
    """The answer of ``GET /v1/version``. A key without a value is left
    out, never ``null``, as on the account endpoints."""
    return {
        "object": "version",
        "gateway": GATEWAY_VERSION,
        "stable": stable(versions),
        "statuses": list(STATUSES),
        "versions": [
            {
                "version": v.version,
                "status": v.status,
                **({"sunset": v.sunset, "successor": v.successor} if v.status == DEPRECATED else {}),
                **({"note": v.note} if v.note else {}),
            }
            for v in versions
        ],
    }
