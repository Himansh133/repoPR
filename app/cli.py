"""The `repopilot` command line.

    repopilot run --repo <path-or-github-url> --issue <n> [--github owner/repo]
    repopilot runs | show <id> | usage [--today]
    repopilot approve <id> [--yes] [--closes-issue] | reject <id>
    repopilot serve [--host 127.0.0.1] [--port 8000]

Exit codes for `run`: 0 passed, 1 failed, 3 inconclusive, 4 aborted or crashed,
5 usage, configuration or budget error (2 is argparse's own "bad arguments").
The work itself lives in `app.service`; this module parses, prints and sets exit codes.
All collaborators are injected through `Deps`, so tests use fakes.
"""

import argparse
import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from app.agent.fix import RunBudgets, RunStatus
from app.evals import (
    EvalError,
    comparison_table,
    load_benchmark,
    load_results,
    metrics_by_model,
    run_eval,
)
from app.github import Issue
from app.service import (
    Deps,
    RunCrashed,
    RunRequest,
    ServiceError,
    execute_run,
    issue_from_fields,
    load_settings,
    open_db,
    open_draft_pr,
    parse_github_slug,
    plan_approval,
    prepare_run,
    reject_run,
)

__all__ = ["CliError", "Deps", "main", "parse_github_slug"]

EXIT = {
    RunStatus.PASSED: 0,
    RunStatus.FAILED: 1,
    RunStatus.INCONCLUSIVE: 3,
    RunStatus.ABORTED: 4,
}
CRASH_CODE = 4
USAGE_CODE = 5

CliError = ServiceError  # a problem the user can fix: printed as one line, exit code 5


def _issue_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repopilot", description="Fix a GitHub issue, with tests."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agent on one issue")
    run.add_argument("--repo", required=True, help="local git repo path or https GitHub URL")
    run.add_argument("--issue", required=True, type=_issue_number, help="issue number")
    run.add_argument("--github", help="owner/repo, if it cannot be worked out from --repo")
    run.add_argument("--issue-file", help="JSON file with title and body (skips GitHub)")
    run.add_argument("--allow-unpriced", action="store_true", help="run even if prices are unset")
    run.add_argument("--cleanup", action="store_true", help="delete the working copy afterwards")

    runs = sub.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=_issue_number, default=20)

    show = sub.add_parser("show", help="show a stored report")
    show.add_argument("run_id", type=int)

    usage = sub.add_parser("usage", help="token and cost totals")
    usage.add_argument("--today", action="store_true")

    ev = sub.add_parser("eval", help="run a benchmark against one or more models")
    ev.add_argument("--models", required=True, help="comma-separated model IDs")
    ev.add_argument("--issues", default="evals/benchmark_issues.jsonl")
    ev.add_argument("--out", help="results folder (default evals/results/<timestamp>)")
    ev.add_argument("--base-dir", default=".", help="folder that 'dir:' repos are relative to")
    ev.add_argument("--max-runs", type=_issue_number, default=30,
                    help="refuse to start if models x issues is larger than this")
    ev.add_argument("--allow-unpriced", action="store_true")

    cmp_ = sub.add_parser("compare", help="print the comparison table of saved eval results")
    cmp_.add_argument("results_dir")

    approve = sub.add_parser("approve", help="open a draft PR for a passed run")
    approve.add_argument("run_id", type=int)
    approve.add_argument("--note", default="")
    approve.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    approve.add_argument("--closes-issue", action="store_true",
                         help="write 'Fixes #N' (closes the issue on merge) instead of 'Refs #N'")

    reject = sub.add_parser("reject", help="reject a run; nothing is sent to GitHub")
    reject.add_argument("run_id", type=int)
    reject.add_argument("--note", default="")

    serve = sub.add_parser("serve", help="start the HTTP API (needs REPOPILOT_API_KEY)")
    serve.add_argument("--host", default="127.0.0.1",
                       help="address to bind (default 127.0.0.1: this machine only)")
    serve.add_argument("--port", type=_issue_number, default=8000)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    deps: Deps | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run the CLI. Returns the exit code."""
    deps = deps or Deps()
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args, deps, out, err)
        if args.command == "eval":
            return _cmd_eval(args, deps, out)
        if args.command == "compare":
            return _cmd_compare(args, out)
        if args.command == "approve":
            return _cmd_approve(args, deps, out)
        if args.command == "reject":
            return _cmd_reject(args, deps, out)
        if args.command == "serve":
            return _cmd_serve(args, deps, out)
        return _cmd_read(args, deps, out)
    except CliError as exc:
        print(f"error: {exc}", file=err)
        return USAGE_CODE


# --- read-only commands --------------------------------------------------------------------------

def _clip_left(text: str, width: int) -> str:
    """Keep the end of `text` (the repo name and issue number) when it is too long."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


