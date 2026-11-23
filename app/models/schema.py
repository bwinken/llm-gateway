from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Index, UniqueConstraint, func
from sqlmodel import Field, SQLModel, select
from pydantic import NaiveDatetime

# Every timestamp column is `timestamp without time zone` holding UTC, and the
# code reads and compares them as naive values. sqlmodel >= 0.0.48 maps a plain
# `datetime` field to a timezone-aware UTCDateTime (TIMESTAMPTZ DDL, rejects
# naive values on write, returns aware values on read); `NaiveDatetime` keeps
# the plain DateTime(timezone=False) mapping. Use it for every new timestamp.


API_KEY_PREFIX = "sk-internal-"
# 32 random bytes from the OS CSPRNG, hex-encoded: 256 bits. Keys issued before
# this format were ``sk-internal-<unix time>-<8 hex>`` (32 random bits plus a
# guessable timestamp). They keep working: auth is an exact lookup, so the
# format only matters when a key is issued.
_API_KEY_RANDOM_BYTES = 32
API_KEY_LENGTH = len(API_KEY_PREFIX) + 2 * _API_KEY_RANDOM_BYTES
_CURRENT_KEY_RE = re.compile(rf"{re.escape(API_KEY_PREFIX)}[0-9a-f]{{{2 * _API_KEY_RANDOM_BYTES}}}")


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_hex(_API_KEY_RANDOM_BYTES)


def is_legacy_api_key(key: str) -> bool:
    """True for a key not in the current format (older, weaker keys; or one
    set by hand). The dashboard nudges its holder to regenerate it."""
    return not _CURRENT_KEY_RE.fullmatch(key or "")


def legacy_api_key_clause():
    """SQL counterpart of ``is_legacy_api_key`` for the admin filter. Checks
    prefix and length only (portable SQL has no regex); a hand-set key of
    exactly the current length is the one case the two disagree on."""
    return (func.length(User.api_key) != API_KEY_LENGTH) | ~User.api_key.startswith(
        API_KEY_PREFIX, autoescape=True
    )


# Service accounts used to be called app accounts and were told apart by
# this prefix of their name. The prefix decides nothing any more
# (``users.is_service_account`` does): it only marks the names the
# migration turned into service accounts, and the dashboard skips it when
# it picks an account's initials.
LEGACY_SERVICE_PREFIX = "app_"


def has_legacy_service_name(username: str) -> bool:
    """Named the way app accounts were (``app_*``). For display only, never
    a test of what an account is: read ``User.is_service_account`` for that."""
    return username.startswith(LEGACY_SERVICE_PREFIX)


def service_account_clause():
    """SQL test for a service account (``users.is_service_account``). Negate
    it with ``~`` for the people. Never test the name instead: any name can
    be a service account, and ``startswith("app_")`` compiles to
    ``LIKE 'app_%'``, where ``_`` is a wildcard."""
    return User.is_service_account.is_(True)  # type: ignore[attr-defined]


def department_of(account: "User") -> str:
    """The department a service account is for (its ``org_code``, set by an
    admin), or "" when it has none. A person's ``org_code`` is their own
    department, from their sign-in."""
    return (account.org_code or "").strip()


def same_department(a: str | None, b: str | None) -> bool:
    """Two org codes name the same department: compared as the IP
    allowlist compares them, ignoring case and surrounding space."""
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def in_department(person: "User", account: "User") -> bool:
    """May ``person``, an owner of the service account ``account``, see it?
    Yes while the account has no department, or the person's own department
    (``org_code``, kept from their last sign-in) is that one. Someone who
    moved to another department keeps the assignment (only an admin ends
    one) but sees nothing of the account until an admin has looked. The one
    test for it: the dashboard, the key reissue and ``/v1/service-accounts``
    all ask here, and the admin's warning is its negation."""
    department = department_of(account)
    return not department or same_department(person.org_code, department)


def service_account_names(session) -> set[str]:
    """The usernames of every service account (``users`` is small): for code
    that holds usernames and no account, such as the client-rule violations."""
    return set(session.exec(select(User.username).where(service_account_clause())).all())


