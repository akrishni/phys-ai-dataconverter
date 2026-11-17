"""
One recording to one aligned episode, independent of the input and output formats.

The camera's frames are the timeline. Each frame takes the joint-state reading and the command nearest
to it in time, within a tolerance (half a frame interval by default). Frames at the start or end that
have no match yet are dropped; a frame in the middle with no match is a gap in the recording and stops
the conversion, so no frame ever carries a stale value without the user asking for it.

Clocks: messages carry the sensor's capture time (`stamp_ns`) and the recorder's write time (`log_ns`).
A camera frame typically reaches the recorder 20-40 ms after capture, a joint reading 1-2 ms after, so
pairing on write time would match each frame with the arm's position of a frame later. So:

- "header": every stream has a capture time; pair on it.
- "mixed" (the usual teleop case): camera and state have capture times, commands do not (JointTrajectory
  from a teleop leader leaves the stamp at 0). Commands are placed on the capture timeline at their
  write time minus the state topic's median write delay.
- "log": pair on write time (`--clock log`, or when the camera or state has no capture time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from phys_ai_dataconverter.align import nearest
from phys_ai_dataconverter.model import IMAGE, JOINT_COMMAND, JOINT_STATE, ConversionError, Stream
from phys_ai_dataconverter.readers import reader_for


@dataclass
class Selection:
    """Which topics become which columns; taken from the first recording and applied to all."""
    cameras: list[str]          # one or more image topics
    state_topic: str
    action_topics: list[str]


@dataclass
class EpisodeReport:
    source: str
    frames: int
    dropped_start: int
    dropped_end: int
    duration_s: float
    clock: str                                # "header", "mixed" or "log"
    clock_offset_ms: float | None             # camera: median log - capture time, when it has capture times
    state_age_ms_max: float
    action_age_ms_max: float | None
    camera_rate_hz: float
    camera_gaps: int = 0                      # frame intervals over 1.5x the nominal one (dropped frames)
    camera_max_gap_ms: float = 0.0
    write_delay_ms: dict = field(default_factory=dict)   # per stamped topic: median log - capture time


@dataclass
class Episode:
    report: EpisodeReport
    frames: dict[str, list]           # camera topic -> images in timeline order (CompressedImage | RawImage)
    state: np.ndarray                 # (n, n_state) float64
    action: np.ndarray | None         # (n, n_action) float64
    state_names: list[str]
    action_names: list[str]


# --------------------------------------------------------------------------- #
# Topic selection
# --------------------------------------------------------------------------- #

def select(path: Path, cameras: list[str] | None, state_topic: str | None, action_topics: list[str] | None,
           no_action: bool, pick_cameras) -> Selection:
    """Resolve the topics, defaulting from what the recording contains. `pick_cameras(path, topics)` chooses
    among several image topics (the converter passes one that prefers the highest resolution)."""
    topics = reader_for(path).list_topics(path)

    def of(kind):
        return {t: i for t, i in topics.items() if i.kind == kind}

    images, states, commands = of(IMAGE), of(JOINT_STATE), of(JOINT_COMMAND)

    def require(name, want, pool, what):
        if want not in topics:
            raise ConversionError(f"{what} topic {want} not in {path.name}; {name} topics: {sorted(pool) or 'none'}")
        return want

    if cameras:
        cams = [require("image", c, images, "camera") for c in cameras]
    elif not images:
        raise ConversionError(f"{path.name} has no image topic")
    elif len(images) == 1:
        cams = [next(iter(images))]
    else:
        cams = pick_cameras(path, sorted(images))
    if state_topic:
        st = require("joint state", state_topic, states, "state")
    elif not states:
        raise ConversionError(f"{path.name} has no joint state topic for observation.state")
    else:
        st = max(states, key=lambda t: states[t].count)
    if no_action:
        acts = []
    elif action_topics is not None:
        acts = [require("joint command", t, commands, "action") for t in action_topics]
    elif not commands:
        raise ConversionError(f"{path.name} has no joint command topic for action; pass --action-topic, "
                              "or --no-action for a dataset without actions")
    else:
        acts = sorted(commands)
    return Selection(cams, st, acts)


# --------------------------------------------------------------------------- #
# Clocks
# --------------------------------------------------------------------------- #

def choose_clock(cam: Stream, state: Stream, acts: list[Stream], wanted: str) -> str:
    if wanted == "log":
        return "log"
    unstamped = [s.topic for s in (cam, state, *acts) if not s.stamped()]
    if wanted == "header":
        if unstamped:
            raise ConversionError(f"--clock header, but {', '.join(unstamped)} have no header stamp; "
                                  "use --clock auto or --clock log")
        return "header"
    if not cam.stamped() or not state.stamped():
        return "log"
    return "mixed" if unstamped else "header"


def timeline(s: Stream, clock: str, delay_ns: int) -> np.ndarray:
    if clock == "log":
        return np.asarray(s.log_ns, dtype=np.int64)
    if s.stamped():
        return np.asarray(s.stamp_ns, dtype=np.int64)
    if clock == "mixed":
        return np.asarray(s.log_ns, dtype=np.int64) - delay_ns
    raise ConversionError(f"{s.topic} has no header stamp")


def sorted_times(s: Stream, clock: str, delay_ns: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(sorted times, order). Capture times can arrive out of order by a few ms."""
    t = timeline(s, clock, delay_ns)
    order = np.argsort(t, kind="stable")
    t = t[order]
    if s.kind == IMAGE and len(t) > 1 and np.any(np.diff(t) == 0):
        dup = int((np.diff(t) == 0).sum())
        raise ConversionError(f"{s.topic}: {dup} frame(s) share a timestamp on the {clock} clock; "
                              f"try --clock {'log' if clock == 'header' else 'header'}")
    return t, order


