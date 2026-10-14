import json
from urllib.parse import parse_qs

import httpx
import numpy as np


def matrix_response(series: list[tuple[dict, np.ndarray, np.ndarray]], start: float, end: float) -> dict:
    result = []
    for labels, ts, vals in series:
        mask = (ts >= start) & (ts <= end)
        if mask.any():
            result.append(
                {"metric": labels, "values": [[float(t), repr(float(v))] for t, v in zip(ts[mask], vals[mask])]}
            )
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


class FakePrometheus:
    """httpx transport serving /api/v1/query_range from in-memory series keyed by query."""

    def __init__(self, data: dict[str, list[tuple[dict, np.ndarray, np.ndarray]]]):
        self.data = data
        self.requests: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.requests.append(params)
        if self.fail:
            return httpx.Response(503, text="unavailable")
        if params["query"] not in self.data:
            body = {"status": "error", "errorType": "bad_data", "error": "unknown query"}
            return httpx.Response(400, content=json.dumps(body))
        body = matrix_response(self.data[params["query"]], float(params["start"]), float(params["end"]))
        return httpx.Response(200, content=json.dumps(body))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
