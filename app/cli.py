"""The `repopilot` command line.

    repopilot run --repo <path-or-github-url> --issue <n> [--github owner/repo]
    repopilot runs | show <id> | usage [--today]
    repopilot approve <id> [--yes] [--closes-issue] | reject <id>

Exit codes for `run`: 0 passed, 1 failed, 3 inconclusive, 4 aborted or crashed,
5 usage, configuration or budget error (2 is argparse's own "bad arguments").
All collaborators are injected through `Deps`, so tests use fakes.
"""

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.fix import RunBudgets, RunStatus, run_issue
from app.agent.investigate import LLM
from app.config import Settings
from app.db import BudgetBlocked, Database
from app.evals import (
    EvalError,
    comparison_table,
    load_benchmark,
    load_results,
    metrics_by_model,
    run_eval,
)
from app.github import GitHubClient, GitHubError, GitHubRateLimit, Issue
from app.llm import LLMClient
from app.patch import PatchError
from app.paths import cache_dir, default_db_path
from app.pr import GitHubWriter, PRError, collect_changes, pr_body, pr_title
from app.report import build_report
from app.sandbox import Sandbox, default_profiles
from app.workspace import Workspace, WorkspaceError

EXIT = {
    RunStatus.PASSED: 0,
    RunStatus.FAILED: 1,
    RunStatus.INCONCLUSIVE: 3,
    RunStatus.ABORTED: 4,
}
CRASH_CODE = 4
USAGE_CODE = 5
_SLUG = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})$")
_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$"
)


class CliError(Exception):
    """A problem the user can fix. Printed as one line; exit code 5."""


class _Paths(BaseSettings):
    """Only what commands without the model need, so they work without a GMI key."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    db_path: str | None = None
    github_write_token: SecretStr | None = None


def _default_clone(url: str, dest: Path) -> Path:
    return Workspace.clone(url, dest).root


def _default_sandbox(settings: Settings) -> Sandbox:
    profiles = default_profiles(settings.sandbox_image, settings.sandbox_timeout_s)
    return Sandbox(profiles=profiles)


@dataclass
class Deps:
    """Everything the CLI talks to. Replace pieces in tests."""

    settings: Callable[[], Settings] = Settings
    llm: Callable[[Settings], LLM] = LLMClient
    sandbox: Callable[[Settings], Sandbox] = _default_sandbox
    github: Callable[[Settings], GitHubClient] = field(
        default=lambda s: GitHubClient(token=s.github_token)
    )
    clone: Callable[[str, Path], Path] = _default_clone
    paths: Callable[[], _Paths] = _Paths
    writer: Callable[[SecretStr], GitHubWriter] = GitHubWriter
    ask: Callable[[str], str] = input


def parse_github_slug(text: str) -> tuple[str, str] | None:
    """`owner/repo`, an https GitHub URL, or an ssh URL -> (owner, repo), else None."""
    text = text.strip()
    match = _URL.match(text) or _SLUG.match(text)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo in (".", ".."):
        return None
    return owner, repo.removesuffix(".git")


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
        return _cmd_read(args, deps, out)
    except CliError as exc:
        print(f"error: {exc}", file=err)
        return USAGE_CODE


# --- read-only commands --------------------------------------------------------------------------

def _open_db(deps: Deps) -> Database:
    path = deps.paths().db_path
    return Database(Path(path) if path else default_db_path())


def _clip_left(text: str, width: int) -> str:
    """Keep the end of `text` (the repo name and issue number) when it is too long."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


def _cmd_read(args, deps: Deps, out: TextIO) -> int:
    db = _open_db(deps)
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

def _load_settings(deps: Deps) -> Settings:
    try:
        return deps.settings()
    except ValidationError as exc:
        missing = [str(e["loc"][0]).upper() for e in exc.errors() if e["type"] == "missing"]
        bad = [str(e["loc"][0]).upper() for e in exc.errors() if e["type"] != "missing"]
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if bad:
            parts.append(f"invalid {', '.join(bad)}")
        raise CliError(
            f"Configuration problem: {'; '.join(parts)}. Copy .env.example to .env and fill it in."
        ) from None