def mask_api_key(key: str) -> str:
    """Render a key for display: enough to identify it, not enough to use it.

    Admin listings show every account at once, so shipping full keys there
    hands whoever loads the page (or the JSON behind it) every credential in
    the org. The last four characters are enough to match against what a
    user reports; the full key is fetched one account at a time from
    `GET /admin/users/{id}/api-key`.
    """
    if not key:
        return ""
    if len(key) <= 12:
        return "\u2026"
    return f"{key[:11]}\u2026{key[-4:]}"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str = Field(default="")
    api_key: str = Field(index=True, unique=True, default_factory=_generate_api_key)
    daily_limit_usd: float = Field(default=10.0)
    # Retired per-user Azure / Bedrock sub-limits: nothing reads or writes
    # them any more (cloud spend has one cap for everyone,
    # [app].cloud_daily_limit_usd). The columns stay until a migration drops
    # them, so an older release can still run against this database.
    azure_daily_limit_usd: float | None = Field(default=None)
    bedrock_daily_limit_usd: float | None = Field(default=None)
    # This user's cloud cap (Azure + Bedrock together, per day). NULL is not
    # "no cap": it follows [app].cloud_daily_limit_usd, the gateway-wide
    # default (deps.effective_cloud_limit).
    cloud_daily_limit_usd: float | None = Field(default=None)
    # Retired as a stored permission: who is an admin is AuthCenter's to say
    # (the ``admin`` scope of the access token). The column stays, but nothing
    # grants anything from its value: API-key auth clears the attribute
    # (``deps._authenticate``), the admin API refuses the field and no listing
    # shows it. ``get_web_user`` reuses the attribute, for one request, to
    # carry the token's scope to the pages; that may still write the column
    # as a by-product, which changes nothing.
    is_admin: bool = Field(default=False)
    is_disabled: bool = Field(default=False)
    can_use_azure: bool = Field(default=False)
    can_use_bedrock: bool = Field(default=False)
    # Exempt from the gateway-wide concurrency limit ([app].concurrency_limit_*,
    # see app/services/concurrency.py). Admins are always exempt. Service accounts
    # are created waived (batch jobs legitimately run many requests at once);
    # an admin can un-waive one to limit it.
    concurrency_waived: bool = Field(default=False)
    # May this account's local requests be served by a cloud model while its
    # on-prem server is congested (app/services/overflow.py)? True for
    # everyone unless an admin turns it off: the answer to "may this
    # account's prompts leave the premises", separate from can_use_azure /
    # can_use_bedrock (which let the account pick a cloud model itself).
    # Service accounts are never moved, whatever this says.
    can_overflow_cloud: bool = Field(default=True)
    # An account for a program rather than a person: made by an admin, used
    # only by API key (an SSO sign-in under its name is refused), owned by
    # the people in ``app_owners``, left out of the per-person figures and
    # never mailed. Formerly "app accounts", recognised by an ``app_`` name;
    # the name is free now and this column is the only test.
    is_service_account: bool = Field(default=False)
    # A service account that sends requests for the people using it: every
    # request must name one in ``X-On-Behalf-Of`` and is then that person's
    # (their budget, limits and access; billed to them), with this account
    # recorded as the way it came in (``usage_logs.via_user_id``). Off for
    # everyone until an admin turns it on; means nothing on a person's
    # account. See ``deps._on_behalf_of``.
    acts_for_users: bool = Field(default=False)
    # What a service account is, in an admin's words ("HR chat page"): shown
    # beside its name wherever an account sees it, on the owner's dashboard
    # and on the dashboard of a person it sent requests for. One line of
    # text an admin wrote, shown escaped; nothing reads it to decide
    # anything. Empty for people (their name is ``display_name``).
    description: str = Field(default="")
    owner_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    display_name: str = Field(default="")
    # A person's department, from their sign-in (``auth._sync_user``). On a
    # service account it is the department the account is for, set by an
    # admin: its owners see it only while they are in that department
    # (``in_department``). The IP allowlist does not read it there
    # (``ip_allowlist.check``): a rule for a department admits its people.
    org_code: str = Field(default="")
    created_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # When the current API key was issued (NULL: before this was tracked and
    # not recoverable from the key) and when it last authenticated, at most
    # one write per KEY_USE_RESOLUTION_S (deps._record_key_use). Both reset
    # when the key is reissued (issue_api_key). Access reviews read them.
    # Naive UTC, like concurrency_leases (compared in SQL against naive values).
    api_key_created_at: NaiveDatetime | None = Field(default_factory=lambda: _naive_utcnow())
    api_key_last_used_at: NaiveDatetime | None = Field(default=None)


