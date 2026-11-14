"""Camera messages to RGB arrays (any reader's CompressedImage / RawImage)."""

from __future__ import annotations

import av
import numpy as np

from phys_ai_dataconverter.model import CompressedImage, ConversionError, RawImage

RAW_ENCODINGS = {"rgb8": ("rgb24", 3), "bgr8": ("bgr24", 3), "rgba8": ("rgba", 4), "bgra8": ("bgra", 4),
                 "mono8": ("gray", 1)}


class Decoder:
    """Decodes images to RGB uint8 HxWx3 arrays: JPEG and PNG compressed, or raw rgb8/bgr8/rgba8/bgra8/mono8."""

    def __init__(self) -> None:
        self._ctx: dict[str, object] = {}

    def __call__(self, img: CompressedImage | RawImage) -> np.ndarray:
        if isinstance(img, RawImage):
            return decode_raw(img)
        codec = "png" if "png" in img.format.lower() else "mjpeg"
        if codec not in self._ctx:
            self._ctx[codec] = av.CodecContext.create(codec, "r")
        frames = self._ctx[codec].decode(av.Packet(img.data))
        if not frames:
            raise ConversionError(f"could not decode a {img.format!r} image")
        return frames[0].to_ndarray(format="rgb24")


def decode_raw(img: RawImage) -> np.ndarray:
    if img.encoding not in RAW_ENCODINGS:
        raise ConversionError(f"raw image encoding {img.encoding!r} is not supported "
                              f"(supported: {', '.join(RAW_ENCODINGS)})")
    pix, ch = RAW_ENCODINGS[img.encoding]
    arr = np.frombuffer(img.data, dtype=np.uint8).reshape(img.height, img.step)[:, : img.width * ch]
    arr = arr.reshape(img.height, img.width, ch)
    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(arr), format=pix if ch > 1 else "gray")
    return frame.to_ndarray(format="rgb24")
