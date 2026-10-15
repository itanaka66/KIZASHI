"""Persistent label store for P3 feedback records.

Records are stored as newline-delimited JSON (JSONL) — one record per line.
The file is append-only; on read all lines are validated and malformed lines
are skipped so a partial write never corrupts earlier records.

Thread safety: all writes are serialised by an internal Lock.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical label values
LABEL_PENDING = "pending"                    # KIZASHI alert fired, awaiting operator judgment
LABEL_TRUE_POSITIVE = "true_positive"        # Operator confirmed: real failure predicted
LABEL_FALSE_POSITIVE = "false_positive"      # Operator confirmed: false alarm
LABEL_RESOLVED = "resolved"                  # Alert auto-resolved / operator closed
LABEL_CONFIRMED_FAILURE = "confirmed_failure"  # Non-predictive alarm confirmed failure

_VALID_OPERATOR_LABELS = frozenset(
    [LABEL_TRUE_POSITIVE, LABEL_FALSE_POSITIVE, LABEL_RESOLVED]
)


@dataclass
class FeedbackRecord:
    fingerprint: str
    alert_name: str
    instance: str
    if_name: str
    failure_mode: str
    started_at: float                   # unix seconds
    label: str
    labeled_at: float                   # unix seconds
    labeled_by: str                     # "operator" | "alertmanager" | "auto"
    alert_labels: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    resolution: str = ""                # optional: "optic_replaced", "false_alarm", …

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> FeedbackRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class LabelStore:
    """Append-only JSONL label store.

    Parameters
    ----------
    path : path to the JSONL file. The parent directory is created on first write.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: list[FeedbackRecord] = []
        self._loaded = False

    # ---------------------------------------------------------------- public

    def append(self, record: FeedbackRecord) -> None:
        """Append a record to the store (thread-safe)."""
        with self._lock:
            self._ensure_loaded()
            self._records.append(record)
            self._write_line(record)

    def list_all(self) -> list[FeedbackRecord]:
        with self._lock:
            self._ensure_loaded()
            return list(self._records)

    def list_since(self, since: float) -> list[FeedbackRecord]:
        with self._lock:
            self._ensure_loaded()
            return [r for r in self._records if r.labeled_at >= since]

    def get_by_fingerprint(self, fingerprint: str) -> list[FeedbackRecord]:
        """Return all records for a given alert fingerprint (oldest first)."""
        with self._lock:
            self._ensure_loaded()
            return [r for r in self._records if r.fingerprint == fingerprint]

    def latest_by_fingerprint(self) -> dict[str, FeedbackRecord]:
        """Return the most recent record per fingerprint (for training export)."""
        with self._lock:
            self._ensure_loaded()
            latest: dict[str, FeedbackRecord] = {}
            for r in self._records:
                latest[r.fingerprint] = r
            return latest

    def training_labels(self, since: float | None = None) -> list[FeedbackRecord]:
        """Return finalized records suitable for P4 training data.

        Includes true_positive and confirmed_failure records only
        (false_positive records are used as hard negatives separately).
        """
        latest = self.latest_by_fingerprint()
        result = [
            r for r in latest.values()
            if r.label in (LABEL_TRUE_POSITIVE, LABEL_FALSE_POSITIVE, LABEL_CONFIRMED_FAILURE)
            and (since is None or r.labeled_at >= since)
        ]
        result.sort(key=lambda r: r.started_at)
        return result

    # --------------------------------------------------------------- private

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._records = _read_jsonl(self._path)
        self._loaded = True

    def _write_line(self, record: FeedbackRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def _read_jsonl(path: Path) -> list[FeedbackRecord]:
    if not path.exists():
        return []
    records: list[FeedbackRecord] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(FeedbackRecord.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("label_store: skipping malformed line %d: %s", lineno, exc)
    return records