def _cmd_read(args, deps: Deps, out: TextIO) -> int:
    db = open_db(deps)
    try:
        if args.command == "runs":
            runs = db.list_runs(args.limit)
            if not runs:
                print("No runs yet.", file=out)
                return 0
            print(f"{'ID':<5}{'DATE':<12}{'STATUS':<14}{'ISSUE':<26}{'MODEL':<26}"
                  f"{'TOKENS':<10}COST", file=out)
            for r in runs:
                cost = "unknown" if r.cost is None else format(r.cost, "f")
                issue = _clip_left(f"{r.repo}#{r.issue_number}", 24)
                model = _clip_left(r.model.rsplit("/", 1)[-1], 24)
                print(f"{r.id:<5}{r.started_at[:10]:<12}{r.status:<14}"
                      f"{issue:<26}{model:<26}"
                      f"{r.input_tokens + r.output_tokens:<10}{cost}", file=out)
            return 0
        if args.command == "show":
            run = db.get_run(args.run_id)
            if run is None:
                raise CliError(f"No run with id {args.run_id}")
            print(run.report or f"(no report; status {run.status}: {run.reason})", file=out)
            if run.pr_url:
                print(f"Draft PR: {run.pr_url}", file=out)
            return 0
        day = datetime.now(UTC).date() if args.today else None
        totals = db.usage_totals(day)
        print(f"Runs: {totals.runs}", file=out)
        print(f"Tokens: {totals.input_tokens} in, {totals.output_tokens} out", file=out)
        unknown = f" ({totals.unknown_cost_runs} run(s) with unknown cost)" \
            if totals.unknown_cost_runs else ""
        print(f"Cost: {format(totals.cost, 'f')}{unknown}", file=out)
        return 0
    finally:
        db.close()


# --- run ------------------------------------------------------------------------------------------

def _load_issue_file(path: str, number: int) -> Issue:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise CliError("Could not read the issue file (it must be valid JSON)") from None
    if not isinstance(data, dict):
        raise CliError("The issue file needs a non-empty string 'title' (and optional 'body')")
    return issue_from_fields(number, data.get("title"), data.get("body"), data.get("labels"))


def _cmd_run(args, deps: Deps, out: TextIO, err: TextIO) -> int:
    settings = load_settings(deps)
    issue = _load_issue_file(args.issue_file, args.issue) if args.issue_file else None
    request = RunRequest(args.repo, args.issue, args.github, issue, args.allow_unpriced)
    db = open_db(deps)
    try:
        prep = prepare_run(request, settings, deps, db)
        try:
            result, report, record = execute_run(prep, settings, deps, db)
        except KeyboardInterrupt:
            print("Interrupted.", file=err)
            return 130
        except RunCrashed as exc:
            print(f"Run crashed: {exc}", file=err)
            return CRASH_CODE

        print(report, file=out)
        cost = (
            "unknown (prices not set)" if record.cost is None
            else format(record.cost, "f") + (" (estimated)" if record.cost_estimated else "")
        )
        print(f"Run #{record.id} saved. Cost: {cost}.", file=out)
        if args.cleanup:
            shutil.rmtree(result.workdir, ignore_errors=True)
            print("Working copy removed.", file=out)
        else:
            print(f"Working copy: {result.workdir} (branch {result.branch})", file=out)
        return EXIT[result.status]
    finally:
        db.close()


