"""
Check a converted dataset with phys-ai-dataprofiler, using only its public 0.4 API.

The profiler inspects each output file the same way it inspects a Hub dataset (the Parquet table and
the MP4 video, read from disk), and its requirements engine gives the verdict: exit code 0 pass,
1 fail, 4 unknown. The JSON report and verdict are saved next to the dataset in meta/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from phys_ai_dataprofiler.connectors.huggingface import (
    RunReport, _to_jsonable, build_totals, inspect_local_file, render_markdown,
)
from phys_ai_dataprofiler.evaluate import evaluate, print_verdict, render_verdict_markdown, verdict_to_dict
from phys_ai_dataprofiler.remote.mp4 import declared_videos
from phys_ai_dataprofiler.requirements import RequirementsError, load_requirements

from phys_ai_dataconvertor.model import ConversionError

# The files that hold the recordings; meta/ holds bookkeeping tables, not data.
DATA_GLOBS = ("data/**/*.parquet", "videos/**/*.mp4")


def profile(root: Path, log=lambda m: None) -> RunReport:
    """The profiler's run report for the dataset at `root` (its data table and videos)."""
    info_path = root / "meta" / "info.json"
    video_info = declared_videos(json.loads(info_path.read_text())) if info_path.is_file() else {}
    files = sorted({p for g in DATA_GLOBS for p in root.glob(g) if p.is_file()})
    reports = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        rep = inspect_local_file(rel, p, p.stat().st_size, video_info=video_info)
        log(f"  profiled {rel}: {rep.detected_format} {'ok' if rep.valid else rep.error}")
        reports.append(rep)
    run = RunReport(
        repo_id=root.name, repo_type="dataset", revision=None, resolved_sha=None, authenticated_as=None,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), mode="download",
        download_dir=str(root), files_listed=len(files), files_selected=len(files), files_downloaded=0,
        files_skipped=[], total_bytes_selected=sum(p.stat().st_size for p in files), files=reports,
        totals=build_totals(reports), run_cost_s=0.0)
    return run


def check(root: str | Path, requirements: str, log=lambda m: None) -> int:
    """Profile the dataset at `root` and check it against `requirements` (a file or preset:NAME).
    Returns the verdict's exit code: 0 pass, 1 fail, 4 unknown."""
    root = Path(root)
    try:
        req = load_requirements(requirements)
    except RequirementsError as exc:
        raise ConversionError(str(exc)) from exc
    run = profile(root, log)
    report = _to_jsonable(run)
    verdict = evaluate(report, req)
    run.verdict = verdict_to_dict(verdict)
    print_verdict(verdict, log)
    meta = root / "meta"
    (meta / "check.json").write_text(json.dumps(_to_jsonable(run), indent=2))
    (meta / "check.md").write_text(render_markdown(run, render_verdict_markdown(verdict)))
    log(f"Check report: {meta / 'check.md'}")
    return verdict.exit_code
