# Full-Corpus Production Verification and Sign-Off

Before a new sandbox setup replaces the one production runs today, it has to
agree with it on the real corpus, every document of it. `stele.verification`
runs that check and records a named person's sign-off on the result. The
baseline is today's bubblewrap-only setup.

The procedure has four steps: pin the corpus, run the campaign, build the
report, sign it off.

```
corpus ──▶ campaign ──────────────▶ journal (resumable)
              │ every document × every lane
              ▼
           ledger + archive (sealed runs)
              │
              ▼
           report (re-checks everything; gates) ──▶ sign-off event in the hash chain
```

## 1. Pin the corpus (`corpus.py`)

```bash
python -m stele.verification corpus /data/production -o corpus.json
```

- `stele.corpus` v1 lists every regular file under the root with its SHA-256
  and size, sorted by path.
- It also lists everything it left out, with the reason: symlinks (never
  followed), FIFOs, devices, unreadable files. Coverage is explicit, never
  silently partial.
- The manifest is canonical JSON, and its SHA-256 is the **corpus id**. Every
  journal, report and sign-off names it.

## 2. Run the campaign (`campaign.py`)

```bash
python -m stele.verification run --corpus corpus.json --root /data/production \
    --ledger verify.db --archive verify-archive --journal campaign.jsonl \
    --script parser.py --baseline bubblewrap --candidate oci-runc --candidate oci-runsc
```

A **lane** is one way to run the parser under test: a backend plus the spec
that runs the parser on it. Every lane must be the same parser name and
version.

