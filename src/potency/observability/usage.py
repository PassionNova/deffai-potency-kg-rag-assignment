"""Usage metering and the global spend guard."""

import uuid

from potency.db import scoped_session, unscoped_spend_session
from potency.settings import get_settings
from potency.tenancy import Scope


class BudgetExceeded(RuntimeError):
    pass


def estimate_usd(usage: dict) -> float:
    s = get_settings()
    inp = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) * 1.25
    inp += usage.get("cache_read_input_tokens", 0) * 0.1
    return round(inp / 1e6 * s.potency_price_input_per_mtok + usage.get("output_tokens", 0) / 1e6 * s.potency_price_output_per_mtok, 6)


def total_spend_usd() -> float:
    with unscoped_spend_session() as conn:
        row = conn.execute("SELECT coalesce(sum(estimated_usd), 0) AS usd FROM model_spend").fetchone()
        return float(row["usd"])


def check_budget() -> None:
    limit = get_settings().potency_max_usd
    spent = total_spend_usd()
    if spent >= limit:
        raise BudgetExceeded(f"estimated spend ${spent:.2f} reached POTENCY_MAX_USD=${limit:.2f}")


def record_usage(scope: Scope, run_id: str | None, purpose: str, model: str, usage: dict) -> float:
    usd = estimate_usd(usage)
    uid = uuid.uuid4().hex
    with scoped_session(scope) as conn:
        conn.execute(
            """
            INSERT INTO usage_ledger (id, tenant_id, project_id, run_id, purpose, model, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, estimated_usd)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (uid, scope.tenant_id, scope.project_id, run_id, purpose, model, usage.get("input_tokens", 0),
             usage.get("output_tokens", 0), usage.get("cache_read_input_tokens", 0),
             usage.get("cache_creation_input_tokens", 0), usd),
        )
    with unscoped_spend_session() as conn:
        conn.execute(
            "INSERT INTO model_spend (id, purpose, model, estimated_usd) VALUES (%s,%s,%s,%s)",
            (uid, purpose, model, usd),
        )
    return usd
