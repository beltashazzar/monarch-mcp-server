"""Tool exposure policy.

This server is given an LLM's credentials to a real personal-finance account. Upstream
ships 20 tools that mutate it — create/delete transactions, set budget amounts, rewrite
auto-categorization rules. Every tool is gated at registration time, so anything not
explicitly enabled is never advertised to the model and cannot be called even by name.

Which tools are enabled is data, not code: see ``read_only.toml``. Enable a write tool by
flipping its line to ``true``, one at a time, and restart. Nothing else needs editing.

**Allowlist, not denylist, and that is the whole point.** Classifying these tools by name
or by which client method they call both fail, in different places:

    split_transaction            reads as a query, calls update_transaction_splits
    categorize_transaction       reads as a query, calls update_transaction
    mark_transaction_reviewed    reads as a query, calls update_transaction
    update_category              named as a write, reaches Monarch by raw GraphQL
    update_merchant              named as a write, reaches Monarch by raw GraphQL
    create_transaction_rule      named as a write, reaches Monarch by raw GraphQL
    review_recurring_stream      reads as a query AND has no write-shaped call --
                                 it is a mutation only if you read the docstring

Three heuristics, three different misses. A denylist inherits that unreliability forever:
the next tool upstream adds is exposed by default, and the failure is silent and unbounded.
An allowlist fails the other way — a tool absent from the config stays hidden, which shows
up as a missing feature rather than as an LLM writing to the account. That trade is
deliberate, and it is why an unreadable config falls back to reads-only rather than to
"expose everything".

Rebase note: this module plus read_only.toml and two requirement lines are the entire fork
patch, so upstream changes flow through untouched. When rebasing, diff the registered tool
inventory against the config — the startup log names anything unlisted — and read the
docstring of anything new. Do not classify it by its name.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

#: Override the config location. Set this to a path OUTSIDE the checkout so that toggling a
#: tool never shows up as a dirty submodule.
CONFIG_ENV_VAR = "MONARCH_TOOLS_CONFIG"

#: Shipped default, used when the env var is unset.
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "read_only.toml"

#: Last-resort floor if the config is missing or unparseable. Deliberately reads only —
#: losing the config must never silently enable a write.
FALLBACK_READ_TOOLS = frozenset(
    {
        "get_accounts", "refresh_accounts", "get_account_holdings",
        "get_account_balance_history", "setup_authentication", "monarch_login",
        "monarch_login_with_token", "monarch_logout", "check_auth_status",
        "debug_session_loading", "get_budgets", "get_transaction_categories",
        "get_transaction_category_groups", "get_category_details", "get_cashflow_by_month",
        "get_cashflow", "get_net_worth", "get_net_worth_by_account_type", "get_merchant",
        "get_transaction_rules", "get_transaction_splits", "get_transactions_summary",
        "get_spending_summary", "get_transaction_tags", "get_transactions",
        "search_transactions", "get_transaction_details", "get_recurring_transactions",
        "get_transactions_needing_review",
    }
)


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG)


def load_enabled() -> set[str]:
    """Return the set of tool names permitted to register.

    Any failure falls back to FALLBACK_READ_TOOLS and says so loudly. A config problem
    should degrade to read-only, never to unrestricted.
    """
    path = config_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        logger.warning(
            "tool config not found at %s — falling back to built-in read-only set. "
            "Set %s to point at one.", path, CONFIG_ENV_VAR,
        )
        return set(FALLBACK_READ_TOOLS)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.error(
            "tool config at %s is unreadable (%s) — falling back to built-in read-only set. "
            "No write tool will be exposed until this is fixed.", path, exc,
        )
        return set(FALLBACK_READ_TOOLS)

    tools = data.get("tools")
    if not isinstance(tools, dict):
        logger.error(
            "tool config at %s has no [tools] table — falling back to built-in read-only set.",
            path,
        )
        return set(FALLBACK_READ_TOOLS)

    # `is True` on purpose: only a real boolean true enables a tool. A stray "false",
    # 0, or "" must never read as permission.
    enabled = {name for name, value in tools.items() if value is True}
    logger.info("tool config %s: %d of %d tool(s) enabled", path, len(enabled), len(tools))
    return enabled


def enforce(mcp) -> tuple[list[str], list[str]]:
    """Wrap ``mcp.tool`` so only enabled tools register. Call before importing tools.

    Returns (withheld, enabled_and_seen). The caller should log both — a silent gate is
    indistinguishable from a gate that has stopped working, and a name in the config that
    never registers usually means upstream renamed or removed it.
    """
    enabled = load_enabled()
    original = mcp.tool
    withheld: list[str] = []
    seen: list[str] = []

    def gated(*args, **kwargs):
        decorate = original(*args, **kwargs)

        def register(fn):
            name = fn.__name__
            if name in enabled:
                seen.append(name)
                return decorate(fn)
            withheld.append(name)
            # Returned undecorated: importable, callable in-process, never an MCP tool.
            return fn

        return register

    mcp.tool = gated
    return withheld, seen