def _cmd_eval(args, deps: Deps, out: TextIO) -> int:
    settings = load_settings(deps)
    models = list(dict.fromkeys(m.strip() for m in args.models.split(",") if m.strip()))
    if not models:
        raise CliError("--models needs at least one model ID")
    unpriced = [m for m in models if None in settings.prices_for(m)]
    if unpriced and not (settings.allow_unpriced or args.allow_unpriced):
        raise CliError(
            f"No prices for {', '.join(unpriced)}, so cost cannot be measured. Set "
            "PRICE_IN_PER_M and PRICE_OUT_PER_M for GMI_MODEL, or MODEL_PRICES for other "
            "models, or pass --allow-unpriced."
        )
    try:
        issues = load_benchmark(args.issues)
    except EvalError as exc:
        raise CliError(str(exc)) from None
    planned = len(models) * len(issues)
    if planned > args.max_runs:
        raise CliError(
            f"Planned {planned} runs ({len(models)} models x {len(issues)} issues) is more than "
            f"--max-runs {args.max_runs}. Raise it if you mean it."
        )
    budgets = RunBudgets.from_settings(settings)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else Path("evals") / "results" / stamp
    print(f"Planned {planned} runs ({len(models)} models x {len(issues)} issues). "
          f"Each run is capped at {budgets.max_tokens} tokens.", file=out)
    db = open_db(deps)
    try:
        results = asyncio.run(
            run_eval(
                issues,
                models,
                lambda model: deps.llm(settings.model_copy(update={"gmi_model": model})),
                lambda: deps.sandbox(settings),
                out_dir,
                budgets,
                base_dir=Path(args.base_dir),
                prices_for=settings.prices_for,
                db=db,
                spend_cap=settings.daily_spend_cap,
                benchmark_path=Path(args.issues),
                profile=settings.sandbox_profile,
            )
        )
    finally:
        db.close()
    print(comparison_table(metrics_by_model(results)), file=out)
    if len(results) < planned:
        print(f"Stopped early: {len(results)} of {planned} runs finished (see meta.json).",
              file=out)
    print(f"Results saved to {out_dir}", file=out)
    return 0


def _cmd_compare(args, out: TextIO) -> int:
    try:
        results = load_results(args.results_dir)
    except EvalError as exc:
        raise CliError(f"No eval results found: {exc}") from None
    print(comparison_table(metrics_by_model(results)), file=out)
    return 0


# --- approve / reject -----------------------------------------------------------------------------

def _cmd_approve(args, deps: Deps, out: TextIO) -> int:
    """Check everything, show what will be sent, ask, then open a draft PR."""
    db = open_db(deps)
    try:
        plan = plan_approval(db, deps, args.run_id, args.closes_issue)
        run = plan.run
        print(f"Run #{run.id}: issue #{run.issue_number} {run.issue_title}", file=out)
        print(f"Target: {plan.owner}/{plan.repo}, new branch {plan.branch} "
              f"from {plan.base_sha[:12]}", file=out)
        print(f"Files: {', '.join(f.path for f in plan.files)}", file=out)
        print(f"Tests: {run.status}. The PR body says '{plan.keyword} #{run.issue_number}'.",
              file=out)
        if not args.yes:
            try:
                answer = deps.ask("Type yes to create a draft pull request: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                print("Not approved. Nothing was sent to GitHub.", file=out)
                return 1
        url = open_draft_pr(db, deps, plan, args.note)
        print(f"Draft PR created: {url}", file=out)
        return 0
    finally:
        db.close()


def _cmd_reject(args, deps: Deps, out: TextIO) -> int:
    db = open_db(deps)
    try:
        run = reject_run(db, args.run_id, args.note)
        print(f"Run #{run.id} rejected. Nothing was sent to GitHub.", file=out)
        return 0
    finally:
        db.close()


# --- serve ----------------------------------------------------------------------------------------

def _cmd_serve(args, deps: Deps, out: TextIO) -> int:
    from app.api import ApiError, create_app

    try:
        app = create_app(deps)
    except ApiError as exc:
        raise CliError(str(exc)) from None
    try:
        import uvicorn
    except ImportError:
        raise CliError("uvicorn is not installed: pip install -r requirements.txt") from None
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {args.host}; anyone who can reach it and has the API key "
              "can start runs.", file=out)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
