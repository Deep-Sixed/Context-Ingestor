# Parser images

Heavy document parsers run in pinned container images on the OCI backend
(`stele.containment.oci`), through `stele.parsers.run_parser()` or
`python -m stele.parsers run`. This directory holds the image build files.

| Parser | Roadmap | Version | CPU image | GPU image |
|---|---|---|---|---|
| MinerU | #9 | 4.0.7 | `mineru/Containerfile` (ONNX models) | `mineru/Containerfile.gpu` (torch, CUDA; untested on real GPUs) |

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

MinerU's structured outputs are `middle.json` (role `structure`: pages,
blocks, spans and bounding boxes), `structured_content.json` and, when
produced, `model_output.json`.

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
`pip install --require-hashes --no-deps`:

```sh
cd parsers/mineru
uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes -o requirements.txt
uv pip compile requirements-gpu.in --python-platform x86_64-manylinux_2_34 --python-version 3.12 \
    --generate-hashes -o requirements-gpu.txt
```

## Testing

The `parser-images` workflow builds each image and runs
`tests/test_parser_images.py` (representative documents parse end to end,
output layout, identity, a missing model fails cleanly with status 3) and the
Phase E containment proofs against the image (`STELE_PROOF_IMAGES`).
The fixtures come from `tests/fixtures/documents/make_fixtures.py`.
