import json
import subprocess
import sys

import typer

from potency.settings import REPO_ROOT

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Potency evidence KG / GraphRAG")
eval_app = typer.Typer(no_args_is_help=True, help="Regression evaluation")
app.add_typer(eval_app, name="eval")

CONFIG_OPT = typer.Option("configs/casgevy-potency.json", "--config", help="Case configuration")


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _cfg(path: str):
    from potency.workflows.config import load_case_config

    return load_case_config(path)


@app.command("migrate")
def migrate() -> None:
    """Apply database migrations (owner role)."""
    raise typer.Exit(subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT))


@app.command("smoke")
def smoke() -> None:
    """Tiny embedding + generation access checks (synthetic input only)."""
    from potency.workflows.smoke import run_smoke

    _echo(run_smoke())


@app.command("import")
def import_cmd(config: str = CONFIG_OPT) -> None:
    """Import normalized records for the configured scope (idempotent)."""
    from potency.workflows.main_case import step_import

    r = step_import(_cfg(config))
    _echo({k: r[k] for k in ("run_id", "counts", "manifest_check", "corrections_check")})


@app.command("index")
def index_cmd(config: str = CONFIG_OPT) -> None:
    """Chunk and embed passages (embedding cache: unchanged text is never re-embedded)."""
    from potency.workflows.main_case import step_index

    _echo(step_index(_cfg(config)))


@app.command("extract")
def extract_cmd(config: str = CONFIG_OPT) -> None:
    """Relevance pass + batched semantic extraction."""
    from potency.workflows.main_case import step_extract

    r = step_extract(_cfg(config))
    _echo({k: r[k] for k in ("run_id", "extractor", "totals", "complete", "budget_stop")})


@app.command("build-graph")
def build_graph_cmd(config: str = CONFIG_OPT) -> None:
    """Rebuild the scope graph and export results/graph/{nodes,edges}.jsonl."""
    from potency.workflows.main_case import step_graph

    _echo(step_graph(_cfg(config)))


@app.command("report")
def report_cmd(config: str = CONFIG_OPT) -> None:
    """Write results/import-report.json from the store."""
    from potency.workflows.main_case import import_report

    r = import_report(_cfg(config))
    _echo({k: r[k] for k in ("corpus_snapshot", "index_version", "imported", "unchanged", "failed", "omitted",
                             "complete_import")})


@app.command("answer")
def answer_cmd(config: str = CONFIG_OPT, question: list[str] = typer.Option(None, "--question", "-q")) -> None:
    """Answer questions.json (P3 in both modes) and write results/answers.jsonl + traces.jsonl."""
    from potency.workflows.main_case import step_answer

    _echo(step_answer(_cfg(config), question or None))


@app.command("ask")
def ask_cmd(text: str, mode: str = "graph", config: str = CONFIG_OPT, budget: int = 6000) -> None:
    """Ad-hoc question against the configured scope (persists a trace, not written to results/)."""
    from potency import ids
    from potency.answering.service import answer_question
    from potency.workflows.config import case_context_text

    cfg = _cfg(config)
    a, t = answer_question(cfg.scope, question_id="adhoc", question=text, mode=mode, run_id=ids.new_run_id("adhoc"),
                           corpus_snapshot=cfg.corpus_snapshot, case_context=case_context_text(cfg.case), budget=budget)
    _echo({"answer": a["answer"], "citations": a["citations"], "not_found": a["not_found"], "trace_id": t["trace_id"],
           "citation_check": a["citation_check"]})


@app.command("pipeline")
def pipeline_cmd(config: str = CONFIG_OPT) -> None:
    """import -> index -> extract -> build-graph -> report (no answers)."""
    from potency.workflows.main_case import import_report, step_extract, step_graph, step_import, step_index

    cfg = _cfg(config)
    out = {"import": step_import(cfg)["counts"], "index": step_index(cfg)}
    out["extract"] = step_extract(cfg)["totals"]
    out["graph"] = {k: v for k, v in step_graph(cfg).items() if k != "export"}
    rep = import_report(cfg)
    out["report"] = {k: rep[k] for k in ("imported", "unchanged", "failed", "omitted", "complete_import")}
    _echo(out)


@app.command("isolation")
def isolation_cmd() -> None:
    """Project-isolation fixture: import both scopes, then query in a fresh process."""
    from potency.workflows.isolation import run_isolation

    r = run_isolation()
    _echo({"passed": r["passed"], "failures": r["failures"], "checks": len(r["checks"])})


@app.command("isolation-query", hidden=True)
def isolation_query_cmd() -> None:
    from potency.workflows.isolation import query_entrypoint

    query_entrypoint()


@app.command("fixtures")
def fixtures_cmd() -> None:
    """Source-correction and unresolved-conflict behavior fixtures."""
    from potency.workflows.behavior import run_behavior_fixtures

    r = run_behavior_fixtures()
    _echo({"checks": [(c["scenario_id"], c["stage"], c["check_id"], c["status"]) for c in r["checks"]],
           "failures": len(r["failures"])})


@app.command("usage")
def usage_cmd() -> None:
    """Summarize recorded model usage and estimated cost into results/usage.json."""
    from potency.workflows.usage_report import usage_report

    _echo(usage_report())


@eval_app.command("freeze")
def eval_freeze() -> None:
    """Record the SHA-256 of the holdout cases before the final run."""
    from potency.evaluation.runner import freeze_holdout

    _echo(freeze_holdout())


@eval_app.command("run")
def eval_run(split: str = typer.Option("all", help="dev | holdout | all")) -> None:
    """Run evaluation cases with real retrieval/generation and save results/evaluation/results.jsonl."""
    from potency.evaluation.runner import run_eval

    _echo(run_eval(split))


@eval_app.command("score")
def eval_score(rescore: bool = typer.Option(True, help="Recompute per-case scores from saved traces/answers")) -> None:
    """Recompute aggregates from saved per-case results (no model calls)."""
    from potency.evaluation.runner import score_saved

    _echo(score_saved(rescore=rescore))


if __name__ == "__main__":
    app()