def _local_slug(path: Path) -> tuple[str, str] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_github_slug(proc.stdout) if proc.returncode == 0 else None


def _load_issue_file(path: str, number: int) -> Issue:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise CliError("Could not read the issue file (it must be valid JSON)") from None
    if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not data["title"]:
        raise CliError("The issue file needs a non-empty string 'title' (and optional 'body')")
    labels = data.get("labels") if isinstance(data.get("labels"), list) else []
    return Issue(
        number=number,
        title=data["title"][:300],
        body=str(data.get("body") or "")[:10_000],
        state="open",
        url="",
        author="local",
        labels=[str(x) for x in labels],
    )


def _cmd_run(args, deps: Deps, out: TextIO, err: TextIO) -> int:
    settings = _load_settings(deps)
    prices = (settings.price_in_per_m, settings.price_out_per_m)
    db = _open_db(deps)
    clone_dir: Path | None = None
    run_id: int | None = None
    try:
        try:
            db.check_can_start(
                settings.daily_spend_cap,
                settings.max_runs_per_day,
                priced=all(p is not None for p in prices),
                allow_unpriced=settings.allow_unpriced or args.allow_unpriced,
            )
        except BudgetBlocked as exc:
            raise CliError(str(exc)) from None

        source, slug, clone_dir = _resolve_repo(args, deps)
        issue = _get_issue(args, slug, settings, deps)
        label = f"{slug[0]}/{slug[1]}" if slug else str(source)

        run_id = db.start_run(label, issue.number, issue.title, settings.gmi_model)
        try:
            result = asyncio.run(
                run_issue(
                    deps.llm(settings),
                    source,
                    issue,
                    deps.sandbox(settings),
                    RunBudgets.from_settings(settings),
                    profile=settings.sandbox_profile,
                )
            )
        except PatchError as exc:
            db.fail_run(run_id, f"could not prepare the repository: {exc}")
            raise CliError(f"Could not prepare the repository: {exc}") from None
        except KeyboardInterrupt:
            db.fail_run(run_id, "interrupted")
            print("Interrupted.", file=err)
            return 130
        except Exception as exc:  # a bug or an unexpected failure: record it, do not hide it
            db.fail_run(run_id, f"{type(exc).__name__}: {exc}")
            print(f"Run crashed: {type(exc).__name__}: {exc}", file=err)
            return CRASH_CODE

        report = build_report(result, issue)
        record = db.finish_run(run_id, result, report, prices)
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
        if clone_dir is not None:
            shutil.rmtree(clone_dir, ignore_errors=True)
        db.close()


def _cmd_eval(args, deps: Deps, out: TextIO) -> int:
    settings = _load_settings(deps)
    prices = (settings.price_in_per_m, settings.price_out_per_m)
    if any(p is None for p in prices) and not (settings.allow_unpriced or args.allow_unpriced):
        raise CliError(
            "Prices are not set (PRICE_IN_PER_M, PRICE_OUT_PER_M), so cost cannot be measured. "
            "Set them, or pass --allow-unpriced."
        )
    models = list(dict.fromkeys(m.strip() for m in args.models.split(",") if m.strip()))
    if not models:
        raise CliError("--models needs at least one model ID")
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
    db = _open_db(deps)
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
                prices=prices,
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

def _decided_run(db: Database, run_id: int):
    run = db.get_run(run_id)
    if run is None:
        raise CliError(f"No run with id {run_id}")
    if run.pr_url:
        raise CliError(f"Run #{run_id} already has a draft PR: {run.pr_url}")
    return run


