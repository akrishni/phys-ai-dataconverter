#!/usr/bin/env python3
"""Checks for the converter, against ROS 2 MCAP files written here with known answers.

    python tests/test_convert.py          # offline
    python tests/test_convert.py --hub    # also converts a public teleop recording from the Hub

Every value in the output is checked against what was written into the source, the alignment against a
brute-force nearest match, and the output against the profiler (format, frame count, rates). When the
`lerobot` package is installed, the output is also loaded with LeRobotDataset.
Exits non-zero on the first failed check.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from mcap_ros2.writer import Writer  # noqa: E402

from phys_ai_dataconverter import ConversionError, ConvertOptions, convert  # noqa: E402
from phys_ai_dataconverter.align import nearest  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="convert_test_"))

HEADER = """
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec"""
IMAGE_DEF = "std_msgs/Header header\nstring format\nuint8[] data" + HEADER
RAW_DEF = ("std_msgs/Header header\nuint32 height\nuint32 width\nstring encoding\nuint8 is_bigendian\n"
           "uint32 step\nuint8[] data" + HEADER)
JOINT_DEF = "std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort" + HEADER
TRAJ_DEF = ("std_msgs/Header header\nstring[] joint_names\ntrajectory_msgs/JointTrajectoryPoint[] points" + HEADER
            + """
