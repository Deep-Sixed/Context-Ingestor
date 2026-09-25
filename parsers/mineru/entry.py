"""Stele entry point for MinerU (roadmap #9). Runs inside the parser image.

Output (under STELE_OUTPUT_DIR):
  document.md               Markdown rendering
  middle.json               MinerU middle JSON: pages, blocks, spans, bboxes
  structured_content.json   MinerU structured content
  model_output.json         raw model output (when MinerU produces it)
  images/...                extracted figures and tables, referenced from the above
  stele-parser.json         manifest (see parsers/README.md)
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/stele")
import stele_entry as se  # noqa: E402

DEFAULTS = {
    # basic: layout, OCR, formula and table models (ONNX, CPU friendly).
    # standard adds the MinerU2.5 VLM and needs an image built with it.
    "tier": "basic",
    # auto: OCR only pages without a usable text layer; txt / ocr force one.
    "ocr_mode": "auto",
    "image_analysis": True,
    # MinerU page-range syntax, e.g. "1-5,8"; "" means every page.
    "pages": "",
}
ALLOWED = {
    "tier": (str,),
    "ocr_mode": (str,),
    "image_analysis": (bool,),
    "pages": (str,),
}
FORMATS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}

# Renamed from MinerU's own file names to Stele's documented layout.
RENAMES = {
    "markdown.md": "document.md",
    "middle_json.json": "middle.json",
}


def _model_paths(tier: str) -> list[Path]:
    from mineru.model.registry import model_repos_for_tier

    return [repo.local_dir() for repo in model_repos_for_tier(tier)]


def run() -> None:
    source = se.input_path()
    out = se.output_dir()
    config = se.parser_config(DEFAULTS, ALLOWED)
    if source.suffix.lower() not in FORMATS:
        se.fail(se.EXIT_BAD_INPUT, f"MinerU image does not accept {source.suffix or 'this file'}")
    if config["tier"] not in ("basic", "standard"):
        se.fail(se.EXIT_BAD_INPUT, "tier must be 'basic' or 'standard'")
    if config["ocr_mode"] not in ("auto", "txt", "ocr"):
        se.fail(se.EXIT_BAD_INPUT, "ocr_mode must be 'auto', 'txt' or 'ocr'")

    # Models come only from the image; MinerU must never try a download.
    os.environ["MINERU_MODEL_SOURCE"] = "local"
    se.require_models(_model_paths(config["tier"]))

    from importlib.metadata import version as dist_version

    from mineru.parser import parse
    from mineru.parser.writer import FileBasedDataWriter

    result = parse(
        source,
        tier=config["tier"],
        ocr_mode=config["ocr_mode"],
        image_analysis=config["image_analysis"],
        page_range=config["pages"],
    )

    # MinerU writes into a scratch directory; only the finished bundle is
    # moved into the output directory.
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "mineru-out"
    result.save(FileBasedDataWriter(str(scratch)))
    for child in sorted(scratch.iterdir()):
        target = out / RENAMES.get(child.name, child.name)
        shutil.move(str(child), str(target))

    outputs = {
        "markdown": "document.md",
        "structure": "middle.json",
        "structured_content": "structured_content.json",
    }
    if (out / "model_output.json").is_file():
        outputs["model_output"] = "model_output.json"
    se.write_manifest(
        out,
        parser="mineru",
        version=dist_version("mineru"),
        config=config,
        source=source,
        outputs=outputs,
        extra={"pages": len(getattr(result, "pages", None) or [])},
    )


if __name__ == "__main__":
    se.main(run)
