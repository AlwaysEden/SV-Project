#!/usr/bin/env python3
"""현장 장비와 백엔드를 잇는 에이전트."""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import threading
import serial
import yaml
import requests

CONFIG_PATH = Path(__file__).with_name("config.yaml")
logger = logging.getLogger("svagent")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def api_ok(response: requests.Response | None) -> bool:
    """전송 실패(None)와 비2xx 응답을 한 번에 걸러낸다. 실패 로그는 send_api가 남긴다."""
    return response is not None and 200 <= response.status_code < 300


def load_config(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError as e:
        raise SystemExit("config not readable: %s" % e) from None
    except yaml.YAMLError as e:
        raise SystemExit("config is not valid YAML: %s" % e) from None

    required = (
        "device_id",
        "device_url",
        "backend_url",
        "heartbeat_interval_sec",
        "command_poll_interval_sec",
        "device_ack_timeout_sec",
        "log_level",
    )
    missing = []
    for k in required:
        if k not in cfg:
            missing.append(k)

    if missing:
        raise SystemExit("config missing keys: %s" % ", ".join(missing))
        
    return cfg


class SVAgent:
    def __init__(self, cfg: dict):
        self.device_id = cfg["device_id"]
        self.device_url = cfg["device_url"]
        self.backend_url = cfg["backend_url"].rstrip("/")
        self.heartbeat_interval = float(cfg["heartbeat_interval_sec"])
        self.command_poll_interval = float(cfg["command_poll_interval_sec"])
        self.device_ack_timeout = float(cfg["device_ack_timeout_sec"])
        self.device_alive = False
        self.ser: serial.Serial | None = None
        self.waiting_pending_queue: list[dict] = [] # 백엔드에서 대기중인 CMD를 읽어온 후 아직 장치에게 전송하지 않은 CMD 목록
        self.processing_pending_queue: list[dict] = [] # 장치에게 전송한 후 아직 장치로부터 ACK/NACK을 받지 못한 CMD 목록

    def parse_data(self, data: str) -> dict:
        """
            장치로부터 오는 메시지를 dictionary로 변환
            HELLO,device=dev-001,fw=sim-1.0를 아래 형식으로 변환
            {
                "type": "HELLO",
                "device": "dev-001",
                "fw": "sim-1.0",
            }
        """
        parts = data.split(",")  
        msg = {"type": parts[0]}

        for part in parts[1:]:
            key, sep, value = part.partition("=")
            if not sep: # 잘린 라인을 원인이 드러나는 메시지로 거절한다.
                p = repr(part)
                raise ValueError(f"missing '=' in field: {p}")
            msg[key] = value
        return msg

    def make_telemetry_line(self, data: dict) -> list[dict]:
        """
            텔레메트리 데이터를 백엔드로 전송할 형식으로 변환
        """
        return [
            {
                "seq": int(data["seq"]),
                "ts":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "temperature": float(data["t"]),
                "humidity": float(data["h"]),
            }
        ]
        
    def connect_device(self) -> None:
        try:  
            self.ser = serial.serial_for_url(self.device_url, timeout=1)
        except OSError as e: # SerialException은 OSError의 하위 클래스. 소켓 계층 오류도 함께 받는다.
            logger.warning("device_connect_failed url=%s reason=%s", self.device_url, e)
            return
        
        logger.info("device_connected url=%s", self.device_url)

    def close_device(self) -> None:
        if self.ser is None: # 이미 닫혀있으면 조용히 넘어간다. 정상 종료 경로에서도 호출된다.
            return

        try:
            self.ser.close()
        except OSError as e:
            logger.warning("device_close_failed reason=%s", e)
        self.ser = None
        logger.info("device_closed")


    def send_api(self, api: str, data: list[dict], cmd_status: str, cmd_id: str) -> requests.Response | None:
        response = None
        try:
            if api == "pending": #2초마다
                response = requests.get(f"{self.backend_url}/api/v1/devices/{self.device_id}/commands/pending")
            elif api == "data_upload": #5초마다
                response = requests.post(f"{self.backend_url}/api/v1/devices/{self.device_id}/telemetry", json={"samples": data})
            elif api == "device_heartbeat": #10초마다
                response = requests.post(f"{self.backend_url}/api/v1/devices/{self.device_id}/heartbeat", json={"device_alive": self.device_alive, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            elif api == "cmd_send": #불특정하게
                if cmd_status == "ACK" or cmd_status == "NACK":
                    response = requests.patch(f"{self.backend_url}/api/v1/commands/{cmd_id}", json={"status": cmd_status})
                elif cmd_status == "FAILED":
                    response = requests.patch(f"{self.backend_url}/api/v1/commands/{cmd_id}", json={"status": cmd_status, "reason": "device_timeout"})
            elif api == "device_registration": #에이전트 실행 시 최초 1회만 실행
                response = requests.post(f"{self.backend_url}/api/v1/devices", json={"device_id": self.device_id, "device_url": self.device_url})
        except requests.RequestException as e: # 네트워크 장애만 여기서 처리한다. 코드 버그는 호출한 쪽으로 올려보낸다.
            logger.warning("api_failed api=%s reason=%s", api, e)
            return None

        if response is None: # 어느 분기에도 걸리지 않은 호출
            logger.warning("api_unknown api=%s cmd_status=%s", api, cmd_status)
            return None

        # 전송 실패와 2xx이 아닌 응답을 여기서만 기록한다. 호출부는 도메인 사건만 남긴다.
        if not 200 <= response.status_code < 300:
            logger.warning("api_rejected api=%s status=%d ", api, response.status_code)

        return response

    def handle_data(self, data: str) -> None:
        
        if not data.startswith(("HELLO", "DATA", "ACK", "NACK")):
            return

        # 라인이 깨졌어도 장치가 응답했다는 사실은 유효하므로 장치가 살아있다고 판단한다.
        self.device_alive = True

        try:
            line = self.parse_data(data)
            samples = self.make_telemetry_line(line) if line["type"] == "DATA" else None
            cmd_id = line["id"] if line["type"] in ("ACK", "NACK") else None
        except (ValueError, KeyError) as e:
            # 필드 누락이나 잘린 라인은 프로토콜 위반.
            logger.warning("device_line_malformed line=%s reason=%r", data, e)
            return

        if samples is not None:
            response = self.send_api("data_upload", samples, None, None)
            if api_ok(response):
                logger.debug("telemetry_uploaded seq=%s", line["seq"])

        if cmd_id is not None:
            for cmd in self.processing_pending_queue:
                if cmd["command_id"] == cmd_id:
                    self.processing_pending_queue.remove(cmd)
                    break

            # 장치 응답을 받은 시점에 명령은 확정된다. 백엔드 보고 실패는 send_api가 남긴다.
            logger.info("cmd_%s cmd_id=%s", line["type"].lower(), cmd_id)
            self.send_api("cmd_send", None, line["type"], cmd_id)

    def has_command(self, command_id: str) -> bool:
        for cmd in self.waiting_pending_queue + self.processing_pending_queue:
            if cmd["command_id"] == command_id:
                return True
        return False

    def flush_cmd_queue(self) -> None:

        for cmd in list(self.waiting_pending_queue): # 순회 중 원본에서 제거하므로 복사본을 돈다
            cmd["deadline"] = time.monotonic() + self.device_ack_timeout
            self.ser.write(cmd["cmd_line"].encode("utf-8"))
            logger.info("cmd_sent_to_device cmd_id=%s line=%s", cmd["command_id"], cmd["cmd_line"].strip())
            self.processing_pending_queue.append(cmd)
            self.waiting_pending_queue.remove(cmd)

    def device_loop(self) -> None:
        '''
        장치의 데이터를 읽어오고, 장치에게 CMD를 전송하는 메인 루프
        '''
        while True:
            try:
                if self.ser is None:
                    self.connect_device()
                    if self.ser is None: # 다시 시도했을 때도 연결 실패. 다음 주기에 재시도.
                        time.sleep(1)
                        continue

                data = self.ser.readline().decode("utf-8", "replace").strip()
                if not data:
                    continue
                logger.debug("device_line %s", data)

                self.handle_data(data)
                if self.waiting_pending_queue: # 대기중인 CMD가 있으면 장치에 전송
                    self.flush_cmd_queue()

            except OSError: # 장비 연결이 끊어졌을 때 재연결을 시도하기 위한 예외처리
                logger.warning("device_connection_lost")
                self.close_device()
                time.sleep(1)
            except Exception: # 루프가 죽으면 장치와의 통신이 멈추므로 스택까지 남기고 계속 돈다.
                logger.exception("device_loop_error")
                self.close_device() # 핸들이 깨졌을 수 있으므로 재연결시킨다. 영구 실패 루프보다 낫다.
                time.sleep(1)

    def api_loop(self) -> None:
        '''
        백엔드로 보내는 API를 처리하는 루프
        '''
        heartbeat_hb = time.monotonic()
        pending_hb = time.monotonic()

        while True:
            try:
                now = time.monotonic()
                if now - heartbeat_hb >= self.heartbeat_interval: #10초마다 하트비트 전송
                    if self.device_alive:
                        response = self.send_api("device_heartbeat", None, None, None)
                        if api_ok(response):
                            self.device_alive = False # 장치가 죽었다고 가정. 다음 하트비트 전송시기까지 여전히 죽어있으면 하트비트 못보내도록.
                            logger.debug("heartbeat_sent")
                    heartbeat_hb = now

                if now - pending_hb >= self.command_poll_interval: #2초마다 대기중인 CMD 확인
                    response = self.send_api("pending", None, None, None)
                    if api_ok(response):
                        for cmd in response.json(): # 대기중인 CMD 전건이 오므로, 이미 처리중인 CMD는 건너뛴다.
                            if self.has_command(cmd["command_id"]):
                                continue
                            self.waiting_pending_queue.append({
                                "command_id": cmd["command_id"],
                                "cmd_line": f"CMD,id={cmd['command_id']},led={0 if cmd['value'] == 'off' else 1}\n",
                                "deadline": ""
                            })
                    pending_hb = now
                # FIFO로 CMD를 전송했어도, 응답은 순서대로 오지 않을 수 있기에 모든 CMD에 대해 타임아웃 검사 및 처리
                for cmd in list(self.processing_pending_queue): # 순회 중 원본에서 제거하므로 복사본을 돈다
                    deadline = cmd.get("deadline")
                    if deadline and time.monotonic() >= deadline:
                        # 타임아웃은 장치 쪽 사건이므로 백엔드 보고 성공 여부와 무관하게 남긴다.
                        logger.warning("cmd_timeout cmd_id=%s", cmd["command_id"])
                        self.processing_pending_queue.remove(cmd)
                        self.send_api("cmd_send", None, "FAILED", cmd["command_id"])

            except Exception:
                logger.exception("api_loop_error")
            time.sleep(1)

def main() -> None:
    ap = argparse.ArgumentParser(description="현장 장비 에이전트")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="설정 파일 경로")
    ap.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨 (생략하면 config.yaml의 log_level)",
    )

    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(args.log_level or cfg["log_level"])

    try:
        agent = SVAgent(cfg)
    except (KeyError, TypeError, ValueError) as e: # 타입이 맞지 않는 설정도 같은 예외로 알린다.
        raise SystemExit("config is invalid: %s" % e) from None

    agent.send_api("device_registration", None, None, None) # 에이전트 실행 시 최초 1회만 실행

    threads = [
        threading.Thread(target=agent.device_loop, name="device-loop", daemon=True),
        threading.Thread(target=agent.api_loop, name="api-loop", daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            for t in threads: # 스레드가 죽으면 기능이 조용히 멈추므로 감지하고 종료한다.
                if not t.is_alive():
                    logger.error("thread_died name=%s", t.name)
                    return
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("agent_stopped")
    finally:
        agent.close_device()

if __name__ == "__main__":
    main()
