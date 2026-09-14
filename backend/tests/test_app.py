from backend.app import TelemetryRequest
from datetime import datetime
import pytest

def test_prepare_telemetry_rows_two_samples() -> None:
    requests = TelemetryRequest(samples=[
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:00Z",
            "temperature": 20.0,
            "humidity": 50.0,
        },
        {
            "seq": 2,
            "ts": "2026-01-02T00:00:01Z",
            "temperature": 25.0,
            "humidity": 55.0,
        }
    ])

    rows, latest = requests.prepare_telemetry_rows()

    assert len(rows) == 2
    assert rows[0] == {
        "seq": 1,
        "ts": datetime.fromisoformat("2026-01-01T00:00:00Z"),
        "temperature": 20.0,
        "humidity": 50.0,
    }
    assert rows[1] == {
        "seq": 2,
        "ts": datetime.fromisoformat("2026-01-02T00:00:01Z"),
        "temperature": 25.0,
        "humidity": 55.0,
    }
    assert latest is requests.samples[1]

def test_prepare_telemetry_rows_one_sample() -> None:
    request = TelemetryRequest(samples=[
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:00Z",
            "temperature": 20.0,
            "humidity": 50.0,
        }
    ])
    rows, latest = request.prepare_telemetry_rows()

    assert len(rows) == 1
    assert rows[0] == {
        "seq": 1,
        "ts": datetime.fromisoformat("2026-01-01T00:00:00Z"),
        "temperature": 20.0,
        "humidity": 50.0,
    }
    assert latest is request.samples[0]


def test_prepare_telemetry_rows_missing_keys() -> None:
    request = TelemetryRequest(samples=[
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:00Z",
            "temperature": 20.0,
        }
    ])
    with pytest.raises(ValueError):
        request.prepare_telemetry_rows()