"""Topology change detection.

Tracks which series keys (label fingerprints) are known for each target metric.
When new series appear — a new device is added, an interface is replaced —
the trainer can perform a targeted re-fit for those series rather than waiting
for the next scheduled full training run.

The state is stored atomically in ``<model_dir>/topology.json``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class TopologyDiff:
    metric: str
    added: frozenset[str]    # series keys not seen before
    removed: frozenset[str]  # series keys that disappeared


class TopologyStore:
    def __init__(self, model_dir: Path):
        self._path = Path(model_dir) / "topology.json"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> dict[str, set[str]]:
        """Return {metric: {series_key, ...}} from the last saved state."""
        try:
            raw = json.loads(self._path.read_text())
            return {k: set(v) for k, v in raw.items()}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("topology store corrupt, ignoring: %s", e)
            return {}

    def save(self, known: dict[str, set[str]]) -> None:
        """Atomically write {metric: sorted(keys)} to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: sorted(v) for k, v in known.items()}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def diff(self, current: dict[str, set[str]]) -> list[TopologyDiff]:
        """Compare *current* series against the last saved state.

        Returns one :class:`TopologyDiff` per metric that has changes.
        Metrics present only in *current* (first ever seen) are always
        reported as all-added so their baselines can be fitted immediately.
        """
        known = self.load()
        diffs: list[TopologyDiff] = []
        for metric, cur_keys in current.items():
            prev_keys = known.get(metric, set())
            added = frozenset(cur_keys - prev_keys)
            removed = frozenset(prev_keys - cur_keys)
            if added or removed:
                diffs.append(TopologyDiff(metric=metric, added=added, removed=removed))
        return diffs

    def update(self, current: dict[str, set[str]]) -> None:
        """Merge *current* into stored state and persist."""
        known = self.load()
        for metric, cur_keys in current.items():
            known[metric] = cur_keys
        self.save(known)

    def purge_removed(self, current: dict[str, set[str]]) -> None:
        """Remove keys that no longer appear in Prometheus, then save."""
        known = self.load()
        for metric, cur_keys in current.items():
            if metric in known:
                known[metric] &= cur_keys  # keep only keys still present
        self.save(known)
