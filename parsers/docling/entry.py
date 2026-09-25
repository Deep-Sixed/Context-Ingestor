"""Stele entry point for Docling (roadmap #10). Runs inside the parser image.

Output (under STELE_OUTPUT_DIR):
  document.md            Markdown rendering
  document.docling.json  the DoclingDocument: every item keeps its provenance
                         (page number, bounding box, character span), the
                         anchors the extraction contract builds on
  stele-parser.json      manifest (see parsers/README.md)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/stele")
import stele_entry as se  # noqa: E402

DEFAULTS = {
    # OCR only where a page has no usable text layer (RapidOCR on onnxruntime).
    "do_ocr": True,
    "force_full_page_ocr": False,
    "do_table_structure": True,
}
ALLOWED = {
    "do_ocr": (bool,),
    "force_full_page_ocr": (bool,),
    "do_table_structure": (bool,),
}
FORMATS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}

MODELS = Path(os.environ.get("DOCLING_ARTIFACTS_PATH", "/opt/docling/models"))


def convert(source: Path, out: Path, config: dict) -> dict:
    """Convert one document into out; returns manifest extras."""
    import torch
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
    from docling_core.types.doc import ImageRefMode

    threads = se.thread_count()
    torch.set_num_threads(threads)
    device = AcceleratorDevice.CUDA if os.environ.get("STELE_PARSER_DEVICE") == "gpu" else AcceleratorDevice.CPU

    options = PdfPipelineOptions(
        artifacts_path=MODELS,
        do_ocr=config["do_ocr"],
        do_table_structure=config["do_table_structure"],
        ocr_options=RapidOcrOptions(
            backend="onnxruntime", force_full_page_ocr=config["force_full_page_ocr"],
        ),
        accelerator_options=AcceleratorOptions(num_threads=threads, device=device),
    )
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
    })
    result = converter.convert(source, raises_on_error=True)
    document = result.document

    (out / "document.md").write_text(document.export_to_markdown(), encoding="utf-8")
    document.save_as_json(out / "document.docling.json", image_mode=ImageRefMode.PLACEHOLDER)
    return {"pages": len(document.pages), "status": str(result.status.value)}


def run() -> None:
    source = se.input_path()
    out = se.output_dir()
    config = se.parser_config(DEFAULTS, ALLOWED)
    if source.suffix.lower() not in FORMATS:
        se.fail(se.EXIT_BAD_INPUT, f"Docling image does not accept {source.suffix or 'this file'}")
    # Written by warmup.py once every model is in the image.
    se.require_models([MODELS / ".stele-ready"])

    from importlib.metadata import version as dist_version

    extra = convert(source, out, config)
    se.write_manifest(
        out,
        parser="docling",
        version=dist_version("docling"),
        config=config,
        source=source,
        outputs={"markdown": "document.md", "structure": "document.docling.json"},
        extra=extra,
    )


if __name__ == "__main__":
    se.main(run)
