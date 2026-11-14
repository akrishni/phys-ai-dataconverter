"""
LeRobot v3.0 output (`codebase_version: v3.0`, the format of lerobot 0.4 and later).

    data/chunk-000/file-000.parquet            one row per frame: observation.state, action, indexes
    videos/<camera>/chunk-000/file-000.mp4     every episode's frames back to back, constant fps
    meta/info.json                             features, fps, totals, path templates
    meta/stats.json                            per-feature min/max/mean/std/quantiles (images: per channel)
    meta/tasks.parquet                         the task strings (pandas index) and their task_index
    meta/episodes/chunk-000/file-000.parquet   per episode: rows, video time span, per-episode stats
    meta/conversion.json                       provenance: sources, topics, clock, match distances, notes

Video is AV1 (libsvtav1, crf 30, a keyframe every 2 frames), as LeRobot records it, unless asked otherwise.
"""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from phys_ai_dataconvertor.model import ConversionError

CODEBASE_VERSION = "v3.0"
QUANTILES = (0.01, 0.10, 0.50, 0.90, 0.99)
INDEX_FEATURES = (("timestamp", "float32"), ("frame_index", "int64"), ("episode_index", "int64"),
                  ("index", "int64"), ("task_index", "int64"))
CODEC_NAMES = {"libsvtav1": "av1", "libaom-av1": "av1", "libx264": "h264", "h264": "h264",
               "libx265": "hevc", "hevc": "hevc"}


def is_dataset(path: Path) -> bool:
    return (path / "meta" / "info.json").is_file()


def video_path(root: Path, camera_key: str) -> Path:
    return root / "videos" / camera_key / "chunk-000" / "file-000.mp4"


