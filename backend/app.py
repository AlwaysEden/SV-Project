#!/usr/bin/env python3
"""에이전트(svagent.py)가 호출하는 REST API를 받는 FastAPI 백엔드."""
from __future__ import annotations

import logging
import threading
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
MONITOR_INTERVAL_SEC = 1.0

_stop_monitor = threading.Event()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.open_pool()

    _stop_monitor.clear()
    monitor = threading.Thread(target=_monitor_loop, name="heartbeat-monitor", daemon=True)
    monitor.start()
    logger.info("heartbeat_monitor_started interval=%ss", MONITOR_INTERVAL_SEC)

    try:
        yield
    finally:
        _stop_monitor.set() # 스레드가 커넥션을 놓은 뒤에 풀을 닫아야 한다.
        monitor.join(timeout=5)
        logger.info("heartbeat_monitor_stopped")
        db.close_pool()
        
app = FastAPI(title="SV Agent Backend", version="v1", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def monitor_heartbeat(conn: psycopg.Connection) -> None:
    """
        하트비트가 끊긴 장치를 OFFLINE으로 내린다.
    """
    dead_heartbeat_interval = 30

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM devices
            WHERE last_heartbeat_time IS NOT NULL
            AND status = 'ONLINE'
            AND NOW() - last_heartbeat_time > (%s * INTERVAL '1 second') -- 하트비트 간격 초과 시 죽은 장치로 판단
            """,
            (dead_heartbeat_interval,),
        )
        rows = cur.fetchall()

        for row in rows:
            cur.execute( # 이벤트 로그 저장
                """
                INSERT INTO event_logs (device_id, type)
                VALUES (%s, 'OFFLINE')
                """,
                (row["id"],),
            )

            cur.execute( # OFFLINE 상태로 변경
                """
                UPDATE devices SET status = 'OFFLINE' WHERE id = %s
                """,
                (row["id"],),
            )

            logger.info("device_offline device=%s", row["id"])


def _monitor_loop() -> None:
    """모니터 스레드 본체. 한 틱이 실패해도 다음 주기로 넘어간다."""
    while not _stop_monitor.is_set():
        try:
            with db.connection() as conn: # 틱마다 커넥션을 빌려 커밋한다.
                monitor_heartbeat(conn)
        except Exception:
            logger.exception("heartbeat_monitor_tick_failed")
        _stop_monitor.wait(MONITOR_INTERVAL_SEC)


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
        raise ValueError(f"invalid {field}: {value}") from None


class DeviceRegistration(BaseModel):
    device_id: str = Field(max_length=10)
    device_url: str = Field(max_length=50)

# 에이전트 send_api 별명: device_registration
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
        logger.info("device_re_registered device=%s url=%s", row["id"], row["url"])
    else:
        logger.info("device_registered device=%s url=%s", row["id"], row["url"])

    return {"device_id": row["id"], "device_url": row["url"]}


class HeartbeatRequest(BaseModel):
    device_alive: bool
    timestamp: str

# 에이전트 send_api 별명: device_heartbeat
@app.post("/api/v1/devices/{device_id}/heartbeat")
def device_heartbeat(
    device_id: str,
    body: HeartbeatRequest,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    # device_alive는 저장하지 않는다. 에이전트는 장치가 살아있을 때만 하트비트를 보내므로
    # last_heartbeat_time 만으로 Offline 판정이 가능하다.
    try:
        heartbeat_at = _parse_ts(body.timestamp, "timestamp")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    with conn.cursor() as cur:
        cur.execute( # OFFLINE 상태였던 장치인지 확인
            """
            SELECT status FROM devices 
            WHERE id = %s
            """,
            (device_id,),
        )
        row = cur.fetchone()

        if row is not None and row["status"] == "OFFLINE": #OFFLINE 상태 장치였다면,
            cur.execute( # ONLINE 상태로 변경
                """
                UPDATE devices SET status = 'ONLINE', last_heartbeat_time = %s WHERE id = %s
                """,
                (heartbeat_at, device_id),
            )

            try:
                cur.execute( # 이벤트 로그 저장
                    """
                    INSERT INTO event_logs (device_id, type)
                    VALUES (%s, 'ONLINE')
                    """,
                    (device_id,),
                )
            except pg_errors.Error: # 상세는 로그에만 남긴다. DB 제약 조건명이 응답으로 나가지 않게 한다.
                logger.exception("event_log_insert_failed device=%s type=ONLINE", device_id)
                raise HTTPException(status_code=500, detail="event log not saved") from None
        elif row is not None and row["status"] == "ONLINE": # ONLINE 상태 장치였다면,
            cur.execute( # 하트비트 시간 업데이트
                "UPDATE devices SET last_heartbeat_time = %s WHERE id = %s",
                (heartbeat_at, device_id),
            )
        else: # 장치가 없다면 에러
            raise HTTPException(status_code=404, detail="device not found")

    logger.debug("heartbeat device=%s alive=%s timestamp=%s", device_id, body.device_alive, body.timestamp)
    return {"status": "ok", "device_alive": body.device_alive, "timestamp": body.timestamp}


class TelemetrySample(BaseModel):
    """샘플 하나. 필드 누락과 타입 오류는 Pydantic이 422로 거절한다."""
    seq: int
    ts: datetime
    temperature: float
    humidity: float


class TelemetryRequest(BaseModel):
    samples: list[TelemetrySample] = Field(min_length=1)

    def prepare_telemetry_rows(
        self,
    ) -> tuple[list[dict], TelemetrySample | None]:

        rows = []
        latest = None
        for sample in self.samples:
            rows.append(
                {
                    "seq": sample.seq,
                    "temperature": sample.temperature,
                    "humidity": sample.humidity,
                    "ts": sample.ts,
                }
            )
            latest = sample

        return rows, latest
    
    def insert_telemetry(
        self,
        conn: psycopg.Connection,
        device_id: str,
        rows: list[dict],
        latest: TelemetrySample | None,
    ) -> int:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO telemetry (device_id, seq, temperature, humidity, ts)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (device_id, row["seq"], row["temperature"], row["humidity"], row["ts"])
                )
            if latest is not None:
                cur.execute(
                    """
                    UPDATE devices
                    SET latest_temperature = %s, latest_humidity = %s
                    WHERE id = %s
                    """,
                    (latest.temperature, latest.humidity, device_id),
                )
        return len(rows)

# 에이전트 send_api 별명: data_upload
@app.post("/api/v1/devices/{device_id}/telemetry")
def save_telemetry(
    device_id: str,
    body: TelemetryRequest,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:
    _require_device(conn, device_id)
    rows, latest = body.prepare_telemetry_rows()
    accepted = body.insert_telemetry(conn, device_id, rows, latest)
    logger.debug("telemetry_saved device=%s accepted=%d", device_id, accepted)

    check_threshold(conn, device_id, latest)
    return {"accepted": accepted}

def check_threshold(conn: psycopg.Connection, device_id: str, latest: TelemetrySample | None) -> None:
    """
        임계치 규칙을 평가해 자동 명령을 만든다.
        규칙 처리 실패가 이미 저장된 텔레메트리를 되돌리지 않도록 저장점 안에서 실행한다.
    """
    if latest is None:
        return

    last_action = None
    command = None
    try:
        # check_threshold함수의 실패가 insert_telemetry에 영향을 주지 않도록 
        # 중첩 transaction()이 SAVEPOINT를 만들어서, 여기서 실패해도 텔레메트리 INSERT는 커밋된다.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                SELECT threshold, last_action FROM rules WHERE device_id = %s
                """,
                (device_id,),
            )
            row = cur.fetchone()
            if row is not None:
            
                if latest.temperature < row["threshold"] and row["last_action"] == "ON":
                    body = CommandCreate(type="SET_LED", value="off", source="rule")
                    command = body.insert_command(conn, device_id)
                    last_action = "OFF"
                elif latest.temperature > row["threshold"] and row["last_action"] == "OFF":
                    body = CommandCreate(type="SET_LED", value="on", source="rule")
                    command = body.insert_command(conn, device_id)
                    last_action = "ON"
                else:
                    #do nothing
                    pass

                if command is not None:
                    logger.info("rule_command_created cmd_id=%s device=%s %s=%s", command["command_id"], device_id, command["type"], command["value"])

                if last_action is not None:
                    cur.execute(
                        """
                        UPDATE rules SET last_action = %s WHERE device_id = %s
                        """,
                        (last_action, device_id),
                    )
                    if cur.rowcount == 0: # 응답 코드가 아닌 내부 불변식 위반이므로 저장점만 되돌린다.
                        raise RuntimeError("last_action not updated device=%s" % device_id)
    except Exception:
        # 규칙 평가는 텔레메트리 수집의 부가 기능이다. 실패해도 수집은 성공으로 응답한다.
        logger.exception("rule_evaluation_failed device=%s", device_id)

