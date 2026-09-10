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
        self.last_data_at = None
        self.ser: serial.Serial | None = None
        self.cmd_queue: list[bytes] = []

        
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

    def send_heartbeat(self) -> None:
        # REST API로 백엔드로 heartbeat 전송
        pass


    def device_loop(self) -> None:
        while True:
            try:
                if self.ser is None:
                    self.connect_device()

                data = self.ser.readline().decode("utf-8", "replace").strip()
                if not data:
                    continue
                logger.info("received data: %s", data)

            except serial.SerialException:
                logger.warning("device connection lost")
                self.close_device()
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

    try:
        threading.Thread(target=agent.device_loop, daemon=True).start()

    except KeyboardInterrupt:
        logger.info("bye")
    finally:
        agent.close_device()

if __name__ == "__main__":
    main()
