"""
The shapes of the account endpoints' answers, for ``openapi.json``.

``app/services/account_view.py`` builds each answer as a plain dict. This
module says what is in it, key by key and with its type, so that ``/docs``
shows a schema and a tool or a frontend can generate its types from the
document instead of reading prose.

They describe; they do not filter. A route names its shape in
``responses={200: {"model": ...}}`` and still returns the dict as built:
nothing here runs on a request, so a shape that lags behind an answer cannot
drop a key from it. What keeps the two equal is a test
(``tests/test_account_schema.py``): every real answer must validate against
its shape and come back unchanged, so a key the answer has and the shape
does not, or the other way round, fails there.

How to read one:

- a key is **required** unless it is ``NotRequired``. ``NotRequired`` is how
  "left out when there is no value" is written: the account endpoints never
  answer ``null``, so no key here is nullable.
- a tool must ignore keys it does not know. The answers change by addition,
  so a newer gateway may say more than the shape a tool was built from.
- a date and time is a string in ISO 8601 with its offset
  (``2026-11-23T21:26:30+08:00``); an amount is a number in US dollars.

The two request bodies (``NoticesResolvedRequest``, ``ClientEventRequest``)
are described the same way, from the constants the code validates with
(``client_events``). The validation itself stays where it is.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from app.services import client_events

Backend = Literal["onprem", "azure", "aws"]


# ── GET /v1/me ──

class DailyBudget(TypedDict):
    limit_usd: Annotated[float, Field(description="Today's limit over every backend. 0 is a $0 budget.")]
    used_usd: float
    remaining_usd: float
    requests: int
    input_tokens: int
    output_tokens: int
    enforced: Annotated[bool, Field(description="False: an overage is only logged, not refused.")]
    timezone: Annotated[str, Field(description="The zone a day is counted in: Asia/Taipei.")]
    resets_at: Annotated[str, Field(description="When the day ends (ISO 8601).")]
    resets_in_seconds: int


class BackendBudget(TypedDict):
    access: Annotated[bool, Field(description="May this account reach the backend.")]
    used_usd: float
    remaining_usd: Annotated[float, Field(description="What can still be spent there today; 0 without access.")]


class Backends(TypedDict):
    onprem: BackendBudget
    azure: BackendBudget
    aws: BackendBudget


class CloudBudget(TypedDict):
    limit_usd: float
    used_usd: float
    remaining_usd: float


class OffpeakHours(TypedDict):
    start: Annotated[str, Field(description="HH:MM, Asia/Taipei. Empty when only weekends are off-peak.")]
    end: str
    weekends: bool
    active: Annotated[bool, Field(description="It is off-peak now: the on-prem limit is lifted.")]


class Concurrency(TypedDict):
    mode: Literal["off", "monitor", "enforce"]
    limit: NotRequired[Annotated[int, Field(description="On-prem requests in flight at once. Absent with mode off.")]]
    cloud_limit: NotRequired[Annotated[int, Field(description="The same for Azure and AWS. Only with cloud access.")]]
    offpeak: NotRequired[OffpeakHours]


class MaskedKey(TypedDict):
    masked: Annotated[str, Field(description="The key's start and last four characters. Never the key.")]
    created_at: NotRequired[Annotated[str, Field(description="When it was issued (ISO 8601).")]]


class Site(TypedDict):
    title: str
    api_base: Annotated[str, Field(description="The base URL to show in examples, ending in /v1.")]
    links: Annotated[dict[str, str], Field(description="The admin's links, only the ones that are set.")]
    setup_page: bool
    installer: bool


class Account(TypedDict):
    object: Literal["account"]
    username: str
    display_name: str
    org_code: NotRequired[Annotated[str, Field(description="The department. Absent when the sign-in gave none.")]]
    is_admin: NotRequired[Annotated[bool, Field(
        description="Only for an SSO sign-in: the token has the admin scope. Absent by API key.",
    )]]
    legacy_key: bool
    api_key: MaskedKey
    daily: DailyBudget
    backends: Backends
    cloud: NotRequired[Annotated[CloudBudget, Field(
        description="Azure and AWS together. Absent without cloud access or without a cloud cap.",
    )]]
    cloud_budget_fallback: bool
    concurrency: Concurrency
    site: Site


# ── GET /v1/status ──

class OnpremSummary(TypedDict):
    total: int
    up: int
    down: Annotated[list[str], Field(description="The ids of the on-prem models that are down.")]


class ModelStatus(TypedDict):
    id: Annotated[str, Field(description="The name to send as `model`.")]
    backend: Backend
    type: Annotated[str, Field(description="llm, vlm, embedding, reranker, ...")]
    state: Annotated[Literal["up", "down", "unchecked"], Field(
        description="up / down for on-prem; unchecked for cloud, which the gateway does not probe.",
    )]
    falls_back_to: NotRequired[Annotated[str, Field(description="On a down model: the one that answers instead.")]]
    running: NotRequired[int]
    waiting: NotRequired[int]
    display_name: NotRequired[str]
    context_window: NotRequired[int]
    max_output_tokens: NotRequired[int]
    supports_tools: NotRequired[bool]
    supports_vision: NotRequired[bool]
    supports_prompt_caching: NotRequired[bool]
    is_reasoning: NotRequired[bool]
    input_price_per_1m: float
    output_price_per_1m: float


class Status(TypedDict):
    object: Literal["status"]
    onprem: OnpremSummary
    models: list[ModelStatus]


# ── GET /v1/usage ──

class ModelUsage(TypedDict):
    model: str
    backend: Backend
    type: str
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    overflow: NotRequired[Annotated[bool, Field(
        description="True: a cloud model served in place of a congested on-prem one. Only in the account's own usage.",
    )]]


class UsageSource(TypedDict):
    source: Annotated[str, Field(description="own_key or service_account. Skip a value you do not know.")]
    service_account: NotRequired[str]
    description: NotRequired[str]
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    models: list[ModelUsage]


class UsageDay(TypedDict):
    date: Annotated[str, Field(description="An Asia/Taipei calendar day, YYYY-MM-DD.")]
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class UsageToday(TypedDict):
    date: str
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    models: list[ModelUsage]
    sources: list[UsageSource]


class UsageMonth(TypedDict):
    month: Annotated[str, Field(description="YYYY-MM.")]
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    models: list[ModelUsage]
    sources: list[UsageSource]


class Usage(TypedDict):
    object: Literal["usage"]
    timezone: str
    days: Annotated[list[UsageDay], Field(description="Oldest first, today last; a quiet day is zeros.")]
    today: UsageToday
    month: UsageMonth


# ── GET /v1/service-accounts ──

class ServiceAccountMonth(TypedDict):
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    models: list[ModelUsage]


class OnBehalf(TypedDict):
    users: int
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class SentForRow(TypedDict):
    user: NotRequired[Annotated[str, Field(description="A named user. Absent on the row that folds the rest.")]]
    display_name: NotRequired[str]
    others: NotRequired[Annotated[int, Field(description="How many users this row stands for.")]]
    backend: Backend
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class SentFor(TypedDict):
    users: int
    rows: list[SentForRow]


class ServiceAccount(TypedDict):
    id: int
    username: str
    description: NotRequired[str]
    department: NotRequired[str]
    daily_limit_usd: float
    legacy_key: bool
    month: ServiceAccountMonth
    acts_for_users: bool
    on_behalf: Annotated[OnBehalf, Field(description="What it sent for its users this month. No part of `month`.")]
    api_key_masked: str
    sent_for: NotRequired[Annotated[SentFor, Field(
        description="Only for an SSO sign-in, and only when it sent requests for its users this month.",
    )]]


class WithheldServiceAccount(TypedDict):
    username: str
    department: str


class ServiceAccounts(TypedDict):
    object: Literal["service_accounts"]
    month: str
    service_accounts: list[ServiceAccount]
    withheld: Annotated[list[WithheldServiceAccount], Field(
        description="Assigned to the caller but made for another department: not shown now.",
    )]
    top_users: NotRequired[Annotated[int, Field(
        description="Only for an SSO sign-in: how many users `sent_for` names before it folds the rest.",
    )]]


# ── GET /v1/notices, POST /v1/notices/resolved ──

class Patch(TypedDict):
    set: Annotated[dict[str, str], Field(description="Variables to set in the env block of settings.json.")]
    unset: Annotated[list[str], Field(description="Variables to remove from it.")]


class CombinedPatch(TypedDict):
    set: dict[str, str]
    unset: list[str]
    covers: Annotated[list[str], Field(description="The notice ids this patch fixes.")]
    manual: Annotated[list[str], Field(description="The notice ids no setting can fix.")]


class Notice(TypedDict):
    id: Annotated[str, Field(description="The check, such as CC001.")]
    title: str
    message: NotRequired[str]
    last_seen_at: str
    client_version: NotRequired[str]
    patch: NotRequired[Patch]


class ModelChange(TypedDict):
    id: Annotated[str, Field(description="Such as MC3.")]
    title: str
    message: NotRequired[str]
    models: Annotated[dict[str, str], Field(description="Old model name to new, for this account's tier.")]
    applied: Annotated[bool, Field(description="The account said on its dashboard that it switched.")]


class ModelChangesPatch(TypedDict):
    models: dict[str, str]


class ModelChanges(TypedDict):
    changes: list[ModelChange]
    retiring: list[str]
    patch: ModelChangesPatch


class Notices(TypedDict):
    object: Literal["notices"]
    window_days: int
    quiet_days: int
    notices: list[Notice]
    patch: NotRequired[Annotated[CombinedPatch, Field(description="Absent when nothing is open.")]]
    model_changes: ModelChanges


class NoticesResolved(TypedDict):
    object: Literal["notices.resolved"]
    resolved: Annotated[list[str], Field(description="The ids that were open and are now resolved.")]


class NoticesResolvedRequest(BaseModel):
    """The body is optional: without it everything open is resolved."""

    ids: list[Annotated[str, Field(max_length=20)]] | None = Field(
        default=None, max_length=50, description='The notices to resolve, such as ["CC001"]. Omit for all of them.',
    )


# ── POST /v1/events ──

class ClientEventRequest(BaseModel):
    """One event. Keys the gateway does not know are ignored."""

    event: Literal[*client_events.EVENTS]
    tool: str = Field(max_length=40, description="The reporting tool: lower-case letters, digits, '.', '_', '-'.")
    tool_version: str = Field(max_length=40)
    component: str = Field(max_length=100, description="What it is about, named as it names itself.")
    os: Literal[*client_events.OS_FAMILIES]
    kind: Literal[*client_events.KINDS] = client_events.KINDS[0]
    client: str | None = Field(default=None, max_length=40, description="What a skill, MCP server or plugin was added to.")
    component_version: str | None = Field(default=None, max_length=40)
    previous_version: str | None = Field(default=None, max_length=40)
    method: str | None = Field(default=None, max_length=40)
    arch: str | None = Field(default=None, max_length=40)
    os_version: str | None = Field(default=None, max_length=client_events._MAX_OS_VERSION)
    hostname: str | None = Field(default=None, max_length=100, description="The machine's name.")
    ok: bool = Field(default=True, description="False: the attempt failed.")
    message: str | None = Field(default=None, max_length=client_events._MAX_MESSAGE)


class EventRecorded(TypedDict):
    object: Literal["event"]
    recorded: bool


# ── GET /v1/settings ──

class SettingOption(TypedDict):
    value: str
    label: str
    description: NotRequired[str]
    backend: NotRequired[Annotated[Backend, Field(description="On a model option.")]]


class SettingField(TypedDict):
    name: Annotated[str, Field(description="The variable in the env block of the client's settings.json.")]
    label: str
    description: str
    type: Literal["model", "choice"]
    default: Annotated[str, Field(description="Never empty; always the value of one of the options.")]
    options: list[SettingOption]


class Settings(TypedDict):
    object: Literal["settings"]
    client: str
    fields: list[SettingField]