# ── Acting for a user ──
# A request a service account sends for a person (``users.acts_for_users``)
# is authenticated into that person's account. The service account rides
# along on the object for the one request: it is not a column, so it is kept
# outside the model's fields (SQLModel refuses unknown attributes).
_VIA_ATTR = "_via_service_account"


def set_via_service_account(user: User, service_account: User) -> None:
    """Mark ``user`` (a detached, per-request object) as reached through
    ``service_account``."""
    object.__setattr__(user, _VIA_ATTR, service_account)


def via_service_account(user: User) -> User | None:
    """The service account this request came in through when it acts for
    ``user`` (``X-On-Behalf-Of``), else None: the account used its own key."""
    return getattr(user, _VIA_ATTR, None)


def via_user_id(user: User) -> int:
    """``usage_logs.via_user_id`` for a request of ``user``: the service
    account's id, or 0 when the account used its own key."""
    via = via_service_account(user)
    return int(via.id) if via is not None and via.id is not None else 0


# An account endpoint (``/v1/me`` ...) takes an API key or an SSO sign-in
# (``deps.get_account_user``). Which one it was is kept on the object for
# the one request, like the service account above: a signed-in person is
# shown more (the admin link, the masked key, who an application was used
# for), and nothing may show that to a key.
_SSO_ATTR = "_signed_in_by_sso"


def mark_signed_in_by_sso(user: User) -> None:
    """Mark ``user`` (a detached, per-request object) as authenticated by
    an SSO token, not by an API key."""
    object.__setattr__(user, _SSO_ATTR, True)


def signed_in_by_sso(user: User) -> bool:
    """True when this request's account signed in by SSO; False for an API
    key, and for any ``User`` that did not come from the account sign-in."""
    return bool(getattr(user, _SSO_ATTR, False))


# What the service account wrote in the request's ``X-Gateway-Tags`` header
# (``deps._gateway_tags``): its own labels for the request,
# kept for the one request like the service account above and sent to
# Langfuse. The application's text: recorded, never acted on.
_TAGS_ATTR = "_request_tags"


def set_request_tags(user: User, tags: dict[str, str]) -> None:
    """Keep the request's ``X-Gateway-Tags`` entries on ``user`` (a
    detached, per-request object)."""
    object.__setattr__(user, _TAGS_ATTR, dict(tags))


def request_tags(user: User) -> dict[str, str]:
    """The ``X-Gateway-Tags`` entries of the request a service account sent
    for ``user``; empty for any other request."""
    return getattr(user, _TAGS_ATTR, None) or {}


def issue_api_key(user: User) -> str:
    """Give ``user`` a new key: the old one stops working at once."""
    user.api_key = _generate_api_key()
    user.api_key_created_at = _naive_utcnow()
    user.api_key_last_used_at = None
    return user.api_key


class AppOwner(SQLModel, table=True):
    """Many-to-many: which users own which app accounts."""
    __tablename__ = "app_owners"

    id: int | None = Field(default=None, primary_key=True)
    app_id: int = Field(foreign_key="users.id", index=True)
    owner_id: int = Field(foreign_key="users.id", index=True)


class ConcurrencyLease(SQLModel, table=True):
    """One in-flight inference request holding a concurrency slot.

    Inserted when a limited request starts, deleted when its response
    (stream included) has been fully sent. Rows of a worker that died
    without cleaning up are removed by the per-worker sweep (dead pid on
    this host) or, failing that, once ``expires_at`` passes — the owning
    worker pushes it forward every heartbeat while the request is alive.
    See app/services/concurrency.py.

    Timestamps are naive UTC (not the aware values other tables use) so a
    PostgreSQL session TimeZone can never shift them between write and
    compare. No foreign key on ``user_id``: rows are short-lived and the
    user delete path removes them explicitly.

    ``pool`` is which limit the request counts against: ``local`` (vLLM) or
    ``cloud`` (Azure / Bedrock). An account's two counts are independent.
    """
    __tablename__ = "concurrency_leases"
    __table_args__ = (
        Index("ix_concurrency_user_expires", "user_id", "expires_at"),
    )

    id: str = Field(primary_key=True)
    user_id: int = Field()
    worker_id: str = Field(index=True)      # "<hostname>:<pid>:<boot token>"
    endpoint: str = Field(default="")
    pool: str = Field(default="local", sa_column=sa.Column(sa.String, nullable=False, server_default="local"))
    created_at: NaiveDatetime = Field()
    expires_at: NaiveDatetime = Field()