| Parser | Option | Lanes |
|--------|--------|-------|
| Python script run as `/stele/parser` | `--script parser.py` | `bubblewrap` (host interpreter), `oci-runc`, `oci-runsc`, `oci-runc-gpu` (the image's `python3`) |
| Packaged ML parser | `--parser mineru\|marker\|docling` | `oci-*`, with the parser's pinned image and limits |
| Wasm extractor | `--extractor chatgpt-export-split` | `wasmtime` |

A script parser's version defaults to `sha256-<first 16 hex of the script>`,
so a changed script is a different parser.

For each document, the baseline lane runs first, then each candidate lane.
For each (document, lane) the campaign:

1. runs the parser through the normal path: `run_parser` → `run_in_sandbox`,
   with the ledger's archive and the lane's backend forced, never
   auto-selected;
2. requires the input Snapshot the parser read to hash to the corpus
   manifest's SHA-256 for that document;
3. records and seals a successful run in the ledger, then deletes the working
   output, since the archive holds it;
4. appends one **observation** to the journal: `sealed`, `failed` (with the
   run's structured failure reason and telemetry), `not_applicable` (the
   parser does not accept the document), `containment_error`, or `error`.

The journal is JSON lines, and each line is fsynced before the next run
starts.

- **Resuming:** the header pins the corpus id, parser, config digest and
  lanes. A rerun skips every (document, lane) already observed, and a torn
  final line from a crash is dropped and run again. `--limit N` runs a batch
  and stops.
- **Unavailable backends:** a campaign whose lane backend is unavailable
  refuses to start. It never falls back to another backend.

## 3. Build the report (`report.py`)

```bash
python -m stele.verification report --corpus corpus.json --root /data/production \
    --ledger verify.db --archive verify-archive --journal campaign.jsonl \
    [--waivers waivers.json] [--max-baseline-failure-rate 0.01] [--max-slowdown 3] \
    -o report.json
```

The journal is trusted only for what the ledger cannot know: why a run failed
and how long it took. For every sealed run, the report re-checks:

- the record exists, is still `SEALED`, and is the run the journal names
  (run id, artifact hash, parser name and version);
- the ledger says it was made on the backend of the lane that claims it, with
  the campaign's parser configuration, and no other document or lane claims
  the same record (so one lane's output can never stand in for another's);
- its `source_hash` is the corpus document's SHA-256;
- its bundle re-verifies in the evidence archive;
- where the parser has an extraction normalizer (Markdown from MinerU,
  Marker, Docling; ChatGPT), the canonical extraction normalizes and every
  unit resolves against the sealed bytes.

Each candidate is then compared with the baseline, document by document:

| Outcome | Meaning |
|---------|---------|
| `identical` | both sealed, same artifact manifest |
| `equivalent` | both sealed, and the parser's comparison policy (the same one replays use) accepts the difference |
| `diverged` | both sealed, outside the policy (or any difference, without one) |
| `regression` | the baseline sealed, the candidate failed |
| `recovered` | the candidate sealed where the baseline fails |
| `consistent_failure` | both failed, for the same reason |
| `failure_mismatch` | both failed for different reasons, or only one lane accepted the document |
| `not_applicable`, `incomplete`, `error` | neither lane applies; a lane has no observation; an error on either lane |

The report also summarizes each lane: status counts, failure reasons, and
p50/p95/max of wall time, CPU time and peak memory. For each candidate it
gives outcome counts and the median slowdown against the baseline.

### Gates

A report passes only if every gate passes.

| Gate | Passes when |
|------|-------------|
| `corpus_unchanged` | the root still hashes exactly as the manifest (fails if the corpus was not checked) |
| `coverage` | every document has an observation on every lane |
| `baseline_produced_output` | the baseline sealed at least one document |
| `no_errors` | no Stele, host or containment errors |
| `records_verified` | every sealed record passes the re-checks above |
| `ledger_verified` | the event chain and the ledger tables agree (`verify_ledger`) |
| `candidates_agree` | no unwaived `diverged`, `regression` or `failure_mismatch` |
| `waivers_used` | every waiver matches a finding |
| `baseline_failure_rate` | (optional) the baseline fails at most this share of applicable documents |
| `slowdown` | (optional) each candidate's median wall-time ratio is within the limit |

**Waivers** accept a known comparison finding. Each waiver names one
document, one candidate lane and one outcome, and gives a written reason:

```json
[{"path": "scans/0042.pdf", "lane": "oci-runsc", "outcome": "regression",
  "reason": "gVisor lacks the AVX path; tracked in #123"}]
```

Only `diverged`, `regression` and `failure_mismatch` can be waived.
Integrity problems (a record that does not verify, a changed corpus, an
unverifiable ledger, an error) cannot be waived.

The report is canonical JSON (`stele.verification-report` v1). Its SHA-256
is what gets signed off.

## 4. Sign off (`signoff.py`)

```bash
python -m stele.verification sign-off --corpus corpus.json --root /data/production \
    --ledger verify.db --archive verify-archive --journal campaign.jsonl \
    report.json --by "Name" --note "Q3 production sign-off"
```

The sign-off does not take the report's word. It refuses unless all of these
hold:

- a **rebuild** of the report from the ledger, journal and corpus, with the
  report's own thresholds and waivers, gives the same report. An edited
  report with its gates flipped to passed is refused, even if it is
  re-encoded canonically;
- every gate passed;
- the event chain still contains the head the report was built at;
- every record in the report is still `SEALED` with the artifact hash it
  names.

It then stores the report in the evidence archive and appends a
`verification.signed_off` event to the hash-chained event log. The event
names the report digest, the corpus, the parser, the lanes and the person
signing off. The command prints the event's anchor `seq:hash`; publish it
outside the ledger.

```bash
python -m stele.verification check --ledger verify.db --archive verify-archive <report sha256>
python -m stele.verification list  --ledger verify.db --archive verify-archive
```

`check` re-verifies a sign-off later:

- the event is in the chain;
- the archived report still hashes to its digest;
- every record the report covers is still sealed.

If a record was invalidated after the sign-off, `check` reports the sign-off
as **stale**.

## What this does not do

- **It does not decide which corpus is production.** Pinning it is the
  operator's job; the manifest makes the choice auditable.
- **Equivalence is only as strong as the parser's comparison policy.** A
  non-deterministic parser without a policy reports every difference as
  `diverged`.
- **An orphan record after a crash.** If a crash lands between sealing a
  record and journaling it, the resumed campaign runs that document again,
  and the earlier sealed record stays in the ledger unreferenced by the
  report.
- **Anyone with write access to the SQLite file can rewrite the chain,
  sign-off included.** As with the event log, publishing the anchor is what
  makes it hold.

## Tests (`tests/test_verification.py`)

| Proof | Test |
|-------|------|
| Content-pinned corpus, strict manifest, exclusions listed, drift caught | `TestCorpus` |
| Every document on every lane, sealed or failed with its reason; resume, torn lines, mismatched journals | `TestCampaign` |
| Every comparison outcome, policy equivalence, waivers, coverage, integrity and threshold gates | `TestReport` |
| Anchored sign-off; refused for failed, stale or forged reports; stale after invalidation | `TestSignOff` |
| Bubblewrap baseline end to end, an OCI candidate against it, and the whole CLI | `TestRealSandboxes` |