class VideoWriter:
    """All episodes of one camera, back to back, at a constant `fps`."""

    def __init__(self, path: Path, fps: int, width: int, height: int, vcodec: str, crf: int, gop: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("SVT_LOG", "1")     # SVT-AV1 prints a config banner to stderr; errors only
        try:
            self.container = av.open(str(path), mode="w")
            self.stream = self.container.add_stream(vcodec, rate=fps)
        except (av.error.FFmpegError, ValueError) as exc:
            raise ConversionError(f"cannot open the {vcodec} video encoder: {exc}") from exc
        self.stream.width, self.stream.height = width, height
        self.stream.pix_fmt = "yuv420p"
        self.stream.time_base = Fraction(1, fps)
        options = {"g": str(gop)}
        if vcodec == "libsvtav1":
            options.update({"crf": str(crf), "preset": "8"})
        elif vcodec in ("h264", "libx264", "hevc", "libx265"):
            options.update({"crf": str(crf), "preset": "medium"})
        self.stream.options = options
        self.vcodec = vcodec
        self.width, self.height = width, height
        self.frames = 0
        self.stats = ImageStats()

    def add(self, rgb: np.ndarray) -> None:
        if rgb.shape[:2] != (self.height, self.width):
            raise ConversionError(f"camera resolution changed from {self.width}x{self.height} to "
                                  f"{rgb.shape[1]}x{rgb.shape[0]} mid-recording")
        self.stats.add(rgb)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        frame.pts = self.frames
        self.frames += 1
        for packet in self.stream.encode(frame):
            self.container.mux(packet)

    def close(self) -> None:
        for packet in self.stream.encode():
            self.container.mux(packet)
        self.container.close()

    def abort(self) -> None:
        """Close after a failure; the file is deleted by the caller. The encoder is flushed first, since
        SVT-AV1 complains when it is torn down mid-stream."""
        try:
            for _ in self.stream.encode():
                pass
        except Exception:
            pass
        try:
            self.container.close()
        except Exception:
            pass


class ImageStats:
    """Per-channel min/max/mean/std over pixels scaled to [0, 1], as LeRobot stores for images.
    Frames are subsampled to 1/4 resolution for speed; min, max and mean barely move."""

    def __init__(self) -> None:
        self.n = 0
        self.sum = np.zeros(3)
        self.sumsq = np.zeros(3)
        self.lo = np.full(3, np.inf)
        self.hi = np.full(3, -np.inf)
        self.frames = 0

    def add(self, rgb: np.ndarray) -> None:
        px = rgb[::4, ::4].reshape(-1, 3).astype(np.float64) / 255.0
        self.n += len(px)
        self.sum += px.sum(0)
        self.sumsq += (px * px).sum(0)
        self.lo = np.minimum(self.lo, px.min(0))
        self.hi = np.maximum(self.hi, px.max(0))
        self.frames += 1

    def snapshot(self) -> dict:
        mean = self.sum / self.n
        std = np.sqrt(np.maximum(self.sumsq / self.n - mean ** 2, 0))

        def chw(v):                     # (C, 1, 1), LeRobot's image stat shape
            return [[[float(x)]] for x in v]

        return {"min": chw(self.lo), "max": chw(self.hi), "mean": chw(mean), "std": chw(std), "count": [self.frames]}


def vector_stats(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    out = {"min": x.min(0).tolist(), "max": x.max(0).tolist(), "mean": x.mean(0).tolist(),
           "std": x.std(0).tolist(), "count": [int(len(x))]}
    for q in QUANTILES:
        out[f"q{int(q * 100):02d}"] = np.quantile(x, q, axis=0).tolist()
    return out


def scalar_stats(x: np.ndarray, integer: bool) -> dict:
    s = vector_stats(x)
    if integer:
        for k in ("min", "max"):
            s[k] = [int(v) for v in s[k]]
    return s


def write(root: Path, *, fps: int, camera_key: str, resolution: list[int], vcodec: str, task: str,
          robot_type: str | None, state_names: list[str], action_names: list[str],
          episodes: list[dict], image_stats: dict, provenance: dict) -> None:
    """Write everything except the video (already written by VideoWriter).

    `episodes`: per episode {"state": (n, k) array, "action": (n, m) array or None, "video_from_s": float}.
    """
    w, h = resolution
    n_total = sum(len(e["state"]) for e in episodes)
    has_action = bool(action_names)

    # ---- data table ------------------------------------------------------------------
    cols: dict[str, list] = {k: [] for k in ("observation.state", "action", "timestamp", "frame_index",
                                              "episode_index", "index", "task_index")}
    spans = []
    index = 0
    for ei, e in enumerate(episodes):
        n = len(e["state"])
        cols["observation.state"] += e["state"].astype(np.float32).tolist()
        if has_action:
            cols["action"] += e["action"].astype(np.float32).tolist()
        cols["timestamp"] += (np.arange(n) / fps).astype(np.float32).tolist()
        cols["frame_index"] += list(range(n))
        cols["episode_index"] += [ei] * n
        cols["index"] += list(range(index, index + n))
        cols["task_index"] += [0] * n
        spans.append((index, index + n))
        index += n
    table = {"observation.state": pa.array(cols["observation.state"], type=pa.list_(pa.float32()))}
    if has_action:
        table["action"] = pa.array(cols["action"], type=pa.list_(pa.float32()))
    for k, dt in INDEX_FEATURES:
        table[k] = pa.array(cols[k], type=pa.float32() if dt == "float32" else pa.int64())
    data_path = root / "data" / "chunk-000" / "file-000.parquet"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(table), data_path)

    # ---- info.json -----------------------------------------------------------------------
    features = {
        camera_key: {"dtype": "video", "shape": [h, w, 3], "names": ["height", "width", "channels"],
                     "info": {"video.height": h, "video.width": w, "video.codec": CODEC_NAMES.get(vcodec, vcodec),
                              "video.pix_fmt": "yuv420p", "video.is_depth_map": False, "video.fps": fps,
                              "video.channels": 3, "has_audio": False}},
        "observation.state": {"dtype": "float32", "shape": [len(state_names)], "names": state_names},
    }
    if has_action:
        features["action"] = {"dtype": "float32", "shape": [len(action_names)], "names": action_names}
    for k, dt in INDEX_FEATURES:
        features[k] = {"dtype": dt, "shape": [1], "names": None}
    info = {
        "codebase_version": CODEBASE_VERSION, "robot_type": robot_type, "total_episodes": len(episodes),
        "total_frames": n_total, "total_tasks": 1, "chunks_size": 1000, "fps": fps,
        "splits": {"train": f"0:{len(episodes)}"},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": features, "data_files_size_in_mb": 100, "video_files_size_in_mb": 500,
    }
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "info.json").write_text(json.dumps(info, indent=4))

    # ---- stats.json ----------------------------------------------------------------------
    stats = {camera_key: image_stats,
             "observation.state": vector_stats(np.asarray(cols["observation.state"], dtype=np.float64))}
    if has_action:
        stats["action"] = vector_stats(np.asarray(cols["action"], dtype=np.float64))
    for k, dt in INDEX_FEATURES:
        stats[k] = scalar_stats(np.asarray(cols[k]), integer=dt == "int64")
    (meta / "stats.json").write_text(json.dumps(stats, indent=4))

    # ---- tasks.parquet: the task string is the pandas index, as LeRobot writes it -------
    tasks = pa.table({"task_index": pa.array([0], type=pa.int64()), "__index_level_0__": pa.array([task])})
    tasks = tasks.replace_schema_metadata({b"pandas": json.dumps({
        "index_columns": ["__index_level_0__"],
        "column_indexes": [{"name": None, "field_name": None, "pandas_type": "unicode", "numpy_type": "object",
                            "metadata": {"encoding": "UTF-8"}}],
        "columns": [{"name": "task_index", "field_name": "task_index", "pandas_type": "int64",
                     "numpy_type": "int64", "metadata": None},
                    {"name": None, "field_name": "__index_level_0__", "pandas_type": "unicode",
                     "numpy_type": "object", "metadata": None}],
        "creator": {"library": "pyarrow", "version": pa.__version__}, "pandas_version": "2.2.0"}).encode()})
    pq.write_table(tasks, meta / "tasks.parquet")

    # ---- episodes table ------------------------------------------------------------------
    vk = f"videos/{camera_key}"
    ep_cols: dict[str, list] = {k: [] for k in (
        "episode_index", "data/chunk_index", "data/file_index", "dataset_from_index", "dataset_to_index",
        f"{vk}/chunk_index", f"{vk}/file_index", f"{vk}/from_timestamp", f"{vk}/to_timestamp",
        "tasks", "length", "meta/episodes/chunk_index", "meta/episodes/file_index")}
    stat_cols: dict[str, list] = {}
    for ei, (e, (a, b)) in enumerate(zip(episodes, spans, strict=True)):
        n = b - a
        for k, v in (("episode_index", ei), ("data/chunk_index", 0), ("data/file_index", 0),
                     ("dataset_from_index", a), ("dataset_to_index", b),
                     (f"{vk}/chunk_index", 0), (f"{vk}/file_index", 0),
                     (f"{vk}/from_timestamp", e["video_from_s"]), (f"{vk}/to_timestamp", e["video_from_s"] + n / fps),
                     ("tasks", [task]), ("length", n), ("meta/episodes/chunk_index", 0), ("meta/episodes/file_index", 0)):
            ep_cols[k].append(v)
        per = {"observation.state": vector_stats(e["state"])}
        if has_action:
            per["action"] = vector_stats(e["action"])
        per["timestamp"] = scalar_stats(np.arange(n) / fps, integer=False)
        per["frame_index"] = scalar_stats(np.arange(n), integer=True)
        per["episode_index"] = scalar_stats(np.full(n, ei), integer=True)
        per["index"] = scalar_stats(np.arange(a, b), integer=True)
        per["task_index"] = scalar_stats(np.zeros(n), integer=True)
        for feat, st in per.items():
            for s, v in st.items():
                stat_cols.setdefault(f"stats/{feat}/{s}", []).append(v)
    ep_cols.update(stat_cols)
    ep_path = meta / "episodes" / "chunk-000" / "file-000.parquet"
    ep_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(ep_cols), ep_path)

    (meta / "conversion.json").write_text(json.dumps(provenance, indent=2, default=str))
