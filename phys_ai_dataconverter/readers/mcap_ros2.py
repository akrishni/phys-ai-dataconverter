"""
ROS 2 recordings in MCAP storage (`ros2 bag record -s mcap`, the default since ROS 2 Iron).

Messages are CDR-encoded with their `.msg` definitions embedded in the file, decoded by
mcap-ros2-support. Each message keeps two times: the MCAP record's `log_time` (when the recorder wrote
it) and the ROS `header.stamp` (when the sensor captured it), when the message type has a header.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from phys_ai_dataconverter.model import (
    IMAGE, JOINT_COMMAND, JOINT_STATE, CompressedImage, ConversionError, RawImage, Stream, TopicInfo,
)

EXTENSIONS = (".mcap",)
KINDS = {
    "sensor_msgs/msg/CompressedImage": IMAGE,
    "sensor_msgs/msg/Image": IMAGE,
    "sensor_msgs/msg/JointState": JOINT_STATE,
    "trajectory_msgs/msg/JointTrajectory": JOINT_COMMAND,
}


def _stamp_ns(msg) -> int:
    h = getattr(msg, "header", None)
    if h is None:
        return 0
    return int(h.stamp.sec) * 1_000_000_000 + int(h.stamp.nanosec)


def _image(type_name: str, msg):
    if type_name.endswith("CompressedImage"):
        return CompressedImage(msg.format, bytes(msg.data))
    return RawImage(msg.encoding, int(msg.width), int(msg.height), int(msg.step), bytes(msg.data))


def list_topics(path: Path) -> dict[str, TopicInfo]:
    with path.open("rb") as f:
        summary = make_reader(f).get_summary()
    if summary is None:
        raise ConversionError(f"{path}: no MCAP summary (recording not closed?); repair it with `mcap recover`")
    counts = summary.statistics.channel_message_counts if summary.statistics else {}
    out = {}
    for cid, ch in summary.channels.items():
        sc = summary.schemas.get(ch.schema_id)
        name = sc.name if sc else ""
        out[ch.topic] = TopicInfo(ch.topic, KINDS.get(name), name, counts.get(cid, 0))
    return out


def first_images(path: Path, topics: list[str]) -> dict[str, CompressedImage | RawImage]:
    found: dict[str, CompressedImage | RawImage] = {}
    with path.open("rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for schema, channel, _, msg in reader.iter_decoded_messages(topics=topics):
            if channel.topic not in found:
                found[channel.topic] = _image(schema.name, msg)
                if len(found) == len(topics):
                    break
    return found


def read(path: Path, topics: set[str]) -> dict[str, Stream]:
    streams: dict[str, Stream] = {}
    with path.open("rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for schema, channel, record, msg in reader.iter_decoded_messages(topics=sorted(topics), log_time_order=True):
            kind = KINDS.get(schema.name)
            if kind is None:
                raise ConversionError(f"{path.name}: {channel.topic} is {schema.name}, which the converter cannot use")
            s = streams.setdefault(channel.topic, Stream(channel.topic, kind, schema.name))
            s.log_ns.append(record.log_time)
            s.stamp_ns.append(_stamp_ns(msg))
            if kind == JOINT_STATE:
                s.values.append((tuple(msg.name), np.asarray(msg.position, dtype=np.float64)))
            elif kind == JOINT_COMMAND:
                pos = np.asarray(msg.points[0].positions, dtype=np.float64) if msg.points else None
                s.values.append((tuple(msg.joint_names), pos))
            else:
                s.values.append(_image(schema.name, msg))
    for t in topics:
        if t not in streams:
            raise ConversionError(f"{path.name}: topic {t} has no messages")
    return streams
