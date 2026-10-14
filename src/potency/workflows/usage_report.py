from collections import defaultdict

from potency.db import scoped_session
from potency.settings import get_settings
from potency.tenancy import Scope
from potency.workflows.io import RESULTS, write_json


def usage_report() -> dict:
    """Aggregates the per-scope usage ledgers of every scope this deployment knows about."""
    s = get_settings()
    scopes = [
        Scope("public-fda", "casgevy-potency"),
        Scope("fixture-tenant", "project-one"),
        Scope("fixture-tenant", "project-two"),
        Scope("fixture-tenant", "source-correction"),
        Scope("fixture-tenant", "unresolved-conflict"),
    ]
    out = {"pricing_assumption_usd_per_mtok": {"input": s.potency_price_input_per_mtok,
                                               "output": s.potency_price_output_per_mtok,
                                               "cache_write_multiplier": 1.25, "cache_read_multiplier": 0.1},
           "note": "Estimated from provider-reported token usage; not an invoice.", "scopes": {}}
    grand = defaultdict(float)
    for sc in scopes:
        with scoped_session(sc) as conn:
            rows = conn.execute(
                "SELECT split_part(purpose, ':', 1) AS purpose, model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
                "sum(output_tokens) AS output_tokens, sum(cache_read_tokens) AS cache_read_tokens, "
                "sum(cache_write_tokens) AS cache_write_tokens, sum(estimated_usd) AS estimated_usd "
                "FROM usage_ledger GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
        out["scopes"][sc.prefix] = [dict(r) for r in rows]
        for r in rows:
            for k in ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                      "estimated_usd"):
                grand[k] += float(r[k] or 0)
    out["totals"] = {k: (round(v, 4) if k == "estimated_usd" else int(v)) for k, v in grand.items()}
    write_json(RESULTS / "usage.json", out)
    return out
