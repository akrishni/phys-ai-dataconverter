"""
The data model shared by readers, alignment and writers.

A reader turns one recording (one episode) into `Stream`s of a few known kinds, whatever the source
format. Alignment and writers only ever see streams, so supporting a new input format means writing a
reader, and a new output format means writing a writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Stream kinds a reader can produce.
IMAGE = "image"                  # values: CompressedImage | RawImage
JOINT_STATE = "joint_state"      # values: (joint names tuple, positions float64 array)
JOINT_COMMAND = "joint_command"  # values: (joint names tuple, positions float64 array, or None if no point)


class ConversionError(RuntimeError):
    """The recording cannot be converted as asked; the message says why and what to change."""


@dataclass(frozen=True)
class CompressedImage:
    format: str          # e.g. "rgb8; jpeg compressed bgr8", "png"
    data: bytes


@dataclass(frozen=True)
class RawImage:
    encoding: str        # ROS image encoding, e.g. "rgb8", "bgr8", "mono8"
    width: int
    height: int
    step: int            # bytes per row
    data: bytes


@dataclass
class TopicInfo:
    name: str
    kind: str | None     # IMAGE, JOINT_STATE, JOINT_COMMAND, or None (not used by the converter)
    type: str            # the source's own type name, e.g. "sensor_msgs/msg/JointState"
    count: int


@dataclass
class Stream:
    """Every message of one topic in one recording, in the order the recorder wrote them.

    `log_ns` is when the recorder wrote each message; `stamp_ns` is the sensor's capture time from the
    message header, 0 when the message has none.
    """
    topic: str
    kind: str
    type: str
    log_ns: list[int] = field(default_factory=list)
    stamp_ns: list[int] = field(default_factory=list)
    values: list = field(default_factory=list)

    def stamped(self) -> bool:
        return bool(self.stamp_ns) and all(t > 0 for t in self.stamp_ns)

    def write_delay_ns(self) -> int:
        """Median log_ns - stamp_ns (how long messages took to reach the recorder); stamped streams only."""
        return int(np.median(np.asarray(self.log_ns, dtype=np.int64) - np.asarray(self.stamp_ns, dtype=np.int64)))
