"""Where everything is in the admin panel: one list of its sections.

The panel has grown to six tabs and two pages, and an admin had to remember
which tab a setting lives on. This list is what the two ways around that are
built from, so they cannot disagree:

- the **section bar** under the tab bar of ``/admin`` (the sections of the
  tab that is open, each a jump, each card foldable), and
- the **quick search** (Ctrl+K on ``/admin``, ``/admin/models`` and
  ``/admin/guide``): every section, every entry of the Guide, every model,
  and the accounts (``GET /admin/api/account-search``).

Pure data and look-ups: no database, no request. A card added to
``admin.html`` gets an entry here with the ``id`` its element carries, in the
same change (``tests/test_admin_nav.py`` fails when a section names an
element that is not on its tab, or a card marked ``data-section`` is missing
from the list). ``keywords`` are the words an admin might type that are not
in the title; they are matched, never shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core import config
from app.services import admin_guide


TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("users", "Users"),
    ("clients", "Clients"),
    ("security", "Security"),
    ("limits", "Limits"),
    ("settings", "Settings"),
)


@dataclass(frozen=True)
class NavSection:
    tab: str            # the admin tab (data-panel) the element is on
    id: str             # the element's id in admin.html
    title: str
    keywords: str = ""
    fold: bool = True   # the card can be folded to one line


SECTIONS: tuple[NavSection, ...] = (
    # Overview: figures, nothing to fold away.
    NavSection("overview", "ov-numbers", "Key numbers", "today spend requests active users month 今日 花費 請求", fold=False),
    NavSection("overview", "ov-trends", "Spend and active users", "chart 30 days estimated spend daily active users dau 趨勢 圖表", fold=False),
    NavSection("overview", "ov-leaderboards", "Leaderboards", "top users service accounts ranking 排行", fold=False),
    NavSection("overview", "ov-departments", "Department usage", "org code department bu 部門", fold=False),
    NavSection("overview", "ov-export", "Export usage report", "xlsx csv download backend costs billing 匯出 報表", fold=False),
    # Users
    NavSection("users", "special-access", "Special access", "azure aws waiver overflow disabled permissions 特殊權限", fold=False),
    NavSection("users", "user-management", "Accounts", "users service accounts api key daily limit search bulk 帳號 使用者 額度", fold=False),
    NavSection("users", "create-app", "Create service account", "new app account owner program 新增 服務帳號"),
    # Clients
    NavSection("clients", "client-rules", "Client rules", "checks CC001 violations notices patch mail claude code versions mail log 規則 違規 通知 寄信"),
    NavSection("clients", "model-changes", "Model changes", "announcement retire replace rename model 模型汰換 公告"),
    NavSection("clients", "client-setup", "Client setup", "settings.json env variables /v1/settings defaults 設定"),
    NavSection("clients", "client-installs", "Client installs", "events installed tools machines os versions /v1/events 安裝"),
    # Security
    NavSection("security", "ip-allowlist", "IP allowlist", "address cidr rule monitor enforce 白名單"),
    NavSection("security", "guardrail", "Guardrail", "qwen3guard screening unsafe block monitor categories 防護"),
    NavSection("security", "auditLogCard", "Audit log", "trail changes who csv 稽核"),
    # Limits
    NavSection("limits", "limits-summary", "In force now", "current limits summary 目前生效", fold=False),
    NavSection("limits", "concurrency-limit", "Concurrency limit", "in flight parallel off-peak hours local cloud 並行 離峰"),
    NavSection("limits", "default-daily-limit", "Default daily limit", "budget usd floor new users 每日額度"),
    NavSection("limits", "cloud-daily-limit", "Cloud daily limit", "azure aws bedrock cap 雲端額度"),
    NavSection("limits", "cloud-budget-fallback", "Cloud budget fallback", "429 on-prem instead 雲端 回退"),
    NavSection("limits", "cloud-overflow", "Cloud overflow", "peak congested queue waiting budget thresholds 尖峰 溢流"),
    # Settings
    NavSection("settings", "weekly-reports", "Weekly reports", "mail usage guardrail schedule recipients langfuse 週報"),
    NavSection("settings", "site-links", "Site links", "support bot install guide url 連結"),
)


def sections_by_tab() -> dict[str, list[NavSection]]:
    out: dict[str, list[NavSection]] = {tab: [] for tab, _ in TABS}
    for section in SECTIONS:
        out[section.tab].append(section)
    return out


def _models() -> list[dict[str, Any]]:
    """Every configured model, as a jump into the Models page (its URL hash
    is the selection: ``#<backend>/<alias>``). A broken config lists none."""
    found: list[dict[str, Any]] = []
    try:
        backends = (
            ("vllm", "On-prem", config.get_model_routing_snapshot()),
            ("azure", "Azure", config.get_azure_models_snapshot()),
            ("bedrock", "AWS", config.get_bedrock_models_snapshot()),
        )
        for kind, label, models in backends:
            for alias in sorted(models):
                found.append({"kind": kind, "alias": str(alias), "backend": label})
    except Exception:  # the search must open even when the config does not load
        return []
    return found


def index() -> dict[str, Any]:
    """What the quick search looks through, as data for the page: the
    sections (also the section bar's), the pages, the Guide's entries and
    the models. Accounts are not in it: they are searched on the server."""
    tab_label = dict(TABS)
    return {
        "sections": [
            {
                "tab": s.tab, "tabLabel": tab_label[s.tab], "id": s.id, "title": s.title,
                "keywords": s.keywords, "fold": s.fold,
            }
            for s in SECTIONS
        ],
        "pages": [
            {"title": "Models", "hint": "routing, pricing, fallbacks, display order", "href": "/admin/models",
             "keywords": "model config toml alias price base url 模型 設定 價格"},
            {"title": "Guide", "hint": "what every setting does and why", "href": "/admin/guide",
             "keywords": "why help documentation trade-offs 說明"},
            {"title": "Dashboard", "hint": "your own account page", "href": "/dashboard", "keywords": "home 首頁"},
        ],
        "guide": [
            {"id": entry.id, "title": entry.name, "section": section.title, "keywords": " ".join(entry.keys)}
            for section in admin_guide.SECTIONS for entry in section.entries
        ],
        "models": _models(),
    }
