#!/usr/bin/env python3
"""에이전트(svagent.py)가 호출하는 REST API를 받는 FastAPI 백엔드."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from backend import db

logger = logging.getLogger("backend")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

COMMAND_STATUSES = ("PENDING", "ACK", "NACK", "FAILED")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.open_pool()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="SV Agent Backend", version="v1", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _require_device(conn: psycopg.Connection, device_id: str) -> None:
    """
        장치의 존재를 확인.
        DB단에서 FK설정을 하지 않았기에 어플리케이션 단에서 조회하여 장치의 존재 확인 필요.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM devices WHERE id = %s", (device_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="device not registered")


def _parse_ts(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {field}: {value}") from None


class DeviceRegistration(BaseModel):
    device_id: str = Field(max_length=10)
    device_url: str = Field(max_length=50)

@app.post("/api/v1/devices", status_code=201)
def register_device(
    body: DeviceRegistration,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM devices WHERE id = %s", (body.device_id,))
        existing = cur.fetchone() is not None

        cur.execute(
            """
            INSERT INTO devices (id, url)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET url = EXCLUDED.url
            RETURNING id, url
            """,
            (body.device_id, body.device_url),
        )
        row = cur.fetchone()

    if existing:
        logger.info("re-registered %s url=%s", row["id"], row["url"])
    else:
        logger.info("registered %s url=%s", row["id"], row["url"])

    return {"device_id": row["id"], "device_url": row["url"]}


class HeartbeatRequest(BaseModel):
    device_alive: bool
    timestamp: str

@app.post("/api/v1/devices/{device_id}/heartbeat")
def device_heartbeat(
    device_id: str,
    body: HeartbeatRequest,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    # device_alive는 저장하지 않는다. 에이전트는 장치가 살아있을 때만 하트비트를 보내므로
    # last_heartbeat_time 만으로 Offline 판정이 가능하다.
    heartbeat_at = _parse_ts(body.timestamp, "timestamp")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devices SET last_heartbeat_time = %s WHERE id = %s",
            (heartbeat_at, device_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="device not registered")

    logger.info("heartbeat %s, alive=%s, timestamp=%s", device_id, body.device_alive, body.timestamp)
    return {"status": "ok", "device_alive": body.device_alive, "timestamp": body.timestamp}


class TelemetryRequest(BaseModel):
    samples: list[dict]

@app.post("/api/v1/devices/{device_id}/telemetry")
def save_telemetry(
    device_id: str,
    body: TelemetryRequest,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    _require_device(conn, device_id)

    accepted = 0
    latest: dict[str, Any] | None = None

    with conn.cursor() as cur:
        for sample in body.samples: #현재는 DATA가 측정되자마자 1건씩 보내지만, 이후에 배치도 대응할 수 있도록 구현.
            missing = [k for k in ("seq", "ts", "temperature", "humidity") if k not in sample]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail="sample missing keys: %s" % ", ".join(missing),
                )

            cur.execute(
                """
                INSERT INTO telemetry (device_id, seq, temperature, humidity, ts)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    device_id,
                    sample["seq"],
                    sample["temperature"],
                    sample["humidity"],
                    _parse_ts(sample["ts"], "ts"),
                ),
            )
            accepted += 1
            latest = sample
            logger.info("telemetry saved id=%s seq=%s", device_id, sample["seq"])

        if latest is not None: # 콘솔 조회용 최신값 캐시. 이력은 telemetry가 담당한다.
            cur.execute(
                """
                UPDATE devices
                SET latest_temperature = %s, latest_humidity = %s
                WHERE id = %s
                """,
                (latest["temperature"], latest["humidity"], device_id),
            )

    return {"accepted": accepted}


class CommandCreate(BaseModel):
    type: str = Field(max_length=20)
    value: str = Field(max_length=10)
    source: str = Field(default="console", max_length=10)
    cmd_id: str | None = Field(default=None, max_length=20)

@app.post("/api/v1/devices/{device_id}/commands", status_code=201)
def create_command(
    device_id: str,
    body: CommandCreate,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    """콘솔이 명령을 생성한다. 에이전트는 pending 폴링으로 이 명령을 읽어간다."""
    _require_device(conn, device_id)

    cmd_id = body.cmd_id or uuid.uuid4().hex[:16]

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO commands (cmd_id, device_id, source, type, value, status)
                VALUES (%s, %s, %s, %s, %s, 'PENDING')
                """,
                (cmd_id, device_id, body.source, body.type, body.value),
            )
        except pg_errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="command id already exists") from None

    logger.info("command created %s device=%s %s=%s", cmd_id, device_id, body.type, body.value)
    return {
        "command_id": cmd_id,
        "device_id": device_id,
        "source": body.source,
        "type": body.type,
        "value": body.value,
        "status": "PENDING",
    }


@app.get("/api/v1/devices/{device_id}/commands/pending")
def get_pending_commands(
    device_id: str,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> list[dict[str, Any]]:
    """대기중인 명령 전건. 에이전트가 이미 받은 command_id는 무시한다."""
    _require_device(conn, device_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cmd_id, type, value
            FROM commands
            WHERE device_id = %s AND status = 'PENDING'
            ORDER BY created_at ASC
            """,
            (device_id,),
        )
        rows = cur.fetchall()

    logger.info("pending %s count=%d", device_id, len(rows))
    return [
        {"command_id": row["cmd_id"], "type": row["type"], "value": row["value"]}
        for row in rows
    ]


class CommandUpdate(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=500)

@app.patch("/api/v1/commands/{command_id}")
def update_command(
    command_id: str,
    body: CommandUpdate,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    if body.status not in COMMAND_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be one of: %s" % ", ".join(COMMAND_STATUSES),
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE commands
            SET status = %s, reason = %s
            WHERE cmd_id = %s
            RETURNING status, reason
            """,
            (body.status, body.reason, command_id),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="command not found")

    logger.info("command %s updated status=%s reason=%s", command_id, row["status"], row["reason"])
    return {"status": row["status"], "reason": row["reason"]}


#TODO: def monitor_heartbeat




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
