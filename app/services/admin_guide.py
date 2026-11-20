"""The admin *Guide* page: for every setting an admin can change, what it
does, why it exists and what was traded for what.

The settings have grown past what a tooltip can carry, and the reason behind
a default is the first thing lost: this is where it is kept, next to the
panel that changes it. Pure data and look-ups: no database, no request.

Keep it true. An entry is added or changed in the same commit as the
setting it describes (``tests/test_admin_guide.py`` fails when an ``[app]``
key of the Limits tab has no entry, or a card links to an entry that is not
here). ``why`` and ``tradeoffs`` say what is in ``CLAUDE.md`` /
``docs/architecture.md`` or what the owner decided, in plain words; they do
not guess. ``decided`` marks a choice the owner made, as opposed to one the
code forces.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entry:
    id: str                     # anchor on the page: /admin/guide#<id>
    name: str
    what: str
    why: str
    tradeoffs: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()  # where it is stored (config.toml key, column)
    default: str = ""
    decided: str = ""           # an owner's decision, in a line


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    where: str                  # where in the panel it is changed
    href: str                   # link to that place
    intro: str
    entries: tuple[Entry, ...] = field(default_factory=tuple)


SECTIONS: tuple[Section, ...] = (
    Section(
        id="concurrency",
        title="Concurrency limit",
        where="Limits → Concurrency Limit",
        href="/admin#limits",
        intro=(
            "How many inference requests one account may have in flight at once. It exists because the "
            "on-prem GPUs are shared: one account running many agents in parallel fills the queue for everyone."
        ),
        entries=(
            Entry(
                id="concurrency-mode",
                name="Mode",
                keys=("[app].concurrency_limit_mode",),
                default="off",
                what="Off: nothing is counted. Monitor: over-limit requests are served and logged. Enforce: they get a 429 with Retry-After: 5.",
                why="Monitor lets you see who would be limited, and how often, before anyone is refused.",
                tradeoffs=(
                    "\"Unlimited\" is the mode Off, never a limit of 0: a limit is always a real number, so switching back on cannot come up with a missing one.",
                    "If the lease store cannot be reached, requests are served (it fails open). A database problem must not become an outage; the cost is that the limit does not hold during one.",
                    "Only chat, responses and messages are counted. Token counting, embeddings and rerank are light and are not.",
                ),
            ),
            Entry(
                id="concurrency-local",
                name="Local limit",
                keys=("[app].concurrency_limit",),
                default="8",
                what="In-flight requests to on-prem models, per account.",
                why="This is the limit that protects the shared GPUs.",
                tradeoffs=(
                    "Lower is fairer between accounts and slower for one person running many agents; higher is the reverse.",
                    "A cloud model served on-prem because the cloud budget is spent counts here: it loads the GPUs.",
                ),
            ),
            Entry(
                id="concurrency-cloud",
                name="Cloud limit",
                keys=("[app].concurrency_cloud_limit",),
                default="10",
                what="In-flight requests to Azure / AWS models, per account, counted apart from local ones.",
                why="A cloud backend has no shared-GPU contention, so it gets its own, wider limit. A full local count neither borrows from nor blocks the cloud one.",
                tradeoffs=(
                    "It still exists: without it one account could open any number of paid cloud requests at once.",
                    "A request moved to the cloud by peak overflow counts here, not as local.",
                ),
            ),
            Entry(
                id="concurrency-offpeak",
                name="Off-peak hours",
                keys=("[app].concurrency_offpeak_start", "[app].concurrency_offpeak_end", "[app].concurrency_offpeak_weekends"),
                default="none",
                what="During these hours (Asia/Taipei) local requests are not held to the local limit.",
                why="At night and on weekends the GPUs are idle; holding batch work to the daytime limit wastes them.",
                tradeoffs=(
                    "Local requests still take their slot off-peak, they are only never refused: the count is then true the minute the window closes.",
                    "It lifts the local limit only. Cloud keeps its limit around the clock, because the reason (idle GPUs) does not apply there.",
                    "A broken setting keeps the limit rather than lifting it.",
                ),
            ),
            Entry(
                id="concurrency-waive",
                name="Waive (per account)",
                keys=("users.concurrency_waived",),
                default="people: limited · service accounts: waived",
                what="An account with Waive is never limited.",
                why="Batch jobs legitimately run many requests at once, so service accounts are created waived.",
                tradeoffs=(
                    "Nothing else exempts an account: being an admin does not (an API key carries no admin right).",
                    "Un-waive a service account to limit it; the Special access strip counts every account that differs from its kind's default.",
                ),
            ),
        ),
    ),
    Section(
        id="spending",
        title="Spending limits",
        where="Limits → Default Daily Limit, Cloud Daily Limit, Cloud Budget Fallback",
        href="/admin#limits",
        intro=(
            "Every account has a daily budget in USD, resetting at Asia/Taipei midnight. On-prem prices are "
            "notional (they make usage comparable); cloud prices are what the provider charges."
        ),
        entries=(
            Entry(
                id="default-daily-limit",
                name="Default daily limit",
                keys=("[app].default_daily_limit_usd", "users.daily_limit_usd"),
                default="$10",
                what="The daily budget a new account gets. Saving it also raises every account below it up to it.",
                why="A floor that can be raised for everyone in one step, without editing each account.",
                tradeoffs=(
                    "0 is a $0 budget (every request refused), never \"unlimited\". There is no unlimited: a forgotten account cannot spend without bound.",
                    "Accounts set to $0 are left alone when the floor is raised: they were blocked on purpose.",
                    "Accounts above the floor are never lowered by it.",
                ),
            ),
            Entry(
                id="cloud-daily-limit",
                name="Cloud daily limit",
                keys=("[app].cloud_daily_limit_usd", "users.cloud_daily_limit_usd"),
                default="none",
                what="A cap on Azure + AWS spend per day for accounts with cloud access. An account's own Cloud $ (Users tab) overrides it.",
                why="Cloud spend is real money, so it gets a tighter bound inside the overall daily limit.",
                tradeoffs=(
                    "One cap for Azure and AWS together: the account splits it however it likes. Separate amounts per provider existed and were retired; they only added ways to be refused.",
                    "It sits inside the daily limit, not beside it: cloud spend counts toward both.",
                    "An empty Cloud $ on an account means \"follow this default\", not \"no cap\". 0 refuses cloud.",
                    "It only applies to accounts with Azure or AWS access. Peak overflow has its own budget for everyone else.",
                ),
            ),
            Entry(
                id="cloud-budget-fallback",
                name="Cloud budget fallback",
                keys=("[app].cloud_budget_fallback",),
                default="off",
                what="On: an account whose cloud cap is spent gets the on-prem model instead of a 429 (the response carries X-Budget-Fallback).",
                why="Off is the long-standing contract: you get the model you asked for, or an explicit budget error. Switching to \"you get a model\" is an operator's decision, made once here.",
                tradeoffs=(
                    "On keeps people working; the cost is that they silently get a different model's output unless their client shows the header.",
                    "It applies to /v1/* only. /azure/v1/* and /aws/v1/* name their backend in the URL and never fall back.",
                    "The overall daily limit still applies: once that is gone, the request is refused either way.",
                ),
            ),
        ),
    ),
    Section(
        id="overflow",
        title="Cloud overflow",
        where="Limits → Cloud Overflow",
        href="/admin#limits",
        intro=(
            "The opposite direction of the budget fallback: while an on-prem server is congested, some accounts' "
            "requests for its models are served by a cloud model, within a daily budget. Added in November 2026 "
            "because the on-prem queue was full at peak hours while most people could, by policy, use the cloud."
        ),
        entries=(
            Entry(
                id="overflow-unit",
                name="Why whole accounts, never single requests",
                what="The gateway moves an account to the cloud and all its requests for that server follow, until it is moved back.",
                why=(
                    "Prompt caching. A conversation that alternated between an on-prem and a cloud model would pay the "
                    "full, uncached price on the cloud every turn, and a full prefill on-prem every time it came back, "
                    "which is exactly the load overflow is meant to remove."
                ),
                tradeoffs=(
                    "An account is moved per server: if server A is congested it keeps using server B on-prem. That costs a little bookkeeping and saves cloud money and data leaving the premises for nothing.",
                    "Moving the busiest accounts first relieves the queue fastest; it also sends the largest contexts, and the largest bills, to the cloud.",
                    "A model the gateway does not have (Claude Code subagents name several) is served by its on-prem stand-in, so it overflows with that server.",
                ),
                decided="Chosen over routing by request, by a hash of the account, and by a fixed 10-minute sentence.",
            ),
            Entry(
                id="overflow-mode",
                name="Mode",
                keys=("[app].overflow_mode",),
                default="off",
                what="Off: nobody is moved. Monitor: the job records who it would move and moves nobody. On: moved accounts are served by the cloud.",
                why="Like the concurrency limit and the IP allowlist, it can be watched before it acts.",
                tradeoffs=(
                    "In Monitor nothing relieves the queue, so the list keeps growing while it is congested: read it as \"who would have been first\", not as the size overflow would reach.",
                    "A Monitor list is never carried into On: switching modes moves everyone back and starts again.",
                    "Off moves everyone back at once.",
                ),
                decided="Went straight to On rather than a monitor period first: the queue was already full, and the budget bounds the cost.",
            ),
            Entry(
                id="overflow-budget",
                name="Daily budget",
                keys=("[app].overflow_daily_budget_usd",),
                default="$100",
                what="What overflow may spend on the cloud per day (Asia/Taipei), all accounts together. Once spent, everyone is moved back until midnight.",
                why="Overflow sends requests to a paid backend without anyone choosing to; a pool bounds what that can cost in a day.",
                tradeoffs=(
                    "It has a default, so switching the mode on starts spending up to $100 a day without a second step. The mode itself is off by default; that is the guard.",
                    "$0 moves nobody. A value in config.toml that is not a number counts as $0, not as the default.",
                    "The spend is summed by the job once a round, not per request, so it can overshoot by one round's worth.",
                    "When it runs out at the worst hour, the queue is full again. Too small a budget buys little; watch the card for a few days.",
                ),
                decided="A site-wide pool, shown on the card, on top of each account's own daily limit. The owner set its default to $100 a day.",
            ),
            Entry(
                id="overflow-stop",
                name="Stop at %",
                keys=("[app].overflow_budget_stop_pct",),
                default="85",
                what="At this share of the budget no further account is moved. Those on the cloud stay until they are moved back.",
                why="Without it, the moment the budget ran out everyone would return on-prem together, each with a cold prompt cache.",
                tradeoffs=("Lower drains the list more gently and leaves more of the budget unused; 100 uses all of it and ends abruptly.",),
            ),
            Entry(
                id="overflow-thresholds",
                name="Move at ≥ / Back at ≤",
                keys=("[app].overflow_waiting_high", "[app].overflow_waiting_low", "overflow_waiting_high / _low on a model"),
                default="12 / 2",
                what="Waiting requests on a server at or above which accounts are moved to the cloud, and at or below which they are moved back. Between the two nothing moves.",
                why="With one threshold the queue would drop below it right after a move, accounts would be released, it would rise again: endless back and forth, each crossing costing a cold cache.",
                tradeoffs=(
                    "A wider gap is steadier and keeps accounts on the cloud (and paying) longer; a narrow one reacts faster and flips more.",
                    "Servers differ in size, so a model can set its own pair in Models; the pair here is for the rest.",
                    "The number is vLLM's own waiting queue, read every 30 s. It is a snapshot, not an average.",
                ),
            ),
            Entry(
                id="overflow-consecutive",
                name="In a row",
                keys=("[app].overflow_consecutive",),
                default="2",
                what="How many readings in a row must be over (or under) before anyone is moved.",
                why="A burst of subagent calls can fill the queue for a moment. One reading is noise.",
                tradeoffs=("Higher ignores more noise and reacts later: each reading is a round of the job (about 30 s).",),
            ),
            Entry(
                id="overflow-pace",
                name="Move per round / Back per round",
                keys=("[app].overflow_grab_max", "[app].overflow_release_per_round"),
                default="2 / 1",
                what="Accounts moved to the cloud per round at most (one just over the threshold, this many at twice it), and accounts moved back per round.",
                why="In fast, out slow. A moved account's requests already on the GPUs still finish there, so the queue reacts late: moving many at once overshoots. Returning many at once fills the queue again.",
                tradeoffs=(
                    "Moving one at a time would take ten minutes to move twenty people while the queue keeps growing.",
                    "A larger number empties the queue sooner and spends the budget sooner.",
                ),
                decided="Both are settings, by the owner's wish; the scaling between one and the maximum is fixed.",
            ),
            Entry(
                id="overflow-stay",
                name="Min stay",
                keys=("[app].overflow_min_stay_min",),
                default="10 minutes",
                what="An account stays on the cloud at least this long, however short the queue gets.",
                why="It is a minimum, not a sentence: a fixed 10 minutes would return an account to a still-full queue and move it again, twice a cold cache every 10 minutes.",
                tradeoffs=("Longer means fewer crossings and more cloud spend after the peak has passed.",),
            ),
            Entry(
                id="overflow-idle",
                name="Idle after",
                keys=("[app].overflow_idle_min",),
                default="5 minutes",
                what="An account with no finished request for this long is idle, and is moved back first.",
                why="After a few quiet minutes neither side holds a warm cache for it any more, so moving it costs nothing. Among the rest, the longest on the cloud goes first.",
                tradeoffs=(
                    "Not a stack (last in, first out): the first people moved would spend the whole peak on the cloud, on their own budget.",
                    "Not purely first in, first out either: that would pull back someone in the middle of a conversation while an idle account sat on the cloud.",
                    "A long stream still running looks idle, because usage is recorded when a request finishes.",
                ),
            ),
            Entry(
                id="overflow-billing",
                name="Who pays",
                what="An overflowed request is billed at the cloud model's real price, counts toward the account's own daily limit, and is marked overflow on its dashboard and in /v1/usage.",
                why="So the books agree: one row, one price, in every report.",
                tradeoffs=(
                    "An account can reach its daily limit sooner because the gateway moved it: on-prem prices are notional, cloud prices are not.",
                    "An account with cloud access keeps its cloud cap. Once that is spent it is served on-prem rather than refused, since it asked for an on-prem model.",
                    "The anomaly scan may read a moved account's higher cost as a cost spike.",
                ),
                decided="The owner chose charging the account over charging only the pool.",
            ),
            Entry(
                id="overflow-who",
                name="Who may be moved",
                keys=("users.can_overflow_cloud",),
                default="allowed",
                what="Every person unless an admin turns Overflow off on the Users tab. Service accounts are never moved.",
                why="\"May this account's prompts leave the premises\" is a different question from \"may it pick a cloud model\" (Azure / AWS access), so it is a switch of its own.",
                tradeoffs=(
                    "Allowed by default means a new account in a restricted group can be moved until someone turns it off. The Special access strip lists every account that is off.",
                    "Turning it off moves the account back at once; a request already sent to the cloud finishes there.",
                    "Service accounts are left alone: moving the people frees enough, and nobody watches a program's requests go to the cloud.",
                ),
                decided="Default on, switchable at any time; service accounts untouched. Both the owner's.",
            ),
            Entry(
                id="overflow-guardrail",
                name="Guardrail",
                what="An overflowed request is not screened by the gateway's guardrail.",
                why="The guardrail screens local traffic; the cloud providers apply their own filters.",
                tradeoffs=("Their verdicts are not recorded here: overflowed requests are absent from the weekly guardrail report and from the repeated-unsafe finding.",),
                decided="The owner's decision.",
            ),
            Entry(
                id="overflow-failsafe",
                name="When something breaks",
                what="A stay is a lease the job renews every round. If the job stops, or a server's queue cannot be read, the stays lapse within 10 minutes. If the cloud refuses a moved request before answering, it is served on-prem.",
                why="Every failure ends on-prem: the data stays in, and the account still gets the model it asked for.",
                tradeoffs=(
                    "A request that is refused by the cloud and then served on-prem took the time of both attempts.",
                    "It keeps the cloud concurrency slot it was admitted with.",
                ),
            ),
        ),
    ),
    Section(
        id="accounts",
        title="Per-account switches",
        where="Users → the table's toggles and fields",
        href="/admin#users",
        intro="What one account may do. Every switch that is not at its default is counted in the Special access strip above the tables.",
        entries=(
            Entry(
                id="account-cloud-access",
                name="Azure / AWS",
                keys=("users.can_use_azure", "users.can_use_bedrock"),
                default="off",
                what="The account may ask for that provider's models, and sees them in its model list.",
                why="Cloud models cost real money and send prompts outside, so access is granted per account.",
                tradeoffs=(
                    "An account without access that names a cloud model is not told \"forbidden\": it is served the on-prem stand-in. Cloud models are hidden through the model list, not through errors.",
                    "Revoking access also hides that provider's past spend from the account's dashboard.",
                ),
            ),
            Entry(
                id="account-disable",
                name="Disable",
                keys=("users.is_disabled",),
                default="off",
                what="The account's API key is refused and it cannot sign in.",
                why="Stops an account without losing its usage history, which deleting does not either: usage is the billing record and is always kept.",
                tradeoffs=("On the web pages an admin (by SSO scope) is let in even when their own account is disabled; on the API nobody is.",),
            ),
            Entry(
                id="account-owner",
                name="Owners of a service account",
                keys=("app_owners",),
                what="The people who see the service account's key and usage on their dashboard.",
                why="A program has no dashboard of its own; someone must be able to rotate its key.",
                tradeoffs=("Only a service account has owners, and an owner is an existing person: assigning a person's account would hand that person's key to someone else.",),
            ),
            Entry(
                id="account-acts-for-users",
                name="On behalf of users (a service account that sends requests for its users)",
                keys=("users.acts_for_users", "usage_logs.via_user_id"),
                default="off",
                what=(
                    "An application people sign in to names the person on each request: in the header X-On-Behalf-Of: <username>, "
                    "or in the request body's metadata.user_id. Beside user_id it may add labels of its own in metadata "
                    "(a module, a ticket number); those are recorded in Langfuse with the request, so it can be looked "
                    "up later, and are not passed on to the model. "
                    "The request is then that person's: their daily limit and cloud cap, their cloud access, their "
                    "bill and their dashboard. The service account is recorded as the way it came in. A request "
                    "that names nobody, or naming someone who has no account here, a disabled account or a "
                    "service account, is refused."
                ),
                why=(
                    "So that an application shared by many people does not hide who is using it: usage is known and "
                    "charged per person, and one heavy user cannot spend the budget of everyone else on it."
                ),
                tradeoffs=(
                    "The name is the application's word, not proof. The gateway authenticates the application (its key); the application authenticated the person. It is enough to know and charge who used it, not to prove it against someone who denies it.",
                    "Whoever holds this account's key can spend any person's daily budget. Keep the key on the application's server only, and give the account an IP allowlist rule for that server.",
                    "The application cannot give a person more than they have: a cloud model is served only to a user with that cloud access, and a user over their limit gets the usual 429.",
                    "The header is honoured from a service account with this switch on, and from nobody else. A person's key that sends it is served as that person.",
                    "The key, its last use and the IP allowlist stay the application's; the concurrency limit counts on the person, together with what they send themselves.",
                    "On behalf of a user the application reads the model list (/v1/models), /v1/me and /v1/status. The user's usage history, notices and settings are refused to it.",
                    "The model list it gets is the named user's own: the models that user would see with their own key. Which models are listed at all is one tick per model for everyone, Listed in GET /v1/models on the Models page; there is no list per application.",
                    "Client rules ignore these requests: what they show is the application's setup, not the person's client.",
                ),
                decided=(
                    "Usage through such an application is charged to the user, not to the service account. A user "
                    "who has no account on the gateway is refused, never created on the application's word."
                ),
            ),
        ),
    ),
    Section(
        id="models",
        title="Per-model settings",
        where="Models (the model editor)",
        href="/admin/models",
        intro="Routing choices made per model. Saved to config.toml and picked up by every worker without a restart.",
        entries=(
            Entry(
                id="model-overflow-to",
                name="When congested, overflow to",
                keys=("overflow_to", "overflow_waiting_high", "overflow_waiting_low"),
                default="nothing",
                what="The Azure / AWS model that serves this on-prem model for moved accounts, and optionally this server's own thresholds.",
                why="Only a person can say which cloud model is a fair stand-in for which on-prem one, so the gateway never guesses: no value, no overflow.",
                tradeoffs=(
                    "Keep the mapping fixed: a conversation's cloud prompt cache depends on always landing on the same model.",
                    "For Claude Code on AWS pick a Claude entry with native Messages; the Converse path does not carry prompt caching.",
                    "A pricier stand-in spends the budget and the accounts' limits faster.",
                ),
            ),
            Entry(
                id="model-rate-limit-fallback",
                name="When rate limited (429), retry on",
                keys=("rate_limit_fallback",),
                default="nothing",
                what="Another model of the same provider that takes the request when this one answers 429, up to three hops.",
                why="Provider quotas are per deployment; a second deployment often has room.",
                tradeoffs=(
                    "Same provider only: a 429 never moves a request between Azure and AWS, or onto the GPUs.",
                    "It switches only before the client has received anything.",
                ),
            ),
            Entry(
                id="model-fallback",
                name="Fallback when a server is down",
                keys=("[fallback].<type>",),
                default="the first server of that type that is up",
                what="The on-prem model that serves a request whose own model is down or not configured.",
                why="An unknown name falls back rather than failing: clients are often configured with a model the gateway does not have.",
                tradeoffs=(
                    "Forgiving, and it hides typos: the Clients tab's check CC004 tells the account instead.",
                    "Every such request lands on this one model. If that is your largest, small jobs sent under unknown names fill it.",
                ),
            ),
            Entry(
                id="model-list-in-api",
                name="Listed in GET /v1/models",
                keys=("list_in_api",),
                default="listed",
                what=(
                    "Ticked, the model is in the list a client asks the gateway for: GET /v1/models and /models, and "
                    "the Azure and AWS listings. That is what fills the model picker of Claude Code and of an "
                    "application. Unticked, it is left out for everyone and every key; a request that names it is still served."
                ),
                why="For models kept for a few programs, or being phased out, without advertising them to every model picker.",
                tradeoffs=(
                    "It is one list for everyone. An application that sends requests for its users gets each user's own list; there is no list per application or per account.",
                    "Unlisted is not private: anyone who knows the name can use it.",
                    "It is apart from the dashboard tick: a model can be offered to clients and kept off the dashboard, or the other way round.",
                    "A model that has no setting of its own follows the dashboard tick, as every model did when one tick did both.",
                ),
                decided="What clients are offered and what the pages show are two separate settings, each with its own tick.",
            ),
            Entry(
                id="model-hidden",
                name="Shown on the dashboard",
                keys=("hidden",),
                default="shown",
                what=(
                    "Ticked, the model is in the model tables people see on the dashboard, and in GET /v1/status, "
                    "the same tables by API key. Unticked, it is left out there; a request that names it is still served."
                ),
                why="The dashboard is what people read to choose a model; a staged or retiring one need not be there.",
                tradeoffs=(
                    "It does not change what GET /v1/models lists: that is the tick above.",
                    "A model taken off the dashboard is also never named there as the stand-in for a model that is down.",
                ),
            ),
        ),
    ),
    Section(
        id="security",
        title="Security switches",
        where="Security → IP Allowlist, Guardrail",
        href="/admin#security",
        intro="Both can be watched before they refuse anything, and both fail in the direction that keeps the service up.",
        entries=(
            Entry(
                id="ip-allowlist-mode",
                name="IP allowlist mode",
                keys=("[app].ip_allowlist_mode",),
                default="off",
                what="Off, Monitor (requests from outside the rules are logged and served) or Enforce (they get 403). A rule is an address range, who it admits, and an optional window.",
                why="An API key that leaks is useless from an address no rule covers.",
                tradeoffs=(
                    "Only API requests are checked. The admin pages stay reachable from any address, or a wrong rule could lock out the person who has to fix it.",
                    "Only admins add or end rules; accounts cannot open access for themselves and are not shown their address.",
                    "Rules name usernames and org codes, not account ids, so deleting an account never changes a rule.",
                ),
            ),
            Entry(
                id="guardrail-action",
                name="Guardrail action",
                keys=("[guardrail].enabled", "[guardrail].action"),
                default="off · monitor",
                what="Screens the newest user turn of local requests with a guard model. Monitor records the verdict; Block refuses an unsafe request with a 400.",
                why="Monitor adds no latency (the guard runs beside the request) and shows what Block would refuse before it refuses anything.",
                tradeoffs=(
                    "It fails open even in Block: a guard that times out or errors lets the request through. The guard misjudges and goes down; the service should not go down with it.",
                    "Block waits for the verdict, so every screened request is slower by the guard's answer time.",
                    "Local traffic only, and never requests moved to the cloud by overflow. The guard's own tokens are not billed.",
                ),
            ),
        ),
    ),
    Section(
        id="clients",
        title="Client rules and announcements",
        where="Clients → Client rules, Model changes",
        href="/admin#clients",
        intro="How accounts are told about a client setting they should change. What a rule detects is code; what you set here is how its accounts are told.",
        entries=(
            Entry(
                id="client-rule-action",
                name="Notice or Remind",
                keys=("client_rule_settings.action",),
                default="notice",
                what="Notice shows the violation on the account's dashboard. Remind also answers the first violating request of each day with the message, as a 400.",
                why="A dashboard notice is easy to miss for someone who lives in a terminal; the reminder reaches them there.",
                tradeoffs=("Remind is the one client-rule setting that fails a request, once a day, on purpose.",),
            ),
            Entry(
                id="client-monthly-mail",
                name="Monthly reminder and mail",
                keys=("[app].client_rules_monthly_mail",),
                default="off",
                what="On the 1st, mails everyone who still has something open. Otherwise mail goes out only when an admin presses a mail button.",
                why="Nothing is mailed unasked. Per-check automatic mail existed and was retired.",
                tradeoffs=(
                    "Mail leaves in small batches (20 every 10 minutes by default): a burst can look like abuse to a mail server, so a large send takes a while.",
                    "One mail per account about everything it has open, never one per rule.",
                ),
                decided="The owner's decision: no check mails by itself.",
            ),
            Entry(
                id="model-change-switch",
                name="Model change: Show it now / Stop showing",
                keys=("model_changes.shown",),
                default="off",
                what="While on, every account the renames are for sees the announcement and can download a script that renames the model in its settings.",
                why="A switch and no schedule: an announcement is on or it is not, with no second way to say so.",
                tradeoffs=("Nobody has to remember a date, and nobody is told automatically: you switch it on, and off.",),
                decided="The owner's choice.",
            ),
        ),
    ),
    Section(
        id="reports",
        title="Reports and links",
        where="Settings → Weekly reports, Site links",
        href="/admin#settings",
        intro="",
        entries=(
            Entry(
                id="weekly-reports",
                name="Weekly reports",
                keys=("report_schedules",),
                default="off",
                what="Two mails about last week: usage, and the guardrail. Each has its own day, time and recipients.",
                why="Tokens and cost come from the usage record (the bill); latency, errors and verdicts exist only in Langfuse.",
                tradeoffs=(
                    "A report can only be turned on while Langfuse monitoring is on; a due report waits and goes out once it is back.",
                    "Recipients are company mailboxes only, or the gateway could be made to mail usage figures elsewhere.",
                    "Cost is called estimated: it is tokens at the configured prices, not an invoice.",
                ),
            ),
            Entry(
                id="site-links",
                name="Site links",
                keys=("[app].support_bot_url", "[app].install_guide_url"),
                what="The support button shown on every page and the site's install guide link.",
                why="They differ per site, so they are settings rather than code.",
                tradeoffs=("Only http(s) or site-relative addresses are accepted: the link is shown to every signed-in user.",),
            ),
        ),
    ),
)


def entry_ids() -> set[str]:
    """Every anchor the page has: sections and entries."""
    return {section.id for section in SECTIONS} | {e.id for section in SECTIONS for e in section.entries}


def documented_keys() -> set[str]:
    """Every storage key some entry names."""
    return {key for section in SECTIONS for e in section.entries for key in e.keys}