class CommandCreate(BaseModel):
    type: str = Field(max_length=20)
    value: str = Field(max_length=10)
    source: str = Field(default="console", max_length=10)
    cmd_id: str | None = Field(default=None, max_length=20)

    def insert_command(self, conn: psycopg.Connection, device_id: str) -> None:
        cmd_id = uuid.uuid4().hex[:16]
        with conn.cursor() as cur:
            try:
                cur.execute(
                """
                INSERT INTO commands (cmd_id, device_id, source, type, value, status)
                VALUES (%s, %s, %s, %s, %s, 'PENDING')
                """,
                (cmd_id, device_id, self.source, self.type, self.value),
            )
            except pg_errors.Error:
                logger.exception("command_insert_failed device=%s type=%s", device_id, self.type)
                raise HTTPException(status_code=500, detail="command not saved") from None
        return {
            "command_id": cmd_id,
            "device_id": device_id,
            "source": self.source,
            "type": self.type,
            "value": self.value,
            "status": "PENDING",
        }

@app.post("/api/v1/devices/{device_id}/commands", status_code=201)
def create_command(
    device_id: str,
    body: CommandCreate,
    conn: psycopg.Connection = Depends(db.get_conn)
) -> dict[str, Any]:
    """콘솔이 명령을 생성한다. 에이전트는 pending 폴링으로 이 명령을 읽어간다."""
    _require_device(conn, device_id)

    command = body.insert_command(conn, device_id)
    logger.info("command_created cmd_id=%s device=%s %s=%s", command["command_id"], device_id, command["type"], command["value"])
    return command