# --------------------------------------------------------------------------- #
# Joints
# --------------------------------------------------------------------------- #

def joint_matrix(s: Stream, order: np.ndarray, names: list[str]) -> np.ndarray:
    """Positions of `names` from each joint-state message, in `order`. Fails if a message lacks one."""
    out = np.empty((len(order), len(names)), dtype=np.float64)
    cache: dict[tuple, np.ndarray] = {}
    for row, i in enumerate(order):
        msg_names, pos = s.values[i]
        idx = cache.get(msg_names)
        if idx is None:
            where = {n: k for k, n in enumerate(msg_names)}
            missing = [n for n in names if n not in where]
            if missing:
                raise ConversionError(f"{s.topic}: message {i} has no joint(s) {missing}")
            if len(pos) < len(msg_names):
                raise ConversionError(f"{s.topic}: message {i} has {len(msg_names)} names but {len(pos)} positions")
            idx = np.array([where[n] for n in names])
            cache[msg_names] = idx
        out[row] = pos[idx]
    return out


def command_matrix(s: Stream, order: np.ndarray) -> tuple[list[str], np.ndarray, np.ndarray]:
    """(joint names, positions per message, valid mask). A message without a point is not a command."""
    names = None
    rows, valid = [], []
    for i in order:
        msg_names, pos = s.values[i]
        if names is None and pos is not None:
            names = list(msg_names)
        ok = pos is not None and names is not None and list(msg_names) == names and len(pos) == len(names)
        valid.append(ok)
        rows.append(pos if ok else None)
    if names is None:
        raise ConversionError(f"{s.topic}: no message has a command point")
    mat = np.array([r if r is not None else np.full(len(names), np.nan) for r in rows], dtype=np.float64)
    return names, mat, np.array(valid)


# --------------------------------------------------------------------------- #
# Episode
# --------------------------------------------------------------------------- #

