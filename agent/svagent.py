#!/usr/bin/env python3
"""현장 장비와 백엔드를 잇는 에이전트."""
from __future__ import annotations

import argparse
import logging
import time
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


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

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
        parts = data.split(",")  # HELLO,device=dev-001,fw=sim-1.0 형식으로 분리
        msg = {"type": parts[0]}

        for part in parts[1:]:
            key, value = part.split("=", 1)
            msg[key] = value
        return msg

        
    def connect_device(self) -> None:
        try:  
            self.ser = serial.serial_for_url(self.device_url, timeout=1)
        except serial.SerialException as e:
            logger.warning("failed to connect device: %s", e)
            return
        
        logger.info("device connected")

    def close_device(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except serial.SerialException:
                logger.warning("failed to close device")
            self.ser = None
            logger.info("device closed")
        else:
            logger.warning("device is not connected")

    def send_api(self, api: str, data: str, cmd_status: str, cmd_id: str) -> requests.Response:
        response = None
        try:
            if api == "pending": #2초마다
                response = requests.get(f"{self.backend_url}/api/v1/devices/{self.device_id}/commands/pending")
            elif api == "data_upload": #5초마다
                response = requests.post(f"{self.backend_url}/api/v1/devices/{self.device_id}/telemetry", json={"data": data})
            elif api == "device_heartbeat": #10초마다
                response = requests.post(f"{self.backend_url}/api/v1/devices/{self.device_id}/heartbeat", json={"device_alive": self.device_alive, "timestamp": time.time()})
            elif api == "cmd_send": #불특정하게
                if cmd_status == "ACK" or cmd_status == "NACK":
                    response = requests.patch(f"{self.backend_url}/api/v1/commands/{cmd_id}", json={"status": cmd_status})
                elif cmd_status == "FAILED":
                    response = requests.patch(f"{self.backend_url}/api/v1/commands/{cmd_id}", json={"status": cmd_status, "reason": "device_timeout"})
            elif api == "device_registration": #에이전트 실행 시 최초 1회만 실행
                response = requests.post(f"{self.backend_url}/api/v1/devices", json={"device_id": self.device_id, "device_url": self.device_url})
        except Exception as e:
            logger.warning("Fail to send API from Agent to Backend")

        return response

    def handle_data(self, data: str) -> None:
        if data.startswith(("HELLO", "DATA", "ACK", "NACK")):
            self.device_alive = True
        if data.startswith(("ACK", "NACK")):
            line = self.parse_data(data)
            response = self.send_api("cmd_send", None, line["type"], line["id"])
            if response.status_code == 200:
                self.processing_pending_queue.remove(cmd for cmd in self.processing_pending_queue if cmd["command_id"] == line["id"])
                logger.info("ACK/NACK,%s(Agent->Backend)", line["id"])
            else:
                logger.warning("Failed to send ACK/NACK,%s(Agent->Backend)", line["id"])

    def flush_cmd_queue(self) -> None:

        for cmd in self.waiting_pending_queue:
            cmd["deadline"] = time.monotonic() + self.device_ack_timeout
            self.ser.write(cmd["cmd_line"].encode("utf-8"))
            logger.info("CMD,%s(Agent->Device): %s", cmd["command_id"], cmd["cmd_line"])
            self.processing_pending_queue.append(cmd)
            self.waiting_pending_queue.pop(0)

    def device_loop(self) -> None:
        '''
        장치의 데이터를 읽어오고, 장치에게 CMD를 전송하는 메인 루프
        '''
        while True:
            try:
                if self.ser is None:
                    self.connect_device()

                data = self.ser.readline().decode("utf-8", "replace").strip()
                if not data:
                    continue
                logger.info("received data: %s", data)

                self.handle_data(data)
                if self.waiting_pending_queue: # 대기중인 CMD가 있으면 장치에 전송
                    self.flush_cmd_queue()

            except serial.SerialException: # 장비 연결이 끊어졌을 때 재연결을 시도하기 위한 예외처리
                logger.warning("device connection lost")
                self.close_device()
                time.sleep(1)

    def api_loop(self) -> None:
        '''
        백엔드로 보내는 API를 처리하는 루프
        '''
        heartbeat_hb = time.monotonic()
        pending_hb = time.monotonic()
        cmd_queue = None

        while True:
            try:
                now = time.monotonic()
                if now - heartbeat_hb >= self.heartbeat_interval: #10초마다 하트비트 전송
                    if self.device_alive:
                        response = self.send_api("device_heartbeat", None, None, None)
                        if 200 <= response.status_code < 300:
                            self.device_alive = False # 장치가 죽었다고 가정. 다음 하트비트 전송시기까지 여전히 죽어있으면 하트비트 못보내도록.
                    heartbeat_hb = now

                if now - pending_hb >= self.command_poll_interval: #2초마다 대기중인 CMD 확인
                    response = self.send_api("pending", None, None, None)
                    if response.status_code == 200:
                        body = response.json()
                        if body:
                            cmd_queue = {
                                "command_id": body["command_id"],
                                "cmd_line": f"CMD,id={body['command_id']},led={0 if body['value'] == 'off' else 1}\n",
                                "deadline": ""
                            }
                            self.waiting_pending_queue.append(cmd_queue)
                    pending_hb = now
                for cmd in self.processing_pending_queue: # FIFO로 CMD를 전송했어도, 응답은 순서대로 오지 않을 수 있기에 모든 CMD에 대해 타임아웃 검사 및 처리
                    deadline = cmd.get("deadline")
                    if deadline and time.monotonic() >= deadline:
                        response = self.send_api("cmd_send", None, "FAILED", cmd["command_id"])
                        self.processing_pending_queue.remove(cmd)
                        if response is not None and response.status_code == 200:
                            logger.warning("CMD timeout,%s(Agent->Backend)", cmd["command_id"])

            except Exception as e:
                logger.warning("API loop error: %s", e)
            time.sleep(1)

def main() -> None:
    ap = argparse.ArgumentParser(description="현장 장비 에이전트")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="설정 파일 경로")
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨",
    )

    args = ap.parse_args()


    setup_logging(args.log_level)
    agent = SVAgent(load_config(Path(args.config)))
    agent.send_api("device_registration", None, None, None) # 에이전트 실행 시 최초 1회만 실행

    try:
        threading.Thread(target=agent.device_loop, daemon=True).start() # 장치에 관한 루프를 스레드로 실행
        threading.Thread(target=agent.api_loop, daemon=True).start() # API 루프를 스레드로 실행
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("bye")
    finally:
        agent.close_device()

if __name__ == "__main__":
    main()