# 에이전트 send_api 별명: pending
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

    # 2초마다 폴링되므로 대기 명령이 없는 응답은 DEBUG로 내린다.
    logger.log(
        logging.INFO if rows else logging.DEBUG,
        "pending_polled device=%s count=%d", device_id, len(rows),
    )
    return [
        {"command_id": row["cmd_id"], "type": row["type"], "value": row["value"]}
        for row in rows
    ]


class CommandUpdate(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=500)

# 에이전트 send_api 별명: cmd_send (ACK / NACK / FAILED)
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

    logger.info("command_updated cmd_id=%s status=%s reason=%s", command_id, row["status"], row["reason"])
    return {"status": row["status"], "reason": row["reason"]}

class ThresholdRule(BaseModel):
    threshold: float = Field(ge=0.0, le=100.0) # 0.0 ~ 100.0

@app.put("/api/v1/devices/{device_id}/rules")
def update_threshold_rule(
    device_id: str,
    body: ThresholdRule,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> dict[str, Any]:

    _require_device(conn, device_id)

    with conn.cursor() as cur:
        cur.execute(
            """
                INSERT INTO rules (device_id, threshold)
                VALUES (%s, %s)
                ON CONFLICT (device_id) DO UPDATE SET threshold = EXCLUDED.threshold
                RETURNING threshold
                """,
                (device_id, body.threshold),
            )
        # ON CONFLICT DO UPDATE ... RETURNING은 삽입이든 갱신이든 항상 행을 반환해야한다.
        row = cur.fetchone()

    # NUMERIC은 Decimal로 돌아오므로 float로 변환한다. 그냥 담으면 JSON에 문자열로 나간다.
    threshold = float(row["threshold"])
    logger.info("rule_updated device=%s threshold=%s", device_id, threshold)
    return {"threshold": threshold}



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
