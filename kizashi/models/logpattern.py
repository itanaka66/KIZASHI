"""Log pattern anomaly detection using the Drain algorithm.

Drain builds a fixed-depth prefix tree for online log template mining.
New templates (unseen in the previous training snapshot) are flagged as anomaly signals.

Reference: He et al., "Drain: An Online Log Parsing Approach with Fixed Depth Tree" (ICWS 2017).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_WILDCARD = "<*>"

# Pre-processing: mask variable tokens before template matching so they do not
# prevent similar log lines from being grouped together.
_PREPROCESS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
    (re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b"), "<MAC>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b\d+\b"), "<NUM>"),
]


@dataclass
class LogTemplate:
    id: int
    tokens: list[str]
    count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    is_new: bool = True

    @property
    def pattern(self) -> str:
        return " ".join(self.tokens)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tokens": self.tokens,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "is_new": self.is_new,
        }

    @classmethod
    def from_dict(cls, d: dict) -> LogTemplate:
        return cls(**d)


class DrainTree:
    """Fixed-depth prefix tree for online log template mining.

    Tree structure: root → {token_length: {first_token: [LogTemplate]}}

    Usage::

        tree = DrainTree()
        for line in log_lines:
            tmpl = tree.add(line)
        tree.mark_known()          # snapshot point — future adds may be "new"
        for line in new_log_lines:
            tmpl = tree.add(line)
        new_templates = list(tree.iter_new())
    """

    def __init__(
        self,
        sim_threshold: float = 0.5,
        max_children: int = 100,
        max_clusters: int = 1000,
        depth: int = 3,
    ) -> None:
        self._sim_threshold = sim_threshold
        self._max_children = max_children
        self._max_clusters = max_clusters
        self._depth = depth

        # {length: {prefix_token: [LogTemplate]}}
        self._tree: dict[int, dict[str, list[LogTemplate]]] = {}
        self._templates: dict[int, LogTemplate] = {}
        self._next_id = 0

    # ------------------------------------------------------------------ public

    @property
    def templates(self) -> dict[int, LogTemplate]:
        return self._templates

    def add(self, line: str, ts: float | None = None) -> LogTemplate:
        """Parse a log line, update the tree, return the matched or new template."""
        if ts is None:
            ts = time.time()
        tokens = self._tokenize(line)
        if not tokens:
            return self._new_template(["<EMPTY>"], ts)

        n = len(tokens)
        prefix = tokens[0]
        clusters = self._tree.setdefault(n, {}).setdefault(prefix, [])

        best, best_sim = self._best_match(clusters, tokens)

        if best is not None and best_sim >= self._sim_threshold:
            self._merge(best, tokens, ts)
            return best

        if len(self._templates) >= self._max_clusters:
            self._evict_oldest()

        tmpl = self._new_template(tokens, ts)
        if len(clusters) < self._max_children:
            clusters.append(tmpl)
        return tmpl

    def mark_known(self) -> None:
        """Clear is_new on all templates. Call after a training snapshot."""
        for tmpl in self._templates.values():
            tmpl.is_new = False

    def iter_new(self) -> Iterator[LogTemplate]:
        """Yield templates that appeared since the last mark_known() call."""
        return (t for t in self._templates.values() if t.is_new)

    # ----------------------------------------------------------------- private

    @staticmethod
    def _tokenize(line: str) -> list[str]:
        text = line.strip()
        for pat, repl in _PREPROCESS:
            text = pat.sub(repl, text)
        return text.split()

    @staticmethod
    def _similarity(tmpl_tokens: list[str], tokens: list[str]) -> float:
        if len(tmpl_tokens) != len(tokens):
            return 0.0
        matched = non_wildcard = 0
        for t, s in zip(tmpl_tokens, tokens):
            if t == _WILDCARD:
                continue
            non_wildcard += 1
            if t == s:
                matched += 1
        return matched / non_wildcard if non_wildcard else 1.0

    def _best_match(
        self, clusters: list[LogTemplate], tokens: list[str]
    ) -> tuple[LogTemplate | None, float]:
        best, best_sim = None, -1.0
        for tmpl in clusters:
            s = self._similarity(tmpl.tokens, tokens)
            if s > best_sim:
                best_sim, best = s, tmpl
        return best, best_sim

    @staticmethod
    def _merge(tmpl: LogTemplate, tokens: list[str], ts: float) -> None:
        """Wildcardize positions where tokens differ from the template."""
        for i, (t, s) in enumerate(zip(tmpl.tokens, tokens)):
            if t != _WILDCARD and t != s:
                tmpl.tokens[i] = _WILDCARD
        tmpl.count += 1
        tmpl.last_seen = ts

    def _new_template(self, tokens: list[str], ts: float) -> LogTemplate:
        tmpl = LogTemplate(
            id=self._next_id,
            tokens=list(tokens),
            count=1,
            first_seen=ts,
            last_seen=ts,
            is_new=True,
        )
        self._next_id += 1
        self._templates[tmpl.id] = tmpl
        return tmpl

    def _evict_oldest(self) -> None:
        oldest = min(self._templates.values(), key=lambda t: t.last_seen)
        del self._templates[oldest.id]
        for grp in self._tree.values():
            for lst in grp.values():
                try:
                    lst.remove(oldest)
                except ValueError:
                    pass

    # --------------------------------------------------------- serialisation

    def to_dict(self) -> dict:
        return {
            "sim_threshold": self._sim_threshold,
            "max_children": self._max_children,
            "max_clusters": self._max_clusters,
            "depth": self._depth,
            "next_id": self._next_id,
            "templates": {str(tid): t.to_dict() for tid, t in self._templates.items()},
            "tree": {
                str(n): {
                    prefix: [t.id for t in clusters]
                    for prefix, clusters in grp.items()
                }
                for n, grp in self._tree.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> DrainTree:
        obj = cls(
            sim_threshold=d["sim_threshold"],
            max_children=d["max_children"],
            max_clusters=d["max_clusters"],
            depth=d["depth"],
        )
        obj._next_id = d["next_id"]
        obj._templates = {int(k): LogTemplate.from_dict(v) for k, v in d["templates"].items()}
        for n_str, grp in d["tree"].items():
            n = int(n_str)
            obj._tree[n] = {}
            for prefix, tid_list in grp.items():
                obj._tree[n][prefix] = [
                    obj._templates[tid] for tid in tid_list if tid in obj._templates
                ]
        return obj

    def save(self, path: Path) -> None:
        """Atomically write tree state to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> DrainTree:
        return cls.from_dict(json.loads(path.read_text()))
