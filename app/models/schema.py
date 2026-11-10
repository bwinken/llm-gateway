from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Index, UniqueConstraint, func
from sqlmodel import Field, SQLModel
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


APP_ACCOUNT_PREFIX = "app_"


def is_app_account(username: str) -> bool:
    """Service accounts are named ``app_*`` (the admin panel adds the prefix)."""
    return username.startswith(APP_ACCOUNT_PREFIX)


def app_account_clause():
    """SQL counterpart of ``is_app_account``. ``autoescape`` matters: a bare
    ``startswith("app_")`` compiles to ``LIKE 'app_%'``, where ``_`` is a
    wildcard, so "apple" or "app1" would count as app accounts."""
    return User.username.startswith(APP_ACCOUNT_PREFIX, autoescape=True)


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
    is_admin: bool = Field(default=False)
    is_disabled: bool = Field(default=False)
    can_use_azure: bool = Field(default=False)
    can_use_bedrock: bool = Field(default=False)
    # Exempt from the gateway-wide concurrency limit ([app].concurrency_limit_*,
    # see app/services/concurrency.py). Admins are always exempt. App accounts
    # are created waived (batch jobs legitimately run many requests at once);
    # an admin can un-waive one to limit it.
    concurrency_waived: bool = Field(default=False)
    owner_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    display_name: str = Field(default="")
    org_code: str = Field(default="")
    created_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # When the current API key was issued (NULL: before this was tracked and
    # not recoverable from the key) and when it last authenticated, at most
    # one write per KEY_USE_RESOLUTION_S (deps._record_key_use). Both reset
    # when the key is reissued (issue_api_key). Access reviews read them.
    # Naive UTC, like concurrency_leases (compared in SQL against naive values).
    api_key_created_at: NaiveDatetime | None = Field(default_factory=lambda: _naive_utcnow())
    api_key_last_used_at: NaiveDatetime | None = Field(default=None)


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
    """
    __tablename__ = "concurrency_leases"
    __table_args__ = (
        Index("ix_concurrency_user_expires", "user_id", "expires_at"),
    )

    id: str = Field(primary_key=True)
    user_id: int = Field()
    worker_id: str = Field(index=True)      # "<hostname>:<pid>:<boot token>"
    endpoint: str = Field(default="")
    created_at: NaiveDatetime = Field()
    expires_at: NaiveDatetime = Field()


class UsageLog(SQLModel, table=True):
    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_user_date_cost", "user_id", "created_at", "cost_usd"),
        Index("ix_usage_created_at", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
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
    created_at: NaiveDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UsageDaily(SQLModel, table=True):
    """Per-local-day rollup of ``usage_logs`` (app/services/usage_rollup.py).

    One row per (Asia/Taipei day, user, backend, model, model_type,
    endpoint): a user produces a handful of rows a day however many requests
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
    rule: str = Field()                     # cost_spike | off_hours | burst_rate | behavior_shift | empty_turn_rate
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
    mail (``auto_email``, again every ``email_repeat_days``, 0 = once).
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
    # violation counts as resolved (no notice, no mail) while this is not
    # older than last_seen_at: a later violation reopens it by itself.
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


class ClientMailLog(SQLModel, table=True):
    """Every client-rule mail the gateway tried to send, one row per mail:
    who it was for, the address it went to, the checks it covered
    (``rule_ids``, comma-separated ids), why it went out (``kind``:
    ``requested`` by an admin's click or the monthly reminder, ``automatic``
    for a check's own mail, ``test`` for an admin's test mail) and how it
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
