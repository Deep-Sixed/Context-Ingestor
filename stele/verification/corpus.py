"""
The corpus under verification, pinned by content: `stele.corpus`, version 1.

A production verification is only meaningful if everyone can say which
documents it covered. A Corpus is the sorted list of every regular file under
a root directory with its SHA-256 and size, plus every path that was left out
and why (symlinks, sockets, unreadable files): coverage is explicit, never
silently partial. Its canonical JSON is strict, and its digest is the corpus
id every campaign, report and sign-off names.

check(root) re-hashes the tree: a verification whose documents changed,
disappeared or gained siblings since the manifest was taken is not a
verification of that corpus.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from ..archive.records import canonical_json, is_digest

SCHEMA = "stele.corpus"
SCHEMA_VERSION = 1
_CHUNK = 1 << 20


class CorpusFormatError(ValueError):
    """Bytes or values that are not a valid stele.corpus/v1."""


def _check_path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        raise CorpusFormatError(f"corpus path must be a non-empty string: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(p in ("", ".", "..") for p in path.split("/")) or "\\" in path:
        raise CorpusFormatError(f"corpus path must be a normalized relative POSIX path: {path!r}")
    return path


@dataclass(frozen=True)
class CorpusEntry:
    path: str       # relative POSIX path under the corpus root
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _check_path(self.path)
        if not is_digest(self.sha256):
            raise CorpusFormatError(f"{self.path}: not a SHA-256 digest: {self.sha256!r}")
        if type(self.size) is not int or self.size < 0:
            raise CorpusFormatError(f"{self.path}: size must be a non-negative int")

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class Excluded:
    path: str
    why: str        # "symlink", "not a regular file", "unreadable: ..."

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "why": self.why}


@dataclass(frozen=True)
class Corpus:
    name: str
    entries: tuple[CorpusEntry, ...]
    excluded: tuple[Excluded, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "excluded", tuple(self.excluded))
        if not isinstance(self.name, str) or not self.name:
            raise CorpusFormatError("corpus name must be a non-empty string")
        paths = [e.path for e in self.entries]
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            raise CorpusFormatError("corpus entries must be unique and sorted by path")
        skipped = [x.path for x in self.excluded]
        if skipped != sorted(skipped) or set(skipped) & set(paths):
            raise CorpusFormatError("excluded paths must be sorted and not also be entries")

    @property
    def corpus_id(self) -> str:
        return hashlib.sha256(self.to_canonical()).hexdigest()

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def by_path(self) -> dict[str, CorpusEntry]:
        return {e.path: e for e in self.entries}

    # -- building and checking ------------------------------------------------

    @classmethod
    def build(cls, root: Path, *, name: str | None = None) -> "Corpus":
        """Hash every regular file under root; record everything left out."""
        root = Path(root)
        if not root.is_dir():
            raise NotADirectoryError(f"corpus root {root} is not a directory")
        entries, excluded = [], []
        for rel, full, kind in _walk(root):
            if kind != "file":
                excluded.append(Excluded(rel, kind))
                continue
            try:
                digest, size = _hash_file(full)
            except OSError as exc:
                excluded.append(Excluded(rel, f"unreadable: {exc.strerror or exc}"))
                continue
            entries.append(CorpusEntry(rel, digest, size))
        return cls(
            name=name or root.resolve().name or "corpus",
            entries=tuple(sorted(entries, key=lambda e: e.path)),
            excluded=tuple(sorted(excluded, key=lambda x: x.path)),
        )

    def check(self, root: Path) -> list[str]:
        """Problems between the manifest and the tree at root; [] means it is this corpus."""
        root = Path(root)
        problems: list[str] = []
        present = {rel for rel, _, _ in _walk(root)}
        wanted = {e.path: e for e in self.entries}
        for rel in sorted(present - set(wanted) - {x.path for x in self.excluded}):
            problems.append(f"{rel}: not in the corpus manifest")
        for path, entry in wanted.items():
            full = root / path
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                problems.append(f"{path}: missing")
                continue
            if not stat.S_ISREG(st.st_mode):
                problems.append(f"{path}: no longer a regular file")
                continue
            try:
                digest, size = _hash_file(full)
            except OSError as exc:
                problems.append(f"{path}: unreadable: {exc.strerror or exc}")
                continue
            if (digest, size) != (entry.sha256, entry.size):
                problems.append(f"{path}: content changed since the manifest was taken")
        return problems

    # -- serialization ----------------------------------------------------------

    def to_canonical(self) -> bytes:
        return canonical_json({
            "schema": SCHEMA, "schema_version": SCHEMA_VERSION, "name": self.name,
            "entries": [e.to_json() for e in self.entries],
            "excluded": [x.to_json() for x in self.excluded],
        })

    @classmethod
    def from_canonical(cls, data: bytes) -> "Corpus":
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorpusFormatError(f"not UTF-8 JSON: {exc}") from exc
        fields = {"schema", "schema_version", "name", "entries", "excluded"}
        if not isinstance(obj, dict) or set(obj) != fields:
            raise CorpusFormatError(f"corpus fields must be exactly {sorted(fields)}")
        if obj["schema"] != SCHEMA or obj["schema_version"] != SCHEMA_VERSION:
            raise CorpusFormatError(f"expected {SCHEMA} v{SCHEMA_VERSION}")
        try:
            corpus = cls(
                name=obj["name"],
                entries=tuple(CorpusEntry(**e) for e in obj["entries"]),
                excluded=tuple(Excluded(**x) for x in obj["excluded"]),
            )
        except TypeError as exc:
            raise CorpusFormatError(f"malformed entry: {exc}") from exc
        if corpus.to_canonical() != data:
            raise CorpusFormatError("corpus manifest is not in canonical form")
        return corpus

    @classmethod
    def load(cls, path: Path) -> "Corpus":
        return cls.from_canonical(Path(path).read_bytes())

    def save(self, path: Path) -> None:
        Path(path).write_bytes(self.to_canonical())


def _walk(root: Path) -> Iterator[tuple[str, Path, str]]:
    """(relative POSIX path, full path, "file" | why excluded), never following links."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames.sort()
        for d in list(dirnames):
            if (base / d).is_symlink():
                dirnames.remove(d)
                yield (base / d).relative_to(root).as_posix(), base / d, "symlink"
        for f in sorted(filenames):
            full = base / f
            rel = full.relative_to(root).as_posix()
            try:
                mode = os.lstat(full).st_mode
            except OSError as exc:
                yield rel, full, f"unreadable: {exc.strerror or exc}"
                continue
            if stat.S_ISLNK(mode):
                yield rel, full, "symlink"
            elif not stat.S_ISREG(mode):
                yield rel, full, "not a regular file"
            else:
                yield rel, full, "file"


def _hash_file(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
