"""svagent 단위 테스트.

각 테스트는 서로 독립적입니다.
에이전트가 필요하면 테스트 함수 안에서 SVAgent를 직접 만듭니다.
"""
import pytest

from agent.svagent import SVAgent, api_ok


def make_agent():
    cfg = {
        "device_id": "dev-001",
        "device_url": "socket://127.0.0.1:5555",
        "backend_url": "http://127.0.0.1:8000/",
        "heartbeat_interval_sec": 10,
        "command_poll_interval_sec": 2,
        "device_ack_timeout_sec": 3,
        "log_level": "INFO",
    }
    return SVAgent(cfg)


# --- parse_data ---

def test_parse_data_hello():
    agent = make_agent()
    result = agent.parse_data("HELLO,device=dev-001,fw=sim-1.0")
    assert result["type"] == "HELLO"
    assert result["device"] == "dev-001"
    assert result["fw"] == "sim-1.0"


def test_parse_data_telemetry():
    agent = make_agent()
    result = agent.parse_data("DATA,seq=1,t=24.1,h=60.2")
    assert result["type"] == "DATA"
    assert result["seq"] == "1"
    assert result["t"] == "24.1"
    assert result["h"] == "60.2"


def test_parse_data_ack():
    agent = make_agent()
    result = agent.parse_data("ACK,id=1,led=1")
    assert result["type"] == "ACK"
    assert result["id"] == "1"
    assert result["led"] == "1"


def test_parse_data_nack():
    agent = make_agent()
    result = agent.parse_data("NACK,id=1,reason=bad_value")
    assert result["type"] == "NACK"
    assert result["id"] == "1"
    assert result["reason"] == "bad_value"


def test_parse_data_type_only():
    agent = make_agent()
    result = agent.parse_data("HELLO")
    assert result["type"] == "HELLO"


def test_parse_data_keeps_values_as_strings():
    agent = make_agent()
    result = agent.parse_data("DATA,seq=1,t=24.1,h=60.2")
    assert result["seq"] == "1"
    assert result["t"] == "24.1"


def test_parse_data_value_containing_equals():
    # 값 안에 =가 있어도 첫 =만 구분자가 되어야 한다.
    agent = make_agent()
    result = agent.parse_data("NACK,id=1,reason=a=b")
    assert result["reason"] == "a=b"


def test_parse_data_rejects_field_without_equals():
    # =가 없는 필드는 ValueError를 낸다.
    agent = make_agent()
    with pytest.raises(ValueError):
        agent.parse_data("DATA,seq=1,t")


# --- handle_data ---

def test_handle_data_truncated_line_does_not_raise():
    # 잘린 라인은 예외로 죽지 않고 그냥 넘어간다.
    agent = make_agent()
    agent.handle_data("DATA,seq=1,t")


def test_handle_data_bad_temperature_does_not_raise():
    agent = make_agent()
    agent.handle_data("DATA,seq=1,t=hot,h=60.2")


def test_handle_data_missing_temperature_does_not_raise():
    agent = make_agent()
    agent.handle_data("DATA,seq=1,h=60.2")


def test_handle_data_ack_without_id_does_not_raise():
    agent = make_agent()
    agent.handle_data("ACK,led=1")


def test_handle_data_malformed_line_sets_device_alive():
    # 라인이 깨져도 장치가 응답한 것은 맞으니까 alive로 둔다.
    agent = make_agent()
    agent.device_alive = False
    agent.handle_data("DATA,seq=1,t")
    assert agent.device_alive is True


def test_handle_data_ignores_unknown_prefix():
    agent = make_agent()
    agent.device_alive = False
    agent.handle_data("GARBAGE,foo=bar")
    assert agent.device_alive is False


# --- api_ok ---

class FakeResponse:
    # requests.Response 대신 status_code만 있는 가짜 응답.
    def __init__(self, status_code):
        self.status_code = status_code


def test_api_ok_none_is_false():
    assert api_ok(None) is False


def test_api_ok_200_is_true():
    assert api_ok(FakeResponse(200)) is True

def test_api_ok_299_is_true():
    assert api_ok(FakeResponse(299)) is True


def test_api_ok_400_is_false():
    assert api_ok(FakeResponse(400)) is False

def test_api_ok_422_is_false():
    assert api_ok(FakeResponse(422)) is False


def test_api_ok_500_is_false():
    assert api_ok(FakeResponse(500)) is False