class UsageLog(SQLModel, table=True):
    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_user_date_cost", "user_id", "created_at", "cost_usd"),
        Index("ix_usage_created_at", "created_at"),
    )

    # bigint: one row per request, and a serial number is never reused, so a
    # 32-bit id ends at 2,147,483,647 requests however many rows retention
    # has deleted (and a failed usage write never fails the request, so the
    # gateway would go on serving without billing). SQLite only numbers a
    # column declared INTEGER PRIMARY KEY, hence the variant for the tests.
    id: int | None = Field(
        default=None,
        sa_column=sa.Column(sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
    )
    user_id: int = Field()
    model: str = Field(default="")
    model_type: str = Field(default="")
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cost_usd: Decimal = Field(default=Decimal("0"), sa_column=sa.Column(sa.Numeric(12, 6), nullable=False, default=0))
    endpoint: str = Field(default="")
    # Which downstream served the request: "vllm" (on-prem), "azure", or
    # "bedrock". Lets billing/limits/reports split cloud spend from on-prem
    # spend without parsing the endpoint label.
    backend: str = Field(default="vllm", sa_column=sa.Column(sa.String, nullable=False, server_default="vllm"))
    # The account asked for an on-prem model and the gateway served it from
    # the cloud because that model's server was congested
    # (app/services/overflow.py). ``backend`` / ``model`` / ``cost_usd`` are
    # the cloud model's, as billed; this only says why it went there.
    overflow: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    # The service account that sent this request for ``user_id``
    # (``X-On-Behalf-Of``; ``users.acts_for_users``), 0 when the account used
    # its own key. The row is the person's in every way (bill, limits,
    # reports); this only says which application it came through. No foreign
    # key, like ``user_id``: usage outlives a deleted account.
    via_user_id: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))
    created_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UsageDaily(SQLModel, table=True):
    """Per-local-day rollup of ``usage_logs`` (app/services/usage_rollup.py).

    One row per (Asia/Taipei day, user, backend, model, model_type,
    endpoint, overflow, via_user_id): a user produces a handful of rows a day however many requests
    they make. Written only by ``scripts/rollup_usage_daily.py``, and only
    for closed days; gateway workers never touch it. Reports read closed
    days here and the rest from ``usage_logs``
    (``usage_rollup.aggregate``). ``empty_turns`` counts requests with at
    most one output token (the anomaly scan's empty-turn baseline).

    No foreign key on ``user_id``, like ``usage_logs``: both are the billing
    and audit record and outlive a deleted account (``delete_user`` keeps
    them).
    """
    __tablename__ = "usage_daily"
    __table_args__ = (
        Index("ix_usage_daily_user_day", "user_id", "day"),
    )

    day: date = Field(primary_key=True)
    user_id: int = Field(primary_key=True)
    backend: str = Field(primary_key=True)
    model: str = Field(primary_key=True)
    model_type: str = Field(primary_key=True)
    endpoint: str = Field(primary_key=True)
    # usage_logs.overflow: cloud usage the gateway chose (peak overflow) is
    # kept apart from cloud usage the account chose.
    overflow: bool = Field(default=False, sa_column=sa.Column(
        sa.Boolean, primary_key=True, nullable=False, server_default=sa.false()))
    # usage_logs.via_user_id: usage a service account sent for this user is
    # kept apart from what the user sent with their own key (0).
    via_user_id: int = Field(default=0, sa_column=sa.Column(
        sa.Integer, primary_key=True, nullable=False, server_default="0"))
    requests: int = Field(default=0, sa_column=sa.Column(sa.BigInteger, nullable=False))
    input_tokens: int = Field(default=0, sa_column=sa.Column(sa.BigInteger, nullable=False))
    output_tokens: int = Field(default=0, sa_column=sa.Column(sa.BigInteger, nullable=False))
    cost_usd: Decimal = Field(default=Decimal("0"), sa_column=sa.Column(sa.Numeric(16, 6), nullable=False))
    empty_turns: int = Field(default=0, sa_column=sa.Column(sa.BigInteger, nullable=False))


