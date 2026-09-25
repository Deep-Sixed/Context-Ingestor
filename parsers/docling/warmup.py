"""Build-time only: fetch Docling's models and prove they load offline.

Downloads the default model set (layout, TableFormer, code/formula, picture
classifier, RapidOCR) into DOCLING_ARTIFACTS_PATH, then converts a small
generated PDF with Hugging Face forced offline, so a model Docling would
otherwise fetch lazily fails the build instead of a parse.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/stele")


def main() -> None:
    models = Path(os.environ["DOCLING_ARTIFACTS_PATH"])
    subprocess.run(
        ["docling-tools", "models", "download", "--quiet", "--output-dir", str(models)],
        check=True,
    )

    # From here on, nothing may be downloaded.
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    import entry

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf = root / "warmup.pdf"
        _make_pdf(pdf)
        out = root / "out"
        out.mkdir()
        entry.convert(pdf, out, dict(entry.DEFAULTS))
        text = (out / "document.md").read_text(encoding="utf-8")
        if "warm-up" not in text:
            raise SystemExit(f"warm-up conversion produced no text:\n{text}")
        print("warm-up: pdf ok")

    (models / ".stele-ready").write_text("docling models baked at build time\n")


def _make_pdf(path: Path) -> None:
    """A one-page PDF with a text layer (standard Helvetica), written by hand."""
    lines = ["Stele warm-up", "Parsers run in a sandbox with no network."]
    ops = "".join(
        f"BT /F1 18 Tf 72 {700 - 30 * i} Td ({line}) Tj ET\n" for i, line in enumerate(lines)
    ).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"endstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    data += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(data))


if __name__ == "__main__":
    main()
