"""
Recordings in, a training dataset out: each input file is one episode.

    from phys_ai_dataconvertor import convert, ConvertOptions
    report = convert(["rec1.mcap", "rec2.mcap"], "./pick_bottles", ConvertOptions(task="pick up the bottles"))

The work happens in three swappable parts: a reader per input format (`readers/`), the format-free
alignment of one recording into an episode (`episode.py`), and a writer per output format (`writers/`).
The output is built in `<output>.partial` and renamed only when complete, so a failed or interrupted
conversion never leaves a half-written dataset.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from phys_ai_dataconvertor import __version__
from phys_ai_dataconvertor import episode as ep_mod
from phys_ai_dataconvertor.episode import EpisodeReport
from phys_ai_dataconvertor.images import Decoder
from phys_ai_dataconvertor.model import ConversionError
from phys_ai_dataconvertor.readers import reader_for
from phys_ai_dataconvertor.writers import WRITERS


@dataclass
class ConvertOptions:
    camera: str | None = None                 # image topic; None picks the highest-resolution one
    state_topic: str | None = None            # joint state topic; None picks the busiest one
    action_topics: list[str] | None = None    # joint command topics, concatenated in this order
    state_joints: list[str] | None = None     # joints for observation.state; None = the commanded joints
    no_action: bool = False
    task: str = "teleoperation"
    fps: int | None = None                    # None: the camera's measured rate, rounded
    tolerance_ms: float | None = None         # None: half a frame interval
    clock: str = "auto"                       # "auto", "header" or "log" (see episode.py)
    camera_key: str | None = None             # feature name; None: observation.images.<from the topic>
    robot_type: str | None = None
    output_format: str = "lerobot-v3"
    vcodec: str = "libsvtav1"                 # LeRobot's default; "h264" encodes faster
    crf: int = 30
    gop: int = 2                              # keyframe every 2 frames, as LeRobot writes (fast seeking)
    overwrite: bool = False


@dataclass
class ConversionReport:
    output: str
    output_format: str
    fps: int
    camera_topic: str
    camera_key: str
    resolution: list[int]
    state_topic: str
    state_names: list[str]
    action_topics: list[str]
    action_names: list[str]
    tolerance_ms: float
    episodes: list[EpisodeReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return sum(e.frames for e in self.episodes)


_CAMERA_NOISE = {"compressed", "image_raw", "image_rect_color", "image_rect_raw", "image_rect", "image",
                 "image_color", "color", "raw", "rgb", "zed_node", "camera", "cam"}


def camera_key(topic: str) -> str:
    """Feature name from an image topic: /zed/zed_node/left/image_rect_color/compressed -> zed_left,
    /camera_left/camera_left/color/image_rect_raw/compressed -> camera_left, /wrist/image_raw -> wrist."""
    parts = [p for p in topic.strip("/").split("/") if p]
    kept = list(dict.fromkeys(p for p in parts if p.lower() not in _CAMERA_NOISE))
    if not kept:
        kept = [p for p in parts if p.lower() != "compressed"][:1] or ["camera"]
    return "observation.images." + "".join(c if c.isalnum() or c == "_" else "_" for c in "_".join(kept))


def _largest_camera(path: Path, topics: list[str]) -> str:
    """The image topic with the most pixels in its first frame."""
    decode = Decoder()
    sizes = {}
    for t, img in reader_for(path).first_images(path, topics).items():
        try:
            rgb = decode(img)
            sizes[t] = rgb.shape[0] * rgb.shape[1]
        except ConversionError:
            sizes[t] = 0
    return max(topics, key=lambda t: sizes.get(t, 0))


def convert(inputs: list[str | Path], output: str | Path, opts: ConvertOptions | None = None,
            log=None) -> ConversionReport:
    opts = opts or ConvertOptions()
    log = log or (lambda m: None)
    if opts.output_format not in WRITERS:
        raise ConversionError(f"unknown output format {opts.output_format!r} (supported: {', '.join(WRITERS)})")
    writer_mod = WRITERS[opts.output_format]
    paths = [Path(p) for p in inputs]
    if not paths:
        raise ConversionError("no input files")
    for p in paths:
        if not p.is_file():
            raise ConversionError(f"{p}: not a file")
        reader_for(p)                         # fail early on an unsupported format
    out = Path(output)
    if out.exists() and any(out.iterdir()):
        if not opts.overwrite:
            raise ConversionError(f"{out} exists and is not empty; pass --overwrite to replace it")
        if not writer_mod.is_dataset(out):
            raise ConversionError(f"{out} is not empty and is not a {opts.output_format} dataset; refusing to delete it")
        shutil.rmtree(out)

    sel = ep_mod.select(paths[0], opts.camera, opts.state_topic, opts.action_topics, opts.no_action, _largest_camera)
    key = opts.camera_key or camera_key(sel.camera)
    log(f"camera {sel.camera} -> {key}; state {sel.state_topic}; action {', '.join(sel.action_topics) or '(none)'}")

    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    fps, tol_ns = opts.fps, None
    video = None
    report: ConversionReport | None = None
    written: list[dict] = []
    try:
        for ei, p in enumerate(paths):
            ep, fps, tol_ns = ep_mod.build(p, sel, opts.clock, opts.state_joints, fps, tol_ns, opts.tolerance_ms)
            if report is not None and ep.action_names != report.action_names:
                raise ConversionError(f"{p.name}: action joints {ep.action_names} differ from the first "
                                      f"episode's {report.action_names}")
            decode = Decoder()
            video_from = video.frames / fps if video else 0.0
            for i, img in enumerate(ep.frames):
                rgb = decode(img)
                if video is None:
                    video = writer_mod.VideoWriter(writer_mod.video_path(tmp, key), fps, rgb.shape[1], rgb.shape[0],
                                                   opts.vcodec, opts.crf, opts.gop)
                    report = ConversionReport(
                        output=str(out), output_format=opts.output_format, fps=fps, camera_topic=sel.camera,
                        camera_key=key, resolution=[rgb.shape[1], rgb.shape[0]], state_topic=sel.state_topic,
                        state_names=ep.state_names, action_topics=sel.action_topics, action_names=ep.action_names,
                        tolerance_ms=tol_ns / 1e6)
                video.add(rgb)
                if (i + 1) % 300 == 0:
                    log(f"    {p.name}: {i + 1}/{len(ep.frames)} frames")
            written.append({"state": ep.state, "action": ep.action, "video_from_s": video_from})
            report.episodes.append(ep.report)
            r = ep.report
            log(f"  episode {ei}: {p.name}: {r.frames} frames at {fps} fps ({r.clock} clock), "
                f"state within {r.state_age_ms_max:.1f} ms"
                + (f", action within {r.action_age_ms_max:.1f} ms" if r.action_age_ms_max is not None else "")
                + (f"; dropped {r.dropped_start} leading / {r.dropped_end} trailing frame(s)"
                   if r.dropped_start or r.dropped_end else ""))
        video.close()
        image_stats = video.stats.snapshot()
        video = None
        _notes(report)
        writer_mod.write(
            tmp, fps=fps, camera_key=key, resolution=report.resolution, vcodec=opts.vcodec, task=opts.task,
            robot_type=opts.robot_type, state_names=report.state_names, action_names=report.action_names,
            episodes=written, image_stats=image_stats,
            provenance={"converter": f"phys-ai-dataconvertor {__version__}", **asdict(report),
                        "total_frames": report.total_frames})
    except BaseException:
        if video is not None:
            video.abort()
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if out.exists():
        out.rmdir()
    tmp.rename(out)
    return report


def _notes(report: ConversionReport) -> None:
    eps = report.episodes
    logged = [e for e in eps if e.clock == "log" and len(e.write_delay_ms) > 1]
    if logged:
        d = logged[0].write_delay_ms
        spread = max(d.values()) - min(d.values())
        if spread > 5:
            report.notes.append(f"aligned on recorder time (--clock log), but topics reach the recorder up to "
                                f"{spread:.0f} ms apart after capture; --clock auto pairs on capture time")
    if any(e.clock == "mixed" for e in eps):
        report.notes.append("the command topics carry no capture time; they were placed on the capture timeline "
                            "using the joint-state topic's recorder delay")
    skewed = [e for e in eps if e.clock_offset_ms is not None and abs(e.clock_offset_ms) > 1000]
    if skewed:
        worst = max(skewed, key=lambda e: abs(e.clock_offset_ms))
        off = worst.clock_offset_ms
        size = f"{off / 3.6e6:,.1f} h" if abs(off) >= 3.6e6 else f"{off / 1000:,.1f} s"
        report.notes.append(
            f"in {len(skewed)} of {len(eps)} episode(s) the camera's capture clock (header.stamp) and the "
            f"recorder's clock (log_time) differ by more than 1 s (up to {size}, {Path(worst.source).name}): the "
            f"robot's clock was not synchronised. Timing inside an episode is unaffected; absolute times in "
            f"those recordings are not wall-clock times.")
    gappy = [e for e in eps if e.camera_gaps]
    if gappy:
        e = max(gappy, key=lambda x: x.camera_max_gap_ms)
        report.notes.append(
            f"{sum(x.camera_gaps for x in gappy)} camera interval(s) over 1.5 frames in {len(gappy)} episode(s), "
            f"worst {e.camera_max_gap_ms:.0f} ms ({Path(e.source).name}). Output timestamps are evenly spaced, so "
            f"the video plays through these gaps; state and action still come from the real frame times.")
    off_rate = [e.camera_rate_hz for e in eps if abs(e.camera_rate_hz - report.fps) > 0.05 * report.fps]
    if off_rate:
        report.notes.append(f"{len(off_rate)} episode(s) recorded the camera at {off_rate[0]:.2f} Hz, more than 5% "
                            f"from the dataset's {report.fps} fps; their timestamps were re-spaced to {report.fps} fps")


def print_report(r: ConversionReport, log=lambda m: print(m, file=sys.stderr)) -> None:
    log("")
    log(f"Wrote {r.output} ({r.output_format}): {len(r.episodes)} episode(s), {r.total_frames} frames at "
        f"{r.fps} fps ({r.total_frames / r.fps:.1f} s)")
    log(f"  {r.camera_key}: {r.camera_topic}, {r.resolution[0]}x{r.resolution[1]}")
    log(f"  observation.state: {len(r.state_names)} joints from {r.state_topic}")
    if r.action_names:
        log(f"  action: {len(r.action_names)} joints from {len(r.action_topics)} command topic(s)")
    log(f"  alignment: nearest timestamp within {r.tolerance_ms:.1f} ms; worst state match "
        f"{max(e.state_age_ms_max for e in r.episodes):.1f} ms"
        + (f", worst action match {max(e.action_age_ms_max for e in r.episodes):.1f} ms" if r.action_names else ""))
    for n in r.notes:
        log(f"  note: {n}")
