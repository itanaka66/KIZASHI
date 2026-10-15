"""Alertmanager HTTP API client.

Used by the trainer to fetch active silences so that their time ranges
can be excluded from training data (§8.2 of the design doc).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx


class AlertmanagerError(RuntimeError):
    pass


@dataclass
class SilenceMatcher:
    name: str
    value: str
    is_regex: bool = False
    is_equal: bool = True  # False means "not equal"

    def matches(self, labels: dict[str, str]) -> bool:
        label_val = labels.get(self.name, "")
        if self.is_regex:
            matched = bool(re.fullmatch(self.value, label_val))
        else:
            matched = label_val == self.value
        return matched if self.is_equal else not matched


@dataclass
class Silence:
    id: str
    start: float  # unix seconds
    end: float    # unix seconds
    matchers: list[SilenceMatcher] = field(default_factory=list)

    def covers(self, ts: float) -> bool:
        return self.start <= ts <= self.end

    def matches_labels(self, labels: dict[str, str]) -> bool:
        """Return True when every matcher in the silence matches the given labels."""
        return all(m.matches(labels) for m in self.matchers)


class AlertmanagerClient:
    def __init__(
        self,
        url: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def active_silences(self) -> list[Silence]:
        """Return all currently active silences from Alertmanager."""
        try:
            resp = self._http.get("/api/v2/silences")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise AlertmanagerError(f"alertmanager: {e}") from e
        silences = []
        for item in resp.json():
            if item.get("status", {}).get("state") != "active":
                continue
            try:
                start = _parse_rfc3339(item["startsAt"])
                end = _parse_rfc3339(item["endsAt"])
            except (KeyError, ValueError):
                continue
            matchers = [
                SilenceMatcher(
                    name=m["name"],
                    value=m["value"],
                    is_regex=m.get("isRegex", False),
                    is_equal=m.get("isEqual", True),
                )
                for m in item.get("matchers", [])
            ]
            silences.append(Silence(id=item["id"], start=start, end=end, matchers=matchers))
        return silences


def _parse_rfc3339(s: str) -> float:
    # Python 3.11+ fromisoformat handles timezone offsets; replace Z for compat.
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def silence_mask(
    timestamps: "import numpy as np; np.ndarray",
    silences: list[Silence],
    labels: dict[str, str],
    buffer: float = 1800.0,
) -> "np.ndarray":
    """Return a boolean mask; True = keep, False = inside a matching silence ± buffer."""
    import numpy as np

    mask = np.ones(len(timestamps), dtype=bool)
    for silence in silences:
        if not silence.matches_labels(labels):
            continue
        # Exclude the silence window plus a configurable buffer on each side.
        lo = silence.start - buffer
        hi = silence.end + buffer
        mask &= ~((timestamps >= lo) & (timestamps <= hi))
    return mask
