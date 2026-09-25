"""The packaged parsers Stele knows how to build and run."""
from __future__ import annotations

from . import ParserImage

MINERU = ParserImage(
    name="mineru",
    version="4.0.7",
    image="localhost/stele/mineru:4.0.7",
    containerfile="parsers/mineru/Containerfile",
    config={"tier": "basic", "ocr_mode": "auto", "image_analysis": True, "pages": ""},
    formats=(".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"),
    # A GPU is used when the host can pass one through and the GPU image
    # (parsers/mineru/Containerfile.gpu) has been built; otherwise the CPU
    # image runs. The GPU image is untested on real hardware (no GPU in CI).
    gpu="optional",
    gpu_image="localhost/stele/mineru-gpu:4.0.7",
    memory="6g",
    cpus=2.0,
    tmpfs_size="2g",
    timeout_seconds=1800,
)

PARSERS: dict[str, ParserImage] = {p.name: p for p in (MINERU,)}


def get_parser(name: str) -> ParserImage:
    try:
        return PARSERS[name]
    except KeyError:
        raise KeyError(
            f"unknown parser {name!r}; known parsers: {', '.join(sorted(PARSERS))}"
        ) from None