class UsageRollupState(SQLModel, table=True):
    """Progress of the ``usage_daily`` rollup: every local day up to and
    including ``rolled_through`` is in ``usage_daily``, with no gaps. Readers
    take days after it from ``usage_logs``, so a rollup that never ran or
    fell behind costs speed, never correctness."""
    __tablename__ = "usage_rollup_state"

    name: str = Field(primary_key=True)
    rolled_through: date = Field()
    updated_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScheduledJob(SQLModel, table=True):
    """One row per in-gateway periodic job (app/services/scheduler.py):
    the anomaly scan, usage-log retention.

    A worker runs a job only after claiming it with a conditional UPDATE of
    ``last_started_at`` (``WHERE last_started_at <= now - interval``), so a
    job runs once per interval across every worker and host however many
    try. Timestamps are naive UTC, like ``concurrency_leases``."""
    __tablename__ = "scheduled_jobs"

    name: str = Field(primary_key=True)
    last_started_at: NaiveDatetime | None = Field(default=None)
    last_finished_at: NaiveDatetime | None = Field(default=None)
    last_status: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    last_detail: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))


class OverflowServer(SQLModel, table=True):
    """Peak overflow's view of one on-prem server (app/services/overflow.py):
    its latest waiting queue and how many readings in a row were above the
    upper / below the lower threshold. One row per ``base_url``, updated in
    place by the overflow job each round, so "N readings in a row" holds
    whichever worker runs the round. Never grows."""
    __tablename__ = "overflow_servers"

    base_url: str = Field(primary_key=True)
    waiting: int | None = Field(default=None)
    running: int | None = Field(default=None)
    over_streak: int = Field(default=0)
    under_streak: int = Field(default=0)
    updated_at: NaiveDatetime = Field(default_factory=_naive_utcnow)


