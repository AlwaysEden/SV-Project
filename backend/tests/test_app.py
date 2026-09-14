from backend.app import TelemetryRequest
from datetime import datetime
import pytest
from pydantic import ValidationError

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
    """필드 누락은 prepare 호출이 아니라 모델 생성 시점에 걸린다."""
    with pytest.raises(ValidationError):
        TelemetryRequest(samples=[
            {
                "seq": 1,
                "ts": "2026-01-01T00:00:00Z",
                "temperature": 20.0,
            }
        ])


def test_telemetry_request_rejects_empty_samples() -> None:
    """빈 samples는 latest=None으로 흘러 500이 되던 입력이다."""
    with pytest.raises(ValidationError):
        TelemetryRequest(samples=[])


def test_telemetry_request_rejects_non_numeric_temperature() -> None:
    with pytest.raises(ValidationError):
        TelemetryRequest(samples=[
            {
                "seq": 1,
                "ts": "2026-01-01T00:00:00Z",
                "temperature": "hot",
                "humidity": 50.0,
            }
        ])


def test_telemetry_request_rejects_invalid_ts() -> None:
    with pytest.raises(ValidationError):
        TelemetryRequest(samples=[
            {
                "seq": 1,
                "ts": "not-a-timestamp",
                "temperature": 20.0,
                "humidity": 50.0,
            }
        ])


def test_prepare_telemetry_rows_returns_model_for_latest() -> None:
    """latest는 dict이 아니라 TelemetrySample이다. insert_telemetry/check_threshold가 속성으로 읽는다."""
    request = TelemetryRequest(samples=[
        {
            "seq": 1,
            "ts": "2026-01-01T00:00:00Z",
            "temperature": 20.0,
            "humidity": 50.0,
        }
    ])
    _, latest = request.prepare_telemetry_rows()

    assert latest.temperature == 20.0
    assert latest.humidity == 50.0