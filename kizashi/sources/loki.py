"""Loki HTTP client for LogQL queries."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_ENTRIES_PER_REQUEST = 5_000
_NS_PER_SECOND = 1_000_000_000


@dataclass
class LogEntry:
    ts: float           # unix seconds (nanosecond resolution truncated)
    line: str
    labels: dict[str, str]


class LokiError(Exception):
    pass


class LokiClient:
    """Minimal Loki HTTP API client.

    Parameters
    ----------
    url : Loki base URL, e.g. ``http://loki:3100``
    timeout : request timeout in seconds
    bearer_token_file : path to a file containing a bearer token (optional)
    """

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        bearer_token_file: str | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._timeout = timeout
        self._token: str | None = None
        if bearer_token_file:
            self._token = Path(bearer_token_file).read_text().strip()

    # ---------------------------------------------------------------------- API

    def query_range(
        self,
        query: str,
        start: float,
        end: float,
        limit: int = _MAX_ENTRIES_PER_REQUEST,
        direction: str = "forward",
    ) -> list[LogEntry]:
        """Execute a LogQL query and return entries sorted by timestamp.

        Parameters
        ----------
        query : LogQL expression, e.g. ``{job="syslog"} |= "BGP"``
        start / end : unix seconds (inclusive)
        limit : maximum log entries to return (Loki hard-limits at 5000 per call)
        direction : ``"forward"`` (oldest first) or ``"backward"``
        """
        params = {
            "query": query,
            "start": str(int(start * _NS_PER_SECOND)),
            "end": str(int(end * _NS_PER_SECOND)),
            "limit": str(limit),
            "direction": direction,
        }
        result = self._get("/loki/api/v1/query_range", params)
        return _parse_streams(result)

    def label_values(
        self,
        label: str,
        start: float | None = None,
        end: float | None = None,
    ) -> list[str]:
        """Return all known values for a Loki stream label."""
        params: dict[str, str] = {}
        if start is not None:
            params["start"] = str(int(start * _NS_PER_SECOND))
        if end is not None:
            params["end"] = str(int(end * _NS_PER_SECOND))
        result = self._get(f"/loki/api/v1/label/{urllib.parse.quote(label)}/values", params)
        if result.get("status") != "success":
            raise LokiError(f"label_values returned status={result.get('status')!r}")
        return list(result.get("data", []))

    # ------------------------------------------------------------------ internal

    def _get(self, path: str, params: dict) -> dict:
        qs = urllib.parse.urlencode(params)
        url = f"{self._base}{path}?{qs}"
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise LokiError(f"Loki GET {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LokiError(f"Loki GET {path} failed: {exc}") from exc


def _parse_streams(result: dict) -> list[LogEntry]:
    if result.get("status") != "success":
        raise LokiError(f"Loki query returned status={result.get('status')!r}")
    entries: list[LogEntry] = []
    for stream in result.get("data", {}).get("result", []):
        labels = dict(stream.get("stream", {}))
        for ts_ns_str, line in stream.get("values", []):
            ts = int(ts_ns_str) / _NS_PER_SECOND
            entries.append(LogEntry(ts=ts, line=line, labels=labels))
    entries.sort(key=lambda e: e.ts)
    return entries
