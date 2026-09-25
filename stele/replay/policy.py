"""
Comparison policies for replay (roadmap #14).

A parser that is not deterministic (ML and GPU parsers such as MinerU, Marker
and Docling) cannot be expected to reproduce its output byte for byte. Its
ParserSpec may carry a comparison policy: explicit, documented rules under
which a replay counts as EQUIVALENT to the recorded run. EQUIVALENT is never
proof of reproduction and is never reported as REPRODUCED.

A policy compares two artifact manifests ({relative path: sha256}) and reads
the bytes it needs from the evidence archive, where both the recorded and the
replayed bundles are stored. Its describe() output is written to the replay
log with every verdict, so the rules a replay was judged by are on record.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from ..archive.store import BlobStore


@dataclass(frozen=True)
class PolicyVerdict:
    accepted: bool
    findings: tuple[str, ...]  # why it was not accepted; empty when accepted


class ComparisonPolicy(Protocol):
    def describe(self) -> dict[str, Any]:
        """Name, version and parameters; recorded with every replay verdict."""
        ...

    def compare(
        self,
        recorded: Mapping[str, str],
        replayed: Mapping[str, str],
        archive: BlobStore,
    ) -> PolicyVerdict:
        ...


@dataclass(frozen=True)
class Tolerance:
    """How far a replayed float may move: math.isclose(rel_tol, abs_tol)."""

    rel_tol: float = 0.0
    abs_tol: float = 0.0

    def __post_init__(self) -> None:
        if self.rel_tol < 0 or self.abs_tol < 0:
            raise ValueError("tolerances must be non-negative")

    def describe(self) -> dict[str, float]:
        return {"rel_tol": self.rel_tol, "abs_tol": self.abs_tol}


@dataclass(frozen=True)
class JsonTolerancePolicy:
    """Structural JSON comparison with numeric tolerance chosen per field.

    - Both runs must produce exactly the same set of files.
    - Files with identical digests match.
    - .json files (and .jsonl, line by line) are parsed and compared
      structurally: same keys (except ignore_keys, at any depth), same list
      lengths, equal strings, booleans and nulls, and equal integers.
    - A float is compared with the tolerance of its nearest enclosing key
      named in key_tolerances (so every number inside "bbox": [...] or
      "bbox": {"l": ...} uses the "bbox" rule), and with rel_tol/abs_tol
      everywhere else. Coordinates can then absorb a fraction of a point
      while scores and every unlisted number stay near-exact.
    - Any other file must be byte-identical.
    """

    rel_tol: float = 0.0
    abs_tol: float = 0.0
    ignore_keys: frozenset[str] = field(default_factory=frozenset)
    key_tolerances: Mapping[str, Tolerance] = field(default_factory=dict)

    NAME = "json-tolerance"
    # 2: per-key tolerances (key_tolerances); 1 applied one tolerance to
    # every float.
    VERSION = 2

    def __post_init__(self) -> None:
        if self.rel_tol < 0 or self.abs_tol < 0:
            raise ValueError("tolerances must be non-negative")
        object.__setattr__(self, "ignore_keys", frozenset(self.ignore_keys))
        tolerances = dict(self.key_tolerances)
        for key, tolerance in tolerances.items():
            if not isinstance(key, str) or not isinstance(tolerance, Tolerance):
                raise TypeError("key_tolerances maps key names to Tolerance")
        object.__setattr__(self, "key_tolerances", MappingProxyType(tolerances))

    @property
    def default(self) -> Tolerance:
        return Tolerance(self.rel_tol, self.abs_tol)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.NAME,
            "version": self.VERSION,
            "rel_tol": self.rel_tol,
            "abs_tol": self.abs_tol,
            "ignore_keys": sorted(self.ignore_keys),
            "key_tolerances": {
                key: self.key_tolerances[key].describe() for key in sorted(self.key_tolerances)
            },
        }

    def compare(
        self,
        recorded: Mapping[str, str],
        replayed: Mapping[str, str],
        archive: BlobStore,
    ) -> PolicyVerdict:
        findings = [f"missing from replay: {p}" for p in sorted(set(recorded) - set(replayed))]
        findings += [f"not in the record: {p}" for p in sorted(set(replayed) - set(recorded))]
        for path in sorted(set(recorded) & set(replayed)):
            if recorded[path] == replayed[path]:
                continue
            if path.endswith(".json") or path.endswith(".jsonl"):
                findings += self._compare_json(
                    path, archive.read(recorded[path]), archive.read(replayed[path])
                )
            else:
                findings.append(f"bytes differ: {path}")
        return PolicyVerdict(accepted=not findings, findings=tuple(findings))

    def _compare_json(self, path: str, a: bytes, b: bytes) -> list[str]:
        try:
            if path.endswith(".jsonl"):
                left = [json.loads(line) for line in a.decode("utf-8").splitlines() if line.strip()]
                right = [json.loads(line) for line in b.decode("utf-8").splitlines() if line.strip()]
            else:
                left, right = json.loads(a), json.loads(b)
        except (UnicodeDecodeError, ValueError) as exc:
            return [f"not comparable as JSON: {path}: {exc}"]
        findings: list[str] = []
        self._walk(left, right, path, self.default, findings)
        return findings

    def _walk(self, a: Any, b: Any, where: str, tol: Tolerance, findings: list[str]) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            keys_a = {k for k in a if k not in self.ignore_keys}
            keys_b = {k for k in b if k not in self.ignore_keys}
            for key in sorted(keys_a ^ keys_b):
                findings.append(f"key only on one side: {where}/{key}")
            for key in sorted(keys_a & keys_b):
                inner = self.key_tolerances.get(key, tol)
                self._walk(a[key], b[key], f"{where}/{key}", inner, findings)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                findings.append(f"length {len(a)} != {len(b)}: {where}")
                return
            for i, (x, y) in enumerate(zip(a, b)):
                self._walk(x, y, f"{where}[{i}]", tol, findings)
        elif _is_number(a) and _is_number(b) and (isinstance(a, float) or isinstance(b, float)):
            if not math.isclose(a, b, rel_tol=tol.rel_tol, abs_tol=tol.abs_tol):
                findings.append(
                    f"{a!r} != {b!r} beyond tolerance (rel {tol.rel_tol}, abs {tol.abs_tol}): {where}"
                )
        elif type(a) is not type(b) or a != b:
            findings.append(f"{a!r} != {b!r}: {where}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
