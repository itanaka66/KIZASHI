"""Minimal Prometheus HTTP API client."""

from __future__ import annotations

import math

import httpx
import numpy as np

from kizashi.series import Series

# Prometheus rejects range queries that would return more than 11,000 points per series.
MAX_POINTS_PER_QUERY = 11_000


class PrometheusError(RuntimeError):
    pass


class PrometheusClient:
    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        bearer_token_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {}
        if bearer_token_file:
            with open(bearer_token_file) as f:
                headers["Authorization"] = f"Bearer {f.read().strip()}"
        self._http = httpx.Client(base_url=url, timeout=timeout, headers=headers, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise PrometheusError(f"{path}: {e}") from e
        try:
            body = resp.json()
        except ValueError:
            raise PrometheusError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}") from None
        if body.get("status") != "success":
            raise PrometheusError(f"{path}: {body.get('errorType')}: {body.get('error')}")
        return body["data"]

    def query_range(self, query: str, start: float, end: float, step: float) -> list[Series]:
        """Run a range query, splitting it into chunks to stay under the per-query point limit."""
        chunk = step * (MAX_POINTS_PER_QUERY - 1)
        merged: dict[str, tuple[dict, list[np.ndarray], list[np.ndarray]]] = {}
        t = start
        while t <= end:
            chunk_end = min(t + chunk, end)
            data = self._get(
                "/api/v1/query_range",
                {"query": query, "start": t, "end": chunk_end, "step": step},
            )
            if data.get("resultType") != "matrix":
                raise PrometheusError(f"query_range: unexpected result type {data.get('resultType')}")
            for item in data["result"]:
                labels = item["metric"]
                ts, vals = _parse_values(item.get("values", []))
                key = Series(labels, ts, vals).key
                entry = merged.setdefault(key, (labels, [], []))
                entry[1].append(ts)
                entry[2].append(vals)
            # Next chunk starts one step later so boundary samples are not duplicated.
            t = chunk_end + step
        return [
            Series(labels, np.concatenate(ts), np.concatenate(vals))
            for labels, ts, vals in merged.values()
        ]


def _parse_values(values: list) -> tuple[np.ndarray, np.ndarray]:
    if not values:
        return np.empty(0), np.empty(0)
    ts = np.fromiter((float(v[0]) for v in values), dtype=float, count=len(values))
    vals = np.fromiter((_to_float(v[1]) for v in values), dtype=float, count=len(values))
    return ts, vals


def _to_float(s: str) -> float:
    # Prometheus encodes special values as "NaN", "+Inf", "-Inf"; float() handles all of them.
    try:
        return float(s)
    except ValueError:
        return math.nan