class OverflowStay(SQLModel, table=True):
    """One stay of an account on the cloud for one congested on-prem server.

    Inserted when the overflow job moves the account, ``released_at`` set
    when it moves it back; kept afterwards (it answers "why was this account
    on the cloud at 14:05"). While ``released_at`` is NULL and ``expires_at``
    is ahead, the account's requests for that server's models go to each
    model's ``overflow_to``. The job pushes ``expires_at`` forward every
    round: if the job stops, every stay lapses and traffic returns on-prem.

    ``username`` is not a foreign key (like the client-rule tables), so
    ``delete_user`` leaves the rows. Timestamps are naive UTC.
    """
    __tablename__ = "overflow_stays"
    __table_args__ = (
        Index("ix_overflow_stays_open", "released_at", "base_url"),
        Index("ix_overflow_stays_entered", "entered_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    base_url: str = Field()
    # The mode the job ran in when it moved the account: "enforce" (requests
    # really go to the cloud) or "monitor" (recorded only).
    mode: str = Field(default="enforce")
    entered_at: NaiveDatetime = Field(default_factory=_naive_utcnow)
    expires_at: NaiveDatetime = Field(default_factory=_naive_utcnow)
    released_at: NaiveDatetime | None = Field(default=None)
    reason_in: str = Field(default="")
    reason_out: str = Field(default="")


class OverflowBudget(SQLModel, table=True):
    """What peak overflow has spent per local (Asia/Taipei) day: the sum of
    that day's ``usage_logs`` rows marked ``overflow``, written by the
    overflow job each round so the request path never sums it. One row a day."""
    __tablename__ = "overflow_budget"

    day: date = Field(primary_key=True)
    spent_usd: Decimal = Field(default=Decimal("0"), sa_column=sa.Column(sa.Numeric(16, 6), nullable=False, default=0))
    updated_at: NaiveDatetime = Field(default_factory=_naive_utcnow)


class AnomalyEvent(SQLModel, table=True):
    """Findings from the periodic usage-anomaly scan (scripts/scan_anomalies.py).

    One row per (scope, rule, window_start) — the scan upserts, so re-running
    the same window is idempotent and an ongoing anomaly keeps updating one
    row (window_end / observed grow) instead of spawning a row per run.
    `scope` is "user:<id>" for per-account rules and "model:<alias>" for
    model-level rules (empty-turn rate), which keeps the uniqueness key
    non-null; `user_id` / `model` are denormalized copies for joins and
    display. Alert side-channels must fire only when a row is CREATED, never
    on updates, so a long-running anomaly alerts exactly once.
    """
    __tablename__ = "anomaly_events"
    __table_args__ = (
        UniqueConstraint("scope", "rule", "window_start", name="uq_anomaly_scope_rule_window"),
        Index("ix_anomaly_status_created", "status", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field()                    # "user:<id>" | "model:<alias>"
    rule: str = Field()                     # cost_spike | off_hours | burst_rate | behavior_shift | empty_turn_rate | guardrail_unsafe
    severity: str = Field(default="warning")  # info | warning | critical
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    model: str | None = Field(default=None)
    window_start: NaiveDatetime = Field()
    window_end: NaiveDatetime = Field()
    observed: float = Field(default=0.0)
    baseline: float = Field(default=0.0)
    ratio: float = Field(default=0.0)
    detail: str = Field(default="{}")       # JSON: summary + structured context
    status: str = Field(default="new")      # new | acknowledged | resolved
    created_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IpAllowRule(SQLModel, table=True):
    """One entry of the API IP allowlist (app/services/ip_allowlist.py).

    While ``[app].ip_allowlist_mode`` is ``monitor`` or ``enforce``, an API
    request (``/v1/*``, ``/azure/*``, ``/aws/*`` and their unprefixed twins)
    is admitted only when an active rule covers both its address (``cidr``)
    and its account: ``usernames`` and ``org_codes`` both empty means
    everyone; otherwise the account's username must be in ``usernames`` or
    its ``org_code`` in ``org_codes``. A rule is active from ``starts_at``
    (NULL: at once) until ``expires_at`` (NULL: permanent). Only admins
    create rules (admin panel → IP Allowlist); ``label`` describes it.

    ``created_by`` is the admin's username, not a foreign key: an expired
    rule stays as the record of who opened what, and deleting an account
    never has to touch this table. Timestamps are naive UTC, like ``scheduled_jobs``.
    """
    __tablename__ = "ip_allow_rules"

    id: int | None = Field(default=None, primary_key=True)
    cidr: str = Field()                      # normalized network, e.g. "203.0.113.5/32"
    label: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    # Who the rule admits: both empty = every account. Usernames rather than
    # user ids, like created_by, so deleting an account never touches rules.
    usernames: list[str] = Field(
        default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default=sa.text("'[]'")),
    )
    org_codes: list[str] = Field(
        default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default=sa.text("'[]'")),
    )
    created_by: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    starts_at: NaiveDatetime | None = Field(default=None)
    expires_at: NaiveDatetime | None = Field(default=None)
    created_at: NaiveDatetime = Field()


class ClientRuleSetting(SQLModel, table=True):
    """An admin's settings for one client check (app/services/client_checks.py).

    The checks themselves are code: what each one detects, its violation id
    (``rule_id``, e.g. ``CC001``) and its default wording and patch. A row
    here exists only once an admin has saved that check's form, and then
    holds everything the form has: on / off, the action (``notice`` or
    ``remind``), the notice text, the settings patch
    (environment variables for the ``env`` block of settings.json, as JSON:
    the base for every account and what Azure / AWS accounts get on top) and
    the mail's subject. ``auto_email`` and ``email_repeat_days`` are retired:
    a check no longer mails by itself (mail goes out on an admin's click or
    by the monthly reminder), nothing reads or writes them, and the columns
    stay so an older release can still run against this database.
    No row means the check's defaults. A row whose check was retired or
    removed from the code is ignored. Nothing here is part of a check's
    judgment: thresholds are constants in the code too.
    """
    __tablename__ = "client_rule_settings"

    rule_id: str = Field(primary_key=True)
    enabled: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    action: str = Field(default="notice", sa_column=sa.Column(sa.String, nullable=False, server_default="notice"))
    title: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    message: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    patch: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    patch_azure: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    patch_aws: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    email_subject: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    auto_email: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    email_repeat_days: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))
    updated_by: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    updated_at: NaiveDatetime = Field()


