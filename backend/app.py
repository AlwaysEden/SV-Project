#!/usr/bin/env python3
"""에이전트(svagent.py)가 호출하는 REST API를 받는 FastAPI 백엔드."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("backend")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(title="SV Agent Backend", version="v1")
_lock = threading.Lock()

# device_id -> 등록/하트비트/텔레메트리
devices: dict[str, dict[str, Any]] = {}


def _require_device(device_id: str) -> dict[str, Any]:
    device = devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not registered")
    return device

class DeviceRegister(BaseModel):
    device_id: str
    device_url: str

@app.post("/api/v1/devices", status_code=201)
def register_device(body: DeviceRegister) -> dict[str, Any]:
    now = time.time()
    with _lock:
        existing = devices.get(body.device_id)
        if existing is None:
            devices[body.device_id] = {
                "device_id": body.device_id,
                "device_url": body.device_url,
                "registered_at": now,
                "last_heartbeat_at": None,
                "device_alive": False,
                "telemetry": [],
            }
            logger.info("registered %s url=%s", body.device_id, body.device_url)
        else:
            existing["device_url"] = body.device_url
            logger.info("re-registered %s url=%s", body.device_id, body.device_url)
        device = devices[body.device_id]
        result = {
            "device_id": device["device_id"],
            "device_url": device["device_url"],
        }
    return result


class HeartbeatIn(BaseModel):
    device_alive: bool

@app.post("/api/v1/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, body: HeartbeatIn) -> dict[str, Any]:
    now = time.time()
    with _lock:
        device = _require_device(device_id)
        device["last_heartbeat_at"] = now
        device["device_alive"] = body.device_alive
    logger.info("heartbeat %s alive=%s", device_id, body.device_alive)
    return {"status": "ok", "device_alive": body.device_alive}



@app.get("/api/v1/devices/{device_id}/commands/pending")
def get_pending_commands(device_id: str) -> dict[str, Any]:
    with _lock:
        device = _require_device(device_id)
        
        # test용도
        import random
        rand_val = random.randint(0, 1)
        rand_cmd_id = random.randint(1, 9999)
        if rand_val == 1:
            pending = {"command_id": rand_cmd_id, "type": "SET_LED", "value": "on"}
        else:
            pending = {}
   
    logger.info("pending %s", device_id)
    return pending

class CommandUpdate(BaseModel):
    status: str
    reason: str | None = None

@app.patch("/api/v1/commands/{command_id}")
def update_command(command_id: str, body: CommandUpdate) -> dict[str, Any]:
    logger.info("command %s updated status=%s reason=%s", command_id, body.status, body.reason)
    return {"status": "ok", "status": body.status, "reason": body.reason if body.reason else None}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
