"""svagent 단위 테스트: parse_data."""
from __future__ import annotations

import pytest

from agent.svagent import SVAgent

CFG = {
    "device_id": "dev-001",
    "device_url": "socket://127.0.0.1:5555",
    "backend_url": "http://127.0.0.1:8000/",
    "heartbeat_interval_sec": 10,
    "command_poll_interval_sec": 2,
    "device_ack_timeout_sec": 3,
    "log_level": "INFO",
}


@pytest.fixture
def agent() -> SVAgent:
    return SVAgent(CFG)


def test_parse_data_hello(agent: SVAgent) -> None:
    assert agent.parse_data("HELLO,device=dev-001,fw=sim-1.0") == {
        "type": "HELLO",
        "device": "dev-001",
        "fw": "sim-1.0",
    }


def test_parse_data_telemetry(agent: SVAgent) -> None:
    assert agent.parse_data("DATA,seq=1,t=24.1,h=60.2") == {
        "type": "DATA",
        "seq": "1",
        "t": "24.1",
        "h": "60.2",
    }


def test_parse_data_ack(agent: SVAgent) -> None:
    assert agent.parse_data("ACK,id=1,led=1") == {
        "type": "ACK",
        "id": "1",
        "led": "1",
    }


def test_parse_data_nack(agent: SVAgent) -> None:
    assert agent.parse_data("NACK,id=1,reason=bad_value") == {
        "type": "NACK",
        "id": "1",
        "reason": "bad_value",
    }


def test_parse_data_type_only(agent: SVAgent) -> None:
    assert agent.parse_data("HELLO") == {"type": "HELLO"}


def test_parse_data_keeps_values_as_strings(agent: SVAgent) -> None:
    parsed = agent.parse_data("DATA,seq=1,t=24.1,h=60.2")
    assert parsed["seq"] == "1"
    assert parsed["t"] == "24.1"
    assert isinstance(parsed["seq"], str)
    assert isinstance(parsed["t"], str)


def test_parse_data_value_containing_equals(agent: SVAgent) -> None:
    assert agent.parse_data("NACK,id=1,reason=a=b") == {
        "type": "NACK",
        "id": "1",
        "reason": "a=b",
    }