class ClientViolation(SQLModel, table=True):
    """One account tripping one client check: the check's violation id
    (``rule_id``, e.g. ``CC001``), when the account first and last did
    (written at most hourly per worker), the Claude Code version it had,
    the local day it last got the in-terminal reminder, and its mail state.

    ``rule_id`` is the id in code, not a foreign key: retiring a check
    leaves its history here. ``username`` is not a foreign key either:
    deleting an account leaves the row, and readers skip names without an
    account. Timestamps are naive UTC.
    """
    __tablename__ = "client_violations"
    __table_args__ = (sa.UniqueConstraint("rule_id", "username", name="uq_client_violations_rule_user"),)

    id: int | None = Field(default=None, primary_key=True)
    rule_id: str = Field(index=True)
    username: str = Field()
    client_version: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    first_seen_at: NaiveDatetime = Field()
    last_seen_at: NaiveDatetime = Field()
    reminded_on: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    mailed_at: NaiveDatetime | None = Field(default=None)
    mail_attempted_at: NaiveDatetime | None = Field(default=None)
    mail_error: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    # When an admin asked for this account to be mailed (a click on the
    # check's mail buttons). The mail job sends it in its next batches; it
    # is done once mailed_at is not older than this.
    mail_requested_at: NaiveDatetime | None = Field(default=None)
    # When the account said "I've applied the fix" on its dashboard. The
    # violation counts as resolved (no notice, no mail, no reminder) until
    # it is seen again more than client_rules.RESOLVED_QUIET_DAYS after
    # this: a violation inside those days only moves last_seen_at.
    resolved_at: NaiveDatetime | None = Field(default=None)


class ClientVersion(SQLModel, table=True):
    """Which client versions an account has used (Claude Code, read from
    the User-Agent of its Messages requests): one row per account and
    version, stamped at most hourly per worker. ``username`` is not a
    foreign key."""
    __tablename__ = "client_versions"
    __table_args__ = (sa.UniqueConstraint("username", "client", "version", name="uq_client_versions_user_client_version"),)

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    client: str = Field()
    version: str = Field()
    first_seen_at: NaiveDatetime = Field()
    last_seen_at: NaiveDatetime = Field()


class ClientEvent(SQLModel, table=True):
    """What a setup tool reported about its user's machine
    (``POST /v1/events``, app/services/client_events.py): which account
    installed, updated, removed or configured what, in which version, on
    which operating system. One row per event; nothing updates a row.

    ``tool`` is the program reporting, ``component`` what the event is
    about and ``kind`` what sort of thing that is: ``app`` (a program of its
    own: ``claude-code``, ``vscode``, the tool itself) or a ``skill`` /
    ``mcp`` / ``plugin`` added to the program ``client`` names. ``os`` is
    ``windows`` / ``linux`` / ``macos``; ``hostname`` is the machine's name
    when the tool sent it (may be ""). ``ok`` false is an
    attempt that failed, with ``message`` saying why. Everything but
    ``username`` and ``created_at`` is what the client sent, validated to
    short slugs and bounded text. ``username`` is not a foreign key, so
    deleting an account leaves its rows. Timestamps are naive UTC.
    """
    __tablename__ = "client_events"
    __table_args__ = (
        Index("ix_client_events_created_at", "created_at"),
        Index("ix_client_events_username_created_at", "username", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: NaiveDatetime = Field(default_factory=_naive_utcnow)
    username: str = Field()
    event: str = Field()
    ok: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.true()))
    tool: str = Field()
    tool_version: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    kind: str = Field(default="app", sa_column=sa.Column(sa.String, nullable=False, server_default="app"))
    component: str = Field()
    client: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    component_version: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    previous_version: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    method: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    os: str = Field()
    os_version: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    arch: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    hostname: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    message: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))


class ModelChange(SQLModel, table=True):
    """A model that is being replaced, as an admin announced it
    (app/services/model_changes.py): the notice people read (``title``,
    ``message``), which model names to replace with which (``renames``, JSON
    ``{old: new}``: the base for every account, and what accounts with Azure
    / AWS access get on top, like a client rule's patch), and whether it is
    shown now (``shown``, a switch an admin turns on and off; there is no
    schedule).

    Who sees it follows from the renames: an account for which they come
    out empty (only the Azure box is filled, and it has no Azure access) is
    not shown the notice and not mailed. Rows are never deleted: the id is
    in mail people received (``MC<id>``) and in the mail log. Timestamps are
    naive UTC.
    """
    __tablename__ = "model_changes"

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    message: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    renames: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    renames_azure: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    renames_aws: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    email_subject: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    shown: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    created_by: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    updated_by: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    created_at: NaiveDatetime = Field(default_factory=_naive_utcnow)
    updated_at: NaiveDatetime = Field(default_factory=_naive_utcnow)


