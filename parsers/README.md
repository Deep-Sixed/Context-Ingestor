# Parser images

Heavy document parsers run in pinned container images on the OCI backend
(`stele.containment.oci`), through `stele.parsers.run_parser()` or
`python -m stele.parsers run`. This directory holds the image build files.

| Parser | Roadmap | Version | CPU image | GPU image |
|---|---|---|---|---|
| MinerU | #9 | 4.0.7 | `mineru/Containerfile` (ONNX models) | `mineru/Containerfile.gpu` (torch, CUDA; untested on real GPUs) |
| Marker | #10 | 2.0.0 | `marker/Containerfile` (fast text-layer mode; CPU torch) | none |
| Docling | #10 | 2.130.0 | `docling/Containerfile` (layout, TableFormer, RapidOCR; CPU torch) | none |

Marker 2's OCR runs a vision-language model through a separate llama.cpp or
vLLM server, which this CPU image does not include: it converts documents with
a text layer (PDF, DOCX, PPTX, XLSX, HTML, EPUB) and refuses scans
(`no text extracted`, status 4) rather than returning them empty. Docling OCRs
pages without a text layer with RapidOCR.

Marker and Docling images are x86_64 only (their dependency locks target
`x86_64-manylinux_2_28`). MinerU's lock is universal.

## Building (the trusted acquisition step)

Building an image is the only time anything is downloaded: Python packages
(pinned by version and hash) and model weights (baked into the image). Parsing
never touches the network, since the parser container has none.

```sh
python -m stele.parsers build-command mineru            # prints the command
docker build --file parsers/mineru/Containerfile --tag localhost/stele/mineru:4.0.7 parsers
```

Stele never pulls or builds at run time. Until the image exists locally, the
backend reports itself unavailable and the run is refused.

The parser's identity is the **content digest** the engine reports for the
image, not its tag. Every run records it, together with a digest of the exact
configuration the parser was given (`ParserRun.identity`).

## Running

```sh
python -m stele.parsers run mineru --input paper.pdf --artifact-dir out/
python -m stele.parsers run mineru --input scan.pdf --artifact-dir out/ \
    --config '{"ocr_mode": "ocr"}' --memory 8g --cpus 4
```

Each run gets:

- **Limits the engine enforces.** The memory limit (no swap), CPU and PID
  limits, and a timeout. A host whose engine cannot enforce them (e.g.
  rootless Podman without delegated cgroup controllers) is refused. Thread
  pools (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, the torch thread count, ...)
  are capped to the CPU allowance.
- **No partial output.** A run that fails, times out or is killed at the
  memory limit keeps nothing it wrote, and nothing is stored in the evidence
  store. The input Snapshot is still archived.
- **GPU when the parser allows it.** Parsers declare `never`, `optional` or
  `required`. `optional` uses the GPU image when the host can pass an NVIDIA
  GPU through and the GPU image is built, and the CPU image otherwise, and the
  run records which one it used. `required` never falls back.

## Output layout

Everything is written under `/stele/output` (the artifact directory):

```
stele-parser.json    manifest: parser, version, device, config, input name,
                     and the role of each output file
document.md          Markdown rendering            (role: markdown)
...                  parser-specific structured output, listed in the manifest
images/...           extracted figures, referenced from the outputs
```

The structured output (role `structure`) keeps page positions for every
block:

| Parser | `structure` file | Positions |
|---|---|---|
| MinerU | `middle.json` (+ `structured_content.json`, `model_output.json`) | page, block and span bounding boxes |
| Marker | `document.json` | page and block polygons/bounding boxes, block types |
| Docling | `document.docling.json` (DoclingDocument) | per item: page number, bounding box and character span |

Models that load lazily (Marker, Docling) are baked in by `warmup.py`, which
converts a small generated document during the build; it writes
`.stele-ready` into the model directory only when that succeeds, and the entry
scripts refuse to run (status 3) without it.

## Ledger and replay

`stele.parsers.replay.record_parser_run(ledger, run, source=Source.from_path(doc))`
records a successful run under the parser's name, version, measured image
digest and merged configuration. The Source is required: the parsers choose
their reader by the file suffix, and the replay must give the document its
original name. The record also keeps the run's device (CPU or GPU image) and
limits. `replay_spec(parser)` lets the replay engine (`docs/replay.md`) run
it again on the recorded document, on the recorded device's image and under
the recorded limits. ML parsers are not deterministic, so a replay is judged
by the parser's `comparison` policy: coordinates may move by half a point or
pixel, model scores by 1e-3, every other number and all text must match
(`EQUIVALENT` or `DIVERGED`, never `REPRODUCED`). It is `UNREPLAYABLE` when
the image is not present locally, no longer has the recorded digest, or the
recorded device (a GPU) is not available here.

## Exit statuses

| Status | Meaning |
|---|---|
| 0 | success |
| 2 | unusable input or configuration (e.g. an unknown configuration key) |
| 3 | model weights missing from the image; nothing is ever downloaded at run time |
| 4 | the parser failed |
| 137 | killed, most likely at the memory limit |

## Dependency locks

`requirements*.txt` are generated with hashes and installed with
`pip install --require-hashes --no-deps`. For Marker and Docling, PyTorch and
its CUDA packages are left out of the lock (`--no-emit-package`) and CPU
wheels of the resolved versions are installed from the PyTorch CPU index,
pinned by version:

```sh
cd parsers/mineru
uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes -o requirements.txt
uv pip compile requirements-gpu.in --python-platform x86_64-manylinux_2_34 --python-version 3.12 \
    --generate-hashes -o requirements-gpu.txt
# marker / docling (repeat --no-emit-package for torch, torchvision, triton,
# cuda-* and nvidia-* as resolved):
uv pip compile requirements.in --python-platform x86_64-manylinux_2_28 --python-version 3.12 \
    --generate-hashes --no-emit-package torch --no-emit-package torchvision ... -o requirements.txt
```

## Testing

The `parser-images` workflow builds each image and runs
`tests/test_parser_images.py` (representative documents parse end to end,
output layout, identity, a missing model fails cleanly with status 3, the
memory and time limits end runs cleanly with nothing kept) and the
containment proofs against the image (`STELE_PROOF_IMAGES`).
The fixtures come from `tests/fixtures/documents/make_fixtures.py`.
