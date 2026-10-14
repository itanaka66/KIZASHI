import numpy as np
import pytest

from kizashi.sources.prometheus import MAX_POINTS_PER_QUERY, PrometheusClient, PrometheusError
from tests.fakes import FakePrometheus

LABELS = {"__name__": "rx", "instance": "rt01", "ifName": "Te0/1"}


def client_for(fake):
    return PrometheusClient("http://prom", transport=fake.transport)


def test_query_range_parses_matrix():
    ts = np.arange(0, 600, 60.0)
    vals = np.arange(len(ts), dtype=float)
    vals[3] = np.nan
    fake = FakePrometheus({"rx": [(LABELS, ts, vals)]})
    [s] = client_for(fake).query_range("rx", 0, 540, 60)
    assert s.labels == LABELS
    np.testing.assert_array_equal(s.timestamps, ts)
    assert np.isnan(s.values[3]) and s.values[4] == 4


def test_long_ranges_are_chunked_without_duplicates():
    step = 60.0
    n = MAX_POINTS_PER_QUERY * 2 + 500
    ts = np.arange(n) * step
    fake = FakePrometheus({"rx": [(LABELS, ts, ts.copy())]})
    [s] = client_for(fake).query_range("rx", 0, ts[-1], step)
    assert len(fake.requests) == 3
    assert len(s) == n
    assert np.all(np.diff(s.timestamps) == step)


def test_api_error():
    fake = FakePrometheus({})
    with pytest.raises(PrometheusError, match="bad_data"):
        client_for(fake).query_range("nope", 0, 60, 60)


def test_http_error_without_json():
    fake = FakePrometheus({"rx": []})
    fake.fail = True
    with pytest.raises(PrometheusError, match="503"):
        client_for(fake).query_range("rx", 0, 60, 60)