class ModelChangeState(SQLModel, table=True):
    """What one account did about one model change, and its mail state: when
    it pressed *I've switched* (``applied_at``; the notice then stays on its
    dashboard folded, and it is no longer mailed), when an admin asked for
    it to be mailed and how that went. A row exists only once one of those
    happened.

    ``change_id`` and ``username`` are not foreign keys: deleting an account
    leaves the row, and readers skip names without an account. Timestamps
    are naive UTC.
    """
    __tablename__ = "model_change_states"
    __table_args__ = (sa.UniqueConstraint("change_id", "username", name="uq_model_change_states_change_user"),)

    id: int | None = Field(default=None, primary_key=True)
    change_id: int = Field(index=True)
    username: str = Field(index=True)
    applied_at: NaiveDatetime | None = Field(default=None)
    mail_requested_at: NaiveDatetime | None = Field(default=None)
    mail_attempted_at: NaiveDatetime | None = Field(default=None)
    mailed_at: NaiveDatetime | None = Field(default=None)
    mail_error: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))


class ClientMailLog(SQLModel, table=True):
    """Every client-rule mail the gateway tried to send, one row per mail:
    who it was for, the address it went to, the checks it covered
    (``rule_ids``, comma-separated ids: checks as ``CC001``, model changes
    as ``MC3``), why it went out (``kind``: ``requested`` by an admin's
    click or the monthly reminder, ``automatic`` for a check's own mail,
    ``model_change`` for a model change, ``test`` for an admin's test mail)
    and how it
    ended (``ok``, and the error summary when it failed). The admin's
    *Mail log* reads it; nothing updates a row.

    ``username`` is the account the mail was about ("" for a test mail,
    whose ``actor`` is the admin who sent it). Neither is a foreign key, so
    deleting an account leaves its rows. The mail's body is not kept.
    Timestamps are naive UTC.
    """
    __tablename__ = "client_mail_log"
    __table_args__ = (
        Index("ix_client_mail_log_created_at", "created_at"),
        Index("ix_client_mail_log_username", "username"),
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: NaiveDatetime = Field(default_factory=_naive_utcnow)
    username: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    address: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    subject: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    rule_ids: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    kind: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    ok: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    error: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    actor: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))


class ReportSchedule(SQLModel, table=True):
    """When each weekly report mail goes out and to whom
    (app/services/weekly_reports.py): one row per report (``usage``,
    ``guardrail``), written by the admin's *Weekly reports* card. A report
    without a row is off.

    ``weekday`` (0 = Monday) and ``send_time`` (``HH:MM``) are Asia/Taipei;
    ``recipients`` is a comma-separated list of mailboxes of the company
    domain. ``last_period`` is the Monday of the week the last report was
    about: the job claims a report by moving it forward with a conditional
    UPDATE, so one worker sends each week's mail. Timestamps are naive UTC.
    """
    __tablename__ = "report_schedules"

    name: str = Field(primary_key=True)
    enabled: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))
    weekday: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))
    send_time: str = Field(default="09:00", sa_column=sa.Column(sa.String, nullable=False, server_default="09:00"))
    recipients: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    last_period: date | None = Field(default=None)
    last_attempt_at: NaiveDatetime | None = Field(default=None)
    last_status: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    last_detail: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    updated_by: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    updated_at: NaiveDatetime = Field(default_factory=_naive_utcnow)


class AuditLog(SQLModel, table=True):
    """Who changed what through the Web UI / admin, and who read credentials
    or exported data (app/services/audit.py). Append-only: the app never
    updates or deletes a row, and on PostgreSQL a trigger refuses both.

    ``actor`` is a username, not a foreign key, so deleting an account keeps
    its trail. ``detail`` is JSON text: the submitted fields and the JSON
    reply (secrets redacted), the target account, and whatever the handler
    noted (e.g. the model-config diff). Timestamps are naive UTC."""
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_actor_created", "actor", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: NaiveDatetime = Field(default_factory=_naive_utcnow)
    actor: str = Field(default="")       # username from the JWT; "" if unauthenticated
    method: str = Field(default="")
    route: str = Field(default="")       # path template, e.g. /admin/users/{user_id}/limit
    path: str = Field(default="")
    status: int = Field(default=0)       # response status; 500 if the handler raised
    client: str = Field(default="")      # request.client.host
    detail: str = Field(default="{}")
