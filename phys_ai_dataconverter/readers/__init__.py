"""
Input formats. A reader is a module with:

    EXTENSIONS: tuple[str, ...]                        file extensions it handles
    list_topics(path) -> dict[str, TopicInfo]          topics with their stream kind (no payloads read)
    read(path, topics) -> dict[str, Stream]            every message of those topics, decoded
    first_images(path, topics) -> dict[str, image]     the first image of each topic (to pick a camera)

and is registered in READERS below.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from phys_ai_dataconverter.model import ConversionError
from phys_ai_dataconverter.readers import mcap_ros2

READERS: dict[str, ModuleType] = {"mcap-ros2": mcap_ros2}


def reader_for(path: Path) -> ModuleType:
    ext = path.suffix.lower()
    for mod in READERS.values():
        if ext in mod.EXTENSIONS:
            return mod
    supported = ", ".join(e for m in READERS.values() for e in m.EXTENSIONS)
    raise ConversionError(f"{path.name}: no reader for {ext or 'files without an extension'} (supported: {supported})")


def input_extensions() -> tuple[str, ...]:
    return tuple(e for m in READERS.values() for e in m.EXTENSIONS)