def build(path: Path, sel: Selection, clock_wanted: str, state_joints: list[str] | None,
          fps: int | None, tolerance_ns: int | None, tolerance_ms: float | None
          ) -> tuple[Episode, int, int]:
    """Align one recording. Returns (episode, fps, tolerance_ns): the first episode fixes both.

    For multi-camera: all cameras must align to the primary camera's timeline. The primary camera
    (first in sel.cameras) defines the frame rate and timeline."""
    streams = reader_for(path).read(path, {*sel.cameras, sel.state_topic, *sel.action_topics})
    cams = [streams[t] for t in sel.cameras]
    primary_cam = cams[0]
    st = streams[sel.state_topic]
    acts = [streams[t] for t in sel.action_topics]
    clock = choose_clock(primary_cam, st, acts, clock_wanted)
    delay = st.write_delay_ns() if clock == "mixed" else 0

    cam_t, cam_order = sorted_times(primary_cam, clock)
    if len(cam_t) < 2:
        raise ConversionError(f"{path.name}: {sel.cameras[0]} has {len(cam_t)} frame(s); need at least 2")
    rate = 1e9 / float(np.median(np.diff(cam_t)))
    if fps is None:
        fps = max(1, round(rate))
    if tolerance_ns is None:
        tolerance_ns = int(tolerance_ms * 1e6) if tolerance_ms is not None else int(0.5e9 / fps)

    # Align all secondary cameras to primary camera timeline
    cam_alignments = {}
    for i, cam in enumerate(cams):
        if i == 0:
            cam_alignments[sel.cameras[i]] = (cam_t, cam_order, None)
        else:
            sec_t, sec_order = sorted_times(cam, clock)
            al_cam = nearest(cam_t, sec_t, tolerance_ns)
            cam_alignments[sel.cameras[i]] = (sec_t, sec_order, al_cam)

    action_names: list[str] = []
    act_cols = []
    for s in acts:
        t, order = sorted_times(s, clock, delay)
        names, mat, valid = command_matrix(s, order)
        action_names += names
        act_cols.append((t[valid], mat[valid]))
    # observation.state defaults to the joints the commands drive, in the same order
    state_names = list(state_joints) if state_joints else (action_names or list(st.values[0][0]))
    st_t, st_order = sorted_times(st, clock)
    state_mat = joint_matrix(st, st_order, state_names)

    al_state = nearest(cam_t, st_t, tolerance_ns)
    al_acts = [nearest(cam_t, t, tolerance_ns) for t, _ in act_cols]

    # For secondary cameras, check alignment
    ok = al_state.matched.copy()
    for a in al_acts:
        ok &= a.matched
    for cam_topic, (_, _, al_cam) in cam_alignments.items():
        if al_cam is not None:  # secondary camera
            ok &= al_cam.matched

    if not ok.any():
        raise ConversionError(f"{path.name}: no primary camera frame has state, commands, and secondary cameras within "
                              f"{tolerance_ns / 1e6:.1f} ms on the {clock} clock; the streams may use different "
                              f"clocks (try --clock log) or need a larger --tolerance-ms")
    first, last = int(np.argmax(ok)), len(ok) - int(np.argmax(ok[::-1]))
    inner_bad = np.flatnonzero(~ok[first:last])
    if len(inner_bad):
        k = first + int(inner_bad[0])
        age_vals = [al_state.age_ns[k]] + [a.age_ns[k] for a in al_acts]
        for _, (_, _, al_cam) in cam_alignments.items():
            if al_cam is not None:
                age_vals.append(al_cam.age_ns[k])
        worst = max(age_vals) / 1e6
        raise ConversionError(
            f"{path.name}: {len(inner_bad)} primary camera frame(s) in the middle of the recording have no state, "
            f"command, or secondary camera within {tolerance_ns / 1e6:.1f} ms (first at {(cam_t[k] - cam_t[0]) / 1e9:.3f} s, "
            f"nearest sample {worst:.1f} ms away): a gap in the recording. Raise --tolerance-ms to accept it.")
    keep = np.arange(first, last)

    # Build frames dict with all cameras
    frames_dict = {}
    for i, (cam_topic, (_, cam_ord, al_cam)) in enumerate(cam_alignments.items()):
        cam_stream = cams[i]
        if al_cam is None:  # primary camera
            frames_dict[cam_topic] = [cam_stream.values[i] for i in cam_ord[keep]]
        else:  # secondary camera
            frames_dict[cam_topic] = [cam_stream.values[i] for i in cam_ord[al_cam.index[keep]]]

    state = state_mat[al_state.index[keep]]
    action = (np.concatenate([mat[a.index[keep]] for (_, mat), a in zip(act_cols, al_acts, strict=True)], axis=1)
              if act_cols else None)

    iv = np.diff(cam_t[keep]) / 1e6
    rep = EpisodeReport(
        source=str(path), frames=len(keep), dropped_start=first, dropped_end=len(ok) - last,
        duration_s=len(keep) / fps, clock=clock,
        clock_offset_ms=primary_cam.write_delay_ns() / 1e6 if primary_cam.stamped() else None,
        state_age_ms_max=float(al_state.age_ns[keep].max() / 1e6),
        action_age_ms_max=float(max(a.age_ns[keep].max() for a in al_acts) / 1e6) if al_acts else None,
        camera_rate_hz=rate, camera_gaps=int((iv > 1.5e3 / fps).sum()) if len(iv) else 0,
        camera_max_gap_ms=float(iv.max()) if len(iv) else 0.0,
        write_delay_ms={s.topic: round(s.write_delay_ns() / 1e6, 3) for s in (primary_cam, st, *acts) if s.stamped()})
    return Episode(rep, frames_dict, state, action, state_names, action_names), fps, tolerance_ns
