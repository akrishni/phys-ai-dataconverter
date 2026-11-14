"""
phys-ai-dataconvertor: convert robot recordings into training datasets.

    phys-ai-dataconvertor recordings/ -o ./pick_bottles --task "pick up the bottles"
    phys-ai-dataconvertor rec1.mcap rec2.mcap -o ./ds --check preset:teleop-imitation

Exit codes: 0 converted (and, with --check, the check passed), 1 the --check failed,
2 the conversion was refused (the message says why), 4 the --check was unknown.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from phys_ai_dataconvertor import __version__

PROG = "phys-ai-dataconvertor"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Convert robot recordings (one episode per file) into a LeRobot v3 dataset: one camera, "
                    "observation.state from a joint state topic and action from joint command topics, aligned "
                    "to the camera frames by nearest capture time. Input: ROS 2 MCAP.",
        epilog="Example: phys-ai-dataconvertor recordings/ -o ./pick_bottles --task \"pick up the bottles\" "
               "--camera /zed/zed_node/left/image_rect_color/compressed --check preset:teleop-imitation",
    )
    p.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    p.add_argument("inputs", nargs="+", help="Recording files, or folders searched for them (one episode per file)")
    p.add_argument("--output", "-o", required=True, help="Folder for the new dataset")
    p.add_argument("--task", default="teleoperation", help="Task description stored with every episode")
    s = p.add_argument_group("streams (defaults are picked from the first file and printed)")
    s.add_argument("--camera", help="Image topic (default: the highest-resolution one)")
    s.add_argument("--camera-key", help="Feature name (default: observation.images.<from the topic>)")
    s.add_argument("--state-topic", help="Joint state topic (default: the busiest one)")
    s.add_argument("--state-joints", help="Comma-separated joint names for observation.state "
                                          "(default: the joints the action topics command)")
    s.add_argument("--action-topic", action="append", dest="action_topics",
                   help="Joint command topic; repeat for several, concatenated in order (default: all, sorted)")
    s.add_argument("--no-action", action="store_true", help="Write no action column")
    t = p.add_argument_group("timing")
    t.add_argument("--fps", type=int, help="Dataset frame rate (default: the camera's measured rate, rounded)")
    t.add_argument("--tolerance-ms", type=float,
                   help="Largest allowed distance from a frame to its matched state/command (default: half a frame)")
    t.add_argument("--clock", choices=["auto", "header", "log"], default="auto",
                   help="auto (default): pair camera and state on capture time (header.stamp), placing unstamped "
                        "commands via the state topic's recorder delay; header: capture time for every topic; "
                        "log: recorder write time for every topic")
    v = p.add_argument_group("output")
    v.add_argument("--format", dest="output_format", default="lerobot-v3", choices=["lerobot-v3"])
    v.add_argument("--vcodec", default="libsvtav1", help="libsvtav1 (LeRobot's default) or h264 (faster)")
    v.add_argument("--crf", type=int, default=30, help="Video quality; lower is better and larger (default 30)")
    v.add_argument("--robot-type", help="Stored in info.json")
    v.add_argument("--overwrite", action="store_true", help="Replace an existing dataset at --output")
    p.add_argument("--check", metavar="FILE|preset:NAME",
                   help="After converting, profile the output with phys-ai-dataprofiler and check it against "
                        "these requirements (a YAML file or a profiler preset); the report goes in meta/")
    p.add_argument("--quiet", action="store_true")
    return p


def _inputs(args: list[str]) -> list[Path]:
    from phys_ai_dataconvertor.readers import input_extensions
    exts = input_extensions()
    files: list[Path] = []
    for i in args:
        q = Path(i)
        if q.is_dir():
            found = sorted(f for f in q.rglob("*") if f.is_file() and f.suffix.lower() in exts)
            if not found:
                raise SystemExit(f"Error: no {' '.join(exts)} files under {q}")
            files += found
        else:
            files.append(q)
    return files


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    log = (lambda m: None) if a.quiet else (lambda m: print(m, file=sys.stderr))
    from phys_ai_dataconvertor.convert import ConvertOptions, convert, print_report
    from phys_ai_dataconvertor.model import ConversionError
    try:
        files = _inputs(a.inputs)
        report = convert(files, a.output, ConvertOptions(
            camera=a.camera, camera_key=a.camera_key, state_topic=a.state_topic,
            state_joints=[j.strip() for j in a.state_joints.split(",")] if a.state_joints else None,
            action_topics=a.action_topics, no_action=a.no_action, task=a.task, fps=a.fps,
            tolerance_ms=a.tolerance_ms, clock=a.clock, output_format=a.output_format, vcodec=a.vcodec,
            crf=a.crf, robot_type=a.robot_type, overwrite=a.overwrite), log=log)
    except ConversionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    print_report(report, log)
    if not a.check:
        log(f"Check it: {PROG} ... --check preset:teleop-imitation (or a requirements file)")
        return 0
    from phys_ai_dataconvertor.verify import check
    log("")
    log(f"Checking {a.output} against {a.check} with phys-ai-dataprofiler")
    try:
        return check(a.output, a.check, log if not a.quiet else (lambda m: None))
    except ConversionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
