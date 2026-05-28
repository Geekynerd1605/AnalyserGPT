import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import TextMessage
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")

from config.constants import WORK_DIR_DOCKER
from config.docker_util import (
    getDockerCommandLineCodeExecutor,
    start_docker_container,
    stop_docker_container,
)
from evals.schema import EvalCase, EvalCaseFile
from models.openai_model_client import get_model_client
from teams.analyzer_gpt import GetDataAnalyzerTeam


WORK_DIR = Path(WORK_DIR_DOCKER)
CODE_BLOCK_RE = re.compile(r"```(?:python|sh)?", re.IGNORECASE)
EXECUTOR_ERROR_RE = re.compile(
    r"(error|traceback|failed|exception|no code blocks found)", re.IGNORECASE
)


def _load_cases(cases_file: Path) -> list[EvalCase]:
    raw = json.loads(cases_file.read_text())
    return EvalCaseFile.model_validate(raw).cases


def _snapshot_png_mtimes() -> dict[str, float]:
    if not WORK_DIR.exists():
        return {}
    snapshot: dict[str, float] = {}
    for png in WORK_DIR.glob("*.png"):
        try:
            snapshot[str(png.resolve())] = png.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _new_pngs_since(snapshot: dict[str, float]) -> list[str]:
    if not WORK_DIR.exists():
        return []
    changed: list[str] = []
    for png in WORK_DIR.glob("*.png"):
        try:
            stat = png.stat()
        except OSError:
            continue
        resolved = str(png.resolve())
        previous = snapshot.get(resolved)
        if previous is None or stat.st_mtime > previous + 0.05:
            changed.append(resolved)
    return sorted(changed)


def _copy_dataset_to_workdir(dataset_rel_path: str) -> None:
    source = ROOT_DIR / dataset_rel_path
    if not source.exists():
        raise FileNotFoundError(f"Dataset not found: {source}")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, WORK_DIR / "data.csv")


def _build_task(prompt: str, original_dataset: str) -> str:
    return (
        "The dataset is in the working directory as `data.csv` "
        f"(copied from `{original_dataset}`). Always load it with pd.read_csv('data.csv').\n\n"
        f"User question: {prompt}"
    )


async def _run_single_case(
    case: EvalCase,
    model_client: Any,
    docker: Any,
    output_dir: Path,
) -> dict[str, Any]:
    _copy_dataset_to_workdir(case.dataset)
    snapshot = _snapshot_png_mtimes()
    started = time.time()

    team = GetDataAnalyzerTeam(docker, model_client)
    task = _build_task(case.prompt, case.dataset)

    transcript: list[dict[str, str]] = []
    analyzer_texts: list[str] = []
    executor_texts: list[str] = []
    stop_reason = ""
    run_exception = None

    try:
        async for message in team.run_stream(task=task):
            if isinstance(message, TextMessage):
                entry = {"source": message.source, "content": message.content}
                transcript.append(entry)
                if message.source.startswith("DataAnalyzerAgent"):
                    analyzer_texts.append(message.content)
                if message.source.startswith("CodeExecutorAgent"):
                    executor_texts.append(message.content)
            elif isinstance(message, TaskResult):
                stop_reason = message.stop_reason
    except Exception as exc:  # pragma: no cover - runtime-dependent
        run_exception = str(exc)

    new_pngs = _new_pngs_since(snapshot)
    elapsed_seconds = round(time.time() - started, 3)

    all_analyzer = "\n".join(analyzer_texts)
    all_executor = "\n".join(executor_texts)

    execution_success = run_exception is None and not EXECUTOR_ERROR_RE.search(all_executor)
    task_completion = "STOP" in all_analyzer or "Text 'STOP'" in stop_reason
    code_generated = bool(CODE_BLOCK_RE.search(all_analyzer))
    chart_generated = len(new_pngs) > 0

    score_components = {
        "task_completion": int(task_completion),
        "execution_success": int(execution_success),
        "code_generated": int(code_generated),
        "chart_generated": int(chart_generated),
    }
    score = round(sum(score_components.values()) / len(score_components), 3)

    result = {
        "case_id": case.case_id,
        "dataset": case.dataset,
        "prompt": case.prompt,
        "tags": case.tags,
        "elapsed_seconds": elapsed_seconds,
        "stop_reason": stop_reason,
        "exception": run_exception,
        "new_pngs": new_pngs,
        "metrics": {
            **score_components,
            "score": score,
        },
        "transcript": transcript,
    }

    (output_dir / "cases").mkdir(parents=True, exist_ok=True)
    (output_dir / "cases" / f"{case.case_id}.json").write_text(
        json.dumps(result, indent=2)
    )
    return result


def _write_summary(run_dir: Path, case_results: list[dict[str, Any]], started_at: datetime) -> None:
    total = len(case_results)
    passed = sum(1 for r in case_results if r["metrics"]["score"] >= 0.75)
    avg_score = round(sum(r["metrics"]["score"] for r in case_results) / max(total, 1), 3)

    summary = {
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": round(passed / max(total, 1), 3),
        "avg_score": avg_score,
        "cases": [
            {
                "case_id": r["case_id"],
                "score": r["metrics"]["score"],
                "execution_success": r["metrics"]["execution_success"],
                "task_completion": r["metrics"]["task_completion"],
                "code_generated": r["metrics"]["code_generated"],
                "chart_generated": r["metrics"]["chart_generated"],
                "elapsed_seconds": r["elapsed_seconds"],
            }
            for r in case_results
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    lines = [
        "# Evaluation Run (Phase A)",
        "",
        f"- Started (UTC): {summary['started_at_utc']}",
        f"- Completed (UTC): {summary['completed_at_utc']}",
        f"- Total cases: {summary['total_cases']}",
        f"- Passed (score >= 0.75): {summary['passed_cases']}",
        f"- Pass rate: {summary['pass_rate']}",
        f"- Average score: {summary['avg_score']}",
        "",
        "## Cases",
        "",
    ]
    for row in summary["cases"]:
        lines.extend(
            [
                f"- `{row['case_id']}`: score={row['score']} "
                f"(exec={row['execution_success']}, task={row['task_completion']}, "
                f"code={row['code_generated']}, chart={row['chart_generated']}) "
                f"in {row['elapsed_seconds']}s",
            ]
        )

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


async def _run_eval(cases_file: Path, max_cases: int | None) -> Path:
    cases = _load_cases(cases_file)
    if max_cases is not None:
        cases = cases[:max_cases]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ROOT_DIR / "eval_runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    model_client = get_model_client()
    docker = getDockerCommandLineCodeExecutor()
    case_results: list[dict[str, Any]] = []

    await start_docker_container(docker)
    try:
        for case in cases:
            print(f"[eval] running case: {case.case_id}")
            result = await _run_single_case(case, model_client, docker, run_dir)
            case_results.append(result)
    finally:
        await stop_docker_container(docker)

    _write_summary(run_dir, case_results, started_at)
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AnalyserGPT Phase A evaluations.")
    parser.add_argument(
        "--cases-file",
        default="evals/cases/phase_a_cases.json",
        help="Path to evaluation cases JSON (relative to repo root).",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="Optional cap on number of cases to run.",
    )
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set. Create `.env` from `.env.example` "
            "and set OPENAI_API_KEY before running evals."
        )

    cases_file = ROOT_DIR / args.cases_file
    run_dir = asyncio.run(_run_eval(cases_file, args.max_cases))
    print(f"[eval] complete: {run_dir}")
    print(f"[eval] summary: {run_dir / 'summary.md'}")


if __name__ == "__main__":
    main()