================================================================================
MSG: trajectory_msgs/JointTrajectoryPoint
float64[] positions
float64[] velocities
float64[] accelerations
float64[] effort
builtin_interfaces/Duration time_from_start
================================================================================
MSG: builtin_interfaces/Duration
int32 sec
uint32 nanosec""")

T0 = 1_700_000_000_000_000_000          # recorder clock
ROBOT_T0 = 1_600_000_000_000_000_000    # robot clock (unsynchronised, ~3 years behind)
ARM = ["j1", "j2", "j3"]
OTHER = ["wheel", "head"]


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def stamp(ns: int) -> dict:
    return {"stamp": {"sec": ns // 1_000_000_000, "nanosec": ns % 1_000_000_000}, "frame_id": ""}


def jpeg(k: int, w: int = 64, h: int = 48) -> bytes:
    """A frame whose content encodes k (a moving bar), so frame order can be checked after video encoding."""
    img = np.full((h, w, 3), 40, dtype=np.uint8)
    x = (k * 3) % (w - 8)
    img[:, x:x + 8] = (220, 120, 30)
    ctx = av.CodecContext.create("mjpeg", "w")
    ctx.width, ctx.height, ctx.pix_fmt = w, h, "yuvj420p"
    ctx.time_base = av.Rational(1, 30) if hasattr(av, "Rational") else __import__("fractions").Fraction(1, 30)
    pkts = ctx.encode(av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuvj420p"))
    pkts += ctx.encode(None)
    return b"".join(bytes(p) for p in pkts)


def bar_position(rgb: np.ndarray) -> int:
    return int(np.argmax(rgb[:, :, 0].mean(0) > 130))


def state_value(t_ns: int, j: int) -> float:
    return 0.1 * (j + 1) + 0.5 * np.sin(t_ns / 1e9 + j)


def command_value(t_ns: int, j: int) -> float:
    return 1.0 + 0.01 * (t_ns // 1_000_000) + j


def recording(path: Path, seconds: float = 2.0, fps: int = 30, joint_hz: int = 100, cam_delay_ms: float = 25.0,
              joint_delay_ms: float = 1.0, drop_frames: tuple = (), joint_gap: tuple | None = None,
              command_from_s: float = 0.0, raw: bool = False, extra_camera: bool = False,
              stamped_commands: bool = False, rng_seed: int = 0) -> Path:
    """A teleop recording: camera at `fps` (capture time + `cam_delay_ms` = write time), joint state at
    `joint_hz` (+ `joint_delay_ms`), and a leader command topic (unstamped, written ~1 ms after the joint
    reading it accompanies, as ROS teleop leaders do)."""
    rng = np.random.default_rng(rng_seed)
    msgs = []    # (log_time, topic, kind, payload)
    n_frames = int(seconds * fps)
    for k in range(n_frames):
        if k in drop_frames:
            continue
        cap = ROBOT_T0 + int(k * 1e9 / fps)
        log = T0 + int(k * 1e9 / fps) + int((cam_delay_ms + rng.uniform(-3, 3)) * 1e6)
        msgs.append((log, "/cam/image_raw/compressed" if not raw else "/cam/image_raw", "img", (cap, k)))
        if extra_camera:
            msgs.append((log + 1000, "/small/image_raw/compressed", "small", (cap, k)))
    for i in range(int(seconds * joint_hz)):
        rel = int(i * 1e9 / joint_hz)
        if joint_gap and joint_gap[0] <= rel / 1e9 < joint_gap[1]:
            continue
        cap = ROBOT_T0 + rel
        log = T0 + rel + int((joint_delay_ms + rng.uniform(-0.5, 0.5)) * 1e6)
        msgs.append((log, "/joint_states", "js", (cap, rel)))
        if rel / 1e9 >= command_from_s:
            msgs.append((log + 1_000_000, "/leader/arm/joint_trajectory", "cmd", (cap, rel)))
    msgs.sort(key=lambda m: m[0])
    with path.open("wb") as f:
        w = Writer(f)
        img = w.register_msgdef("sensor_msgs/msg/CompressedImage", IMAGE_DEF)
        rawsc = w.register_msgdef("sensor_msgs/msg/Image", RAW_DEF)
        js = w.register_msgdef("sensor_msgs/msg/JointState", JOINT_DEF)
        tr = w.register_msgdef("trajectory_msgs/msg/JointTrajectory", TRAJ_DEF)
        for log, topic, kind, (cap, x) in msgs:
            if kind == "img" and raw:
                pix = av.CodecContext.create("mjpeg", "r").decode(av.Packet(jpeg(x)))[0].to_ndarray(format="rgb24")
                w.write_message(topic, rawsc, {"header": stamp(cap), "height": 48, "width": 64, "encoding": "rgb8",
                                               "is_bigendian": 0, "step": 64 * 3, "data": pix.tobytes()}, log_time=log)
            elif kind in ("img", "small"):
                size = (64, 48) if kind == "img" else (32, 24)
                w.write_message(topic, img, {"header": stamp(cap), "format": "rgb8; jpeg compressed bgr8",
                                             "data": jpeg(x, *size)}, log_time=log)
            elif kind == "js":
                names = ARM + OTHER
                w.write_message(topic, js, {"header": stamp(cap), "name": names,
                                            "position": [state_value(x, j) for j in range(len(names))],
                                            "velocity": [0.0] * len(names), "effort": [0.0] * len(names)},
                                log_time=log)
            else:
                w.write_message(topic, tr, {"header": stamp(cap) if stamped_commands else stamp(0),
                                            "joint_names": ARM,
                                            "points": [{"positions": [command_value(x, j) for j in range(3)],
                                                        "velocities": [], "accelerations": [], "effort": [],
                                                        "time_from_start": {"sec": 0, "nanosec": 0}}]},
                                log_time=log)
        w.finish()
    return path


def out_dir(name: str) -> Path:
    return TMP / name


def load(out: Path) -> tuple[dict, list[dict], list[np.ndarray], dict]:
    info = json.loads((out / "meta" / "info.json").read_text())
    rows = pq.read_table(out / "data" / "chunk-000" / "file-000.parquet").to_pylist()
    vid = next((out / "videos").rglob("*.mp4"))
    frames = [f.to_ndarray(format="rgb24") for f in av.open(str(vid)).decode(video=0)]
    conv = json.loads((out / "meta" / "conversion.json").read_text())
    return info, rows, frames, conv


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_align() -> None:
    a = nearest(np.array([0, 10, 20, 35]), np.array([1, 9, 19, 21, 100]), tolerance_ns=5)
    check(a.index.tolist() == [0, 1, 2, -1] and a.age_ns.tolist() == [1, 1, 1, 14],
          f"nearest match, and a tick past the tolerance gets no value: {a.index.tolist()}")
    a = nearest(np.array([10]), np.array([5, 15]), tolerance_ns=10)
    check(a.index.tolist() == [0], "a tie goes to the earlier sample (no peeking at the future)")
    a = nearest(np.array([0, 1]), np.array([], dtype=np.int64), tolerance_ns=10)
    check(a.n_unmatched == 2, "no samples: nothing matched, no crash")


def test_basic() -> None:
    src = recording(TMP / "rec.mcap")
    rep = convert([src], out_dir("basic"), ConvertOptions(task="pick"))
    info, rows, frames, _ = load(out_dir("basic"))
    e = rep.episodes[0]
    check(info["codebase_version"] == "v3.0" and info["fps"] == 30 and info["total_frames"] == len(rows) == len(frames),
          f"LeRobot v3 layout, 30 fps, one video frame per row ({len(rows)} rows, {len(frames)} frames)")
    check(e.clock == "mixed" and len(rows) == 60 - e.dropped_start - e.dropped_end,
          f"camera and state on capture time, unstamped commands mapped onto it ({e.clock})")
    check(info["features"]["observation.state"]["names"] == ARM and info["features"]["action"]["names"] == ARM,
          "observation.state is the commanded joints, in command order (wheels and head left out)")
    # every value against the source formula, at the nearest joint reading to the frame's capture time
    ok_state = ok_action = True
    for r in rows:
        k = r["frame_index"] + e.dropped_start
        cap_rel = int(k * 1e9 / 30)
        j_rel = int(round(cap_rel / 1e7)) * 10_000_000          # nearest 100 Hz reading
        want = np.float32([state_value(j_rel, j) for j in range(3)])
        ok_state &= np.allclose(np.float32(r["observation.state"]), want, atol=0)
        ok_action &= np.allclose(np.float32(r["action"]), np.float32([command_value(j_rel, j) for j in range(3)]), atol=0)
    check(ok_state, "each frame's state is the joint reading nearest its capture time, exactly")
    check(ok_action, "each frame's action is the command sent with that joint reading, exactly")
    check(e.state_age_ms_max <= 5.0 and e.action_age_ms_max <= 5.0 + 1.0,
          f"matches within half a joint interval ({e.state_age_ms_max:.2f} ms state, {e.action_age_ms_max:.2f} ms action)")
    bars = [bar_position(f) for f in frames]
    want_bars = [bar_position(np.asarray(av.CodecContext.create("mjpeg", "r").decode(av.Packet(jpeg(k + e.dropped_start)))[0]
                                         .to_ndarray(format="rgb24"))) for k in range(len(frames))]
    check(bars == want_bars, "video frames are the source frames, in order (moving-bar position per frame)")
    check([r["timestamp"] for r in rows[:3]] == [np.float32(0), np.float32(1 / 30), np.float32(2 / 30)]
          and [r["index"] for r in rows] == list(range(len(rows))), "timestamps k/fps, global index 0..n-1")
    check(any("not synchronised" in n for n in rep.notes), "the 3-year robot clock offset is reported")
    tasks = pq.read_table(out_dir("basic") / "meta" / "tasks.parquet").to_pylist()
    check(tasks == [{"task_index": 0, "__index_level_0__": "pick"}], "task stored as LeRobot writes it")
    stats = json.loads((out_dir("basic") / "meta" / "stats.json").read_text())
    st = np.float64([r["observation.state"] for r in rows])
    check(np.allclose(stats["observation.state"]["mean"], st.mean(0)) and "q01" in stats["action"]
          and np.asarray(stats[rep.camera_key]["mean"]).shape == (3, 1, 1), "stats: per-joint mean, quantiles, image stats (3,1,1)")

    # log clock pairs each frame with a joint reading ~25 ms later than its capture
    rep_log = convert([src], out_dir("log"), ConvertOptions(clock="log"))
    _, rows_log, _, _ = load(out_dir("log"))
    shifted = np.mean([abs(a["observation.state"][0] - b["observation.state"][0]) > 1e-6 for a, b in zip(rows[5:], rows_log[5:], strict=True)])
    check(rep_log.episodes[0].clock == "log" and shifted > 0.9 and any("capture time" in n for n in rep_log.notes),
          f"--clock log: different (later) state in {shifted:.0%} of frames, and the report says why")


def test_profiler_sees_it() -> None:
    from phys_ai_dataprofiler.connectors.huggingface import inspect_local_file
    out = out_dir("basic")
    info = json.loads((out / "meta" / "info.json").read_text())
    from phys_ai_dataprofiler.remote.mp4 import declared_videos
    vid = next((out / "videos").rglob("*.mp4"))
    rel = vid.relative_to(out).as_posix()
    rep = inspect_local_file(rel, vid, None, video_info=declared_videos(info))
    c = rep.rich["channels"][0]
    check(rep.valid and c["msg_count"] == info["total_frames"] and abs(c["rate_hz"] - 30) < 0.01
          and not rep.rich["findings"], "the profiler reads the output video: frame count, 30 fps, matches info.json")
    rep = inspect_local_file("data/chunk-000/file-000.parquet", out / "data" / "chunk-000" / "file-000.parquet", None)
    check(rep.valid and abs(rep.summary["frame_rate_hz"] - 30) < 0.01 and rep.summary["episodes"] == 1,
          "the profiler reads the output table: 30 Hz, 1 episode")


def test_episodes() -> None:
    a = recording(TMP / "a.mcap", seconds=1.0, rng_seed=1)
    b = recording(TMP / "b.mcap", seconds=1.5, rng_seed=2)
    rep = convert([a, b], out_dir("two"), ConvertOptions())
    info, rows, frames, _ = load(out_dir("two"))
    eps = pq.read_table(out_dir("two") / "meta" / "episodes" / "chunk-000" / "file-000.parquet").to_pylist()
    key = rep.camera_key
    n0 = eps[0]["length"]
    check(info["total_episodes"] == 2 and len(frames) == len(rows) == n0 + eps[1]["length"],
          "two files, two episodes, one video holding both")
    check(eps[1]["dataset_from_index"] == n0 and abs(eps[1][f"videos/{key}/from_timestamp"] - n0 / 30) < 1e-9
          and rows[n0]["frame_index"] == 0 and rows[n0]["episode_index"] == 1,
          "episode 1 starts at row n0 and at video time n0/fps; frame_index restarts")
    check("stats/observation.state/mean" in eps[0] and len(eps[0]["stats/observation.state/mean"]) == 3,
          "per-episode stats in meta/episodes")


def test_edges() -> None:
    late = recording(TMP / "late.mcap", command_from_s=0.3)
    rep = convert([late], out_dir("late"), ConvertOptions())
    e = rep.episodes[0]
    check(e.dropped_start >= 8 and e.dropped_end == 0 and e.frames == 60 - e.dropped_start,
          f"commands start 0.3 s in: the frames before them are dropped from the start ({e.dropped_start})")

    gap = recording(TMP / "gap.mcap", joint_gap=(0.8, 1.0))
    try:
        convert([gap], out_dir("gap"), ConvertOptions())
        check(False, "a joint-state gap mid-episode must fail")
    except ConversionError as exc:
        check("gap in the recording" in str(exc) and not out_dir("gap").exists()
              and not out_dir("gap").with_name("gap.partial").exists(),
              "200 ms joint gap mid-episode: refused with the reason, nothing left on disk")
    rep = convert([gap], out_dir("gap_ok"), ConvertOptions(tolerance_ms=120))
    check(rep.episodes[0].state_age_ms_max > 90, "--tolerance-ms accepts the gap, and the report shows the stale match")

    drops = recording(TMP / "drops.mcap", drop_frames=(20, 21, 22))
    rep = convert([drops], out_dir("drops"), ConvertOptions())
    check(rep.episodes[0].camera_gaps == 1 and rep.episodes[0].camera_max_gap_ms > 120
          and any("camera interval" in n for n in rep.notes), "3 dropped camera frames are reported")

    raw = recording(TMP / "raw.mcap", raw=True)
    rep = convert([raw], out_dir("raw"), ConvertOptions())
    _, _, frames, _ = load(out_dir("raw"))
    check(rep.resolution == [64, 48] and len(frames) == rep.total_frames, "raw sensor_msgs/Image (rgb8) converts")

    two = recording(TMP / "twocam.mcap", extra_camera=True)
    rep = convert([two], out_dir("twocam"), ConvertOptions())
    check(rep.camera_topic == "/cam/image_raw/compressed" and rep.camera_key == "observation.images.cam",
          f"with two cameras, the higher-resolution one is the default ({rep.camera_key})")
    rep = convert([two], out_dir("small"), ConvertOptions(camera="/small/image_raw/compressed"))
    check(rep.resolution == [32, 24], "--camera picks another")

    stamped = recording(TMP / "stamped.mcap", stamped_commands=True)
    check(convert([stamped], out_dir("stamped"), ConvertOptions()).episodes[0].clock == "header",
          "every topic stamped: capture time throughout")
    check(convert([src := TMP / "rec.mcap"], out_dir("noact"), ConvertOptions(no_action=True)).action_names == []
          and "action" not in json.loads((out_dir("noact") / "meta" / "info.json").read_text())["features"],
          "--no-action: no action feature")
    for opts, words in ((ConvertOptions(camera="/nope"), "not in"), (ConvertOptions(clock="header"), "no header stamp"),
                        (ConvertOptions(state_joints=["j1", "missing"]), "no joint")):
        try:
            convert([src], out_dir("err"), opts)
            check(False, f"{opts} must fail")
        except ConversionError as exc:
            check(words in str(exc), f"clear error: {exc}"[:150])
    try:
        convert([src], out_dir("basic"), ConvertOptions())
        check(False, "existing output must not be overwritten")
    except ConversionError as exc:
        check("--overwrite" in str(exc), "existing dataset: refused without --overwrite")
    keep = TMP / "precious"
    keep.mkdir()
    (keep / "notes.txt").write_text("x")
    try:
        convert([src], keep, ConvertOptions(overwrite=True))
        check(False, "a non-dataset folder must never be deleted")
    except ConversionError:
        check((keep / "notes.txt").exists(), "--overwrite refuses to delete a folder that is not a LeRobot dataset")


def test_cli() -> None:
    from phys_ai_dataconverter.cli import main
    src = TMP / "rec.mcap"
    req = TMP / "req.yaml"
    req.write_text("streams:\n  - name: camera\n    match: {modality: video}\n    min_rate_hz: 25\n"
                   "  - name: state\n    match: {topic: observation.state}\n    min_rate_hz: 25\n")
    folder = TMP / "cli_in"
    folder.mkdir()
    recording(folder / "a.mcap", seconds=1.0, rng_seed=5)
    recording(folder / "sub_b.mcap", seconds=1.0, rng_seed=6)
    (folder / "notes.txt").write_text("not a recording")
    code = main([str(folder), "-o", str(out_dir("cli")), "--check", str(req), "--quiet"])
    info = json.loads((out_dir("cli") / "meta" / "info.json").read_text())
    check(code == 0 and info["total_episodes"] == 2 and (out_dir("cli") / "meta" / "check.json").is_file(),
          "folder input (2 recordings, other files ignored), then --check with the profiler: pass, report in meta/")
    code = main([str(src), "-o", str(out_dir("cli2")), "--check", "preset:teleop-imitation", "--quiet"])
    check(code == 1, "--check fails (exit 1) when the dataset misses the requirements (2 s, not 1 h)")
    check(main([str(src), "-o", str(out_dir("cli")), "--quiet"]) == 2, "refusal (existing output): exit 2")
    bad = TMP / "notes.bag"
    bad.write_bytes(b"x")
    check(main([str(bad), "-o", str(out_dir("cli3")), "--quiet"]) == 2, "unsupported input format: exit 2")


def test_lerobot() -> None:
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError:
        print("note: lerobot not installed; LeRobotDataset load check skipped")
        return
    ds = LeRobotDataset("local/two", root=out_dir("two"), video_backend="pyav")
    item = ds[len(ds) - 1]
    check(ds.num_episodes == 2 and tuple(item[ds.meta.camera_keys[0]].shape) == (3, 48, 64),
          "LeRobotDataset loads it and decodes the last frame of the last episode")


def test_hub() -> None:
    from huggingface_hub import hf_hub_download
    p = Path(hf_hub_download("ola31/readme_test_rosbag_1", "0/0_0.mcap", repo_type="dataset",
                             local_dir=str(TMP / "hub")))
    rep = convert([p], out_dir("hub_out"), ConvertOptions(task="test"))
    e = rep.episodes[0]
    check(rep.camera_topic == "/zed/zed_node/left/image_rect_color/compressed" and rep.resolution == [672, 376]
          and len(rep.state_names) == 19 and e.clock == "mixed" and e.state_age_ms_max < 6,
          f"Hub teleop MCAP: {e.frames} frames, 19 joints, state within {e.state_age_ms_max:.1f} ms of capture")


def main(hub: bool = False) -> None:
    test_align()
    test_basic()
    test_profiler_sees_it()
    test_episodes()
    test_edges()
    test_cli()
    test_lerobot()
    if hub:
        test_hub()
    print("all checks passed")


if __name__ == "__main__":
    main(hub="--hub" in sys.argv)