def _cmd_approve(args, deps: Deps, out: TextIO) -> int:
    """Check everything, show what will be sent, ask, record the approval, open a draft PR."""
    db = _open_db(deps)
    try:
        run = _decided_run(db, args.run_id)
        if any(a["decision"] == "rejected" for a in db.get_approvals(run.id)):
            raise CliError(f"Run #{run.id} was rejected; start a new run instead")
        if run.status != "passed":
            raise CliError(f"Only passed runs can be approved (run #{run.id} is {run.status})")
        slug = parse_github_slug(run.repo)
        if slug is None:
            raise CliError(f"Run #{run.id} is not linked to a GitHub repo (it ran on a local "
                           "path with no GitHub origin remote)")
        token = deps.paths().github_write_token
        if token is None:
            raise CliError("Set GITHUB_WRITE_TOKEN (fine-grained; Contents and Pull requests "
                           "read/write on this repo only) to open a PR")
        patches = db.get_patches(run.id)
        if not patches:
            raise CliError(f"Run #{run.id} has no patch")
        try:
            base, files = collect_changes(
                Path(run.workdir or ""), patches[0]["commit_sha"], patches[-1]["commit_sha"]
            )
        except PRError as exc:
            raise CliError(str(exc)) from None

        branch = f"repopilot/issue-{run.issue_number}-run-{run.id}"
        keyword = "Fixes" if args.closes_issue else "Refs"
        print(f"Run #{run.id}: issue #{run.issue_number} {run.issue_title}", file=out)
        print(f"Target: {slug[0]}/{slug[1]}, new branch {branch} from {base[:12]}", file=out)
        print(f"Files: {', '.join(f.path for f in files)}", file=out)
        print(f"Tests: {run.status}. The PR body says '{keyword} #{run.issue_number}'.", file=out)
        if not args.yes:
            try:
                answer = deps.ask("Type yes to create a draft pull request: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                print("Not approved. Nothing was sent to GitHub.", file=out)
                return 1

        db.record_approval(run.id, "approved", args.note)
        title = pr_title(run.issue_number, run.issue_title)
        body = pr_body(run.issue_number, run.id, run.report or "", args.closes_issue)
        try:
            url = asyncio.run(deps.writer(token).create_draft_pr(
                slug[0], slug[1], branch, base, files, title, body))
        except PRError as exc:
            raise CliError(f"Approved, but the draft PR was not created: {exc}") from None
        db.set_pr_url(run.id, url)
        print(f"Draft PR created: {url}", file=out)
        return 0
    finally:
        db.close()


def _cmd_reject(args, deps: Deps, out: TextIO) -> int:
    db = _open_db(deps)
    try:
        run = _decided_run(db, args.run_id)
        db.record_approval(run.id, "rejected", args.note)
        print(f"Run #{run.id} rejected. Nothing was sent to GitHub.", file=out)
        return 0
    finally:
        db.close()


def _resolve_repo(args, deps: Deps) -> tuple[Path, tuple[str, str] | None, Path | None]:
    """Return (path to a local git repo, github slug or None, temp clone dir or None)."""
    explicit = parse_github_slug(args.github) if args.github else None
    if args.github and explicit is None:
        raise CliError("--github must look like owner/repo")

    if args.repo.startswith(("https://", "git@")):
        slug = parse_github_slug(args.repo)
        if slug is None or not args.repo.startswith("https://"):
            raise CliError("Only https://github.com/<owner>/<repo> URLs are supported")
        clone_dir = cache_dir() / "clones" / uuid.uuid4().hex[:12]
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            path = deps.clone(args.repo, clone_dir)
        except WorkspaceError as exc:
            shutil.rmtree(clone_dir, ignore_errors=True)
            raise CliError(f"Could not clone: {exc}") from None
        return Path(path), explicit or slug, clone_dir

    path = Path(args.repo).expanduser()
    if not path.is_dir():
        raise CliError(f"Repository path not found: {args.repo}")
    if not (path / ".git").exists():
        raise CliError("The repository path is not a git repository")
    return path, explicit or _local_slug(path), None


def _get_issue(args, slug, settings: Settings, deps: Deps) -> Issue:
    if args.issue_file:
        return _load_issue_file(args.issue_file, args.issue)
    if slug is None:
        raise CliError("Cannot tell which GitHub repo this is; pass --github owner/repo")
    try:
        return asyncio.run(deps.github(settings).get_issue(slug[0], slug[1], args.issue))
    except GitHubRateLimit as exc:
        reset = f" (resets at Unix time {exc.reset_at})" if exc.reset_at else ""
        raise CliError(f"{exc}{reset}") from None
    except GitHubError as exc:
        raise CliError(str(exc)) from None


if __name__ == "__main__":
    raise SystemExit(main())
