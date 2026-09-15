#!/usr/bin/env python3
"""에이전트(svagent.py)가 호출하는 REST API를 받는 FastAPI 백엔드."""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
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

OPENAPI_TAGS = [
    {"name": "health", "description": "프로세스 생존 확인"},
    {"name": "devices", "description": "장치 등록, 목록, 상세"},
    {"name": "heartbeat", "description": "장치 생존 하트비트"},
    {"name": "telemetry", "description": "센서 샘플 업로드와 조회"},
    {"name": "commands", "description": "명령 생성, 폴링, 상태 갱신"},
    {"name": "rules", "description": "온도 임계치 규칙"},
]

_stop_monitor = threading.Event()


class ErrorMessage(BaseModel):
    detail: str


NOT_FOUND = {404: {"model": ErrorMessage, "description": "대상을 찾지 못함"}}
BAD_REQUEST = {400: {"model": ErrorMessage, "description": "요청 값이 올바르지 않음"}}
BAD_REQUEST_OR_NOT_FOUND = {
    400: {"model": ErrorMessage, "description": "요청 값이 올바르지 않음"},
    404: {"model": ErrorMessage, "description": "대상을 찾지 못함"},
}


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


app = FastAPI(
    title="SV Agent Backend",
    version="v1",
    description="에이전트와 콘솔이 호출하는 REST API. Swagger UI는 /docs, ReDoc은 /redoc, 스키마는 /openapi.json.",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)


class HealthOut(BaseModel):
    status: str


@app.get("/health", tags=["health"], summary="프로세스 생존 확인")
def health() -> HealthOut:
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


def utc_to_datetime(value: str, field: str) -> datetime:
    """
        UTC를 비교/저장이 가능한 datetime으로 변환.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid {field}: {value}") from None


def yyyymmddhhmm_to_utc(value: str, field: str) -> datetime: 
    """
        YYYYMMDDHHMM을 UTC datetime으로 변환.
    """
    try:
        return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {field}: expected YYYYMMDDHHMM, got {value}",
        ) from None


def _as_float(value: Any) -> float | None:
    """temperature, humidity, threashold는 float으로 변환해줘야하는데, 값이 없는 경우도 있기 때문에 이를 처리하기 위함."""
    if value is None:
        return None
    return float(value)


class DeviceRegistration(BaseModel):
    device_id: str = Field(max_length=10, description="장치 ID")
    device_url: str = Field(max_length=50, description="에이전트가 장치에 붙는 주소")


class DeviceOut(BaseModel):
    device_id: str
    device_url: str

# 에이전트 send_api 별명: device_registration
@app.post(
    "/api/v1/devices",
    status_code=201,
    tags=["devices"],
    summary="장치 등록",
)
def register_device(
    body: DeviceRegistration,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> DeviceOut:
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
    device_alive: bool = Field(description="장치가 살아있는지 여부")
    timestamp: str = Field(description="에이전트 UTC 시각")


class HeartbeatOut(BaseModel):
    status: str
    device_alive: bool
    timestamp: str

# 에이전트 send_api 별명: device_heartbeat
@app.post(
    "/api/v1/devices/{device_id}/heartbeat",
    tags=["heartbeat"],
    summary="하트비트 보고",
    responses=BAD_REQUEST_OR_NOT_FOUND,
)
def device_heartbeat(
    device_id: str,
    body: HeartbeatRequest,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> HeartbeatOut:
    # device_alive는 저장하지 않는다. 에이전트는 장치가 살아있을 때만 하트비트를 보내므로
    # last_heartbeat_time 만으로 Offline 판정이 가능하다.
    try:
        heartbeat_at = utc_to_datetime(body.timestamp, "timestamp")
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
    seq: int = Field(description="장치 샘플 일련번호")
    ts: datetime = Field(description="샘플 시각")
    temperature: float = Field(description="온도")
    humidity: float = Field(description="습도")


class TelemetryRequest(BaseModel):
    samples: list[TelemetrySample] = Field(min_length=1, description="업로드할 샘플. 1건 이상")

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


class TelemetryAcceptOut(BaseModel):
    accepted: int


# 에이전트 send_api 별명: data_upload
@app.post(
    "/api/v1/devices/{device_id}/telemetry",
    tags=["telemetry"],
    summary="텔레메트리 업로드",
    responses=NOT_FOUND,
)
def save_telemetry(
    device_id: str,
    body: TelemetryRequest,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> TelemetryAcceptOut:
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
    type: str = Field(max_length=20, description="명령 종류. 예: SET_LED")
    value: str = Field(max_length=10, description="명령 값. 예: on, off")
    source: str = Field(default="console", max_length=10, description="명령 출처. console 또는 rule")
    cmd_id: str | None = Field(
        default=None,
        max_length=20,
        description="클라이언트가 넣는 ID. 서버는 무시하고 새로 발급한다",
    )

    def insert_command(self, conn: psycopg.Connection, device_id: str) -> dict[str, Any]:
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


class CommandOut(BaseModel):
    command_id: str
    device_id: str
    source: str
    type: str
    value: str
    status: str


@app.post(
    "/api/v1/devices/{device_id}/commands",
    status_code=201,
    tags=["commands"],
    summary="명령 생성",
    responses=NOT_FOUND,
)
def create_command(
    device_id: str,
    body: CommandCreate,
    conn: psycopg.Connection = Depends(db.get_conn)
) -> CommandOut:
    """콘솔이 명령을 생성한다. 에이전트는 pending 폴링으로 이 명령을 읽어간다."""
    _require_device(conn, device_id)

    command = body.insert_command(conn, device_id)
    logger.info("command_created cmd_id=%s device=%s %s=%s", command["command_id"], device_id, command["type"], command["value"])
    return command


class CommandDetailOut(BaseModel):
    command_id: str
    device_id: str
    source: str
    type: str
    value: str
    status: str
    reason: str | None = None


@app.get(
    "/api/v1/commands/{command_id}",
    tags=["commands"],
    summary="명령 단건 조회",
    responses=NOT_FOUND,
)
def get_command(
    command_id: str,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> CommandDetailOut:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cmd_id, device_id, source, type, value, status, reason FROM commands WHERE cmd_id = %s""",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="command not found")
        return {
            "command_id": row["cmd_id"],
            "device_id": row["device_id"],
            "source": row["source"],
            "type": row["type"],
            "value": row["value"],
            "status": row["status"],
            "reason": row["reason"],
        }


class PendingCommandOut(BaseModel):
    command_id: str
    type: str
    value: str


# 에이전트 send_api 별명: pending
@app.get(
    "/api/v1/devices/{device_id}/commands/pending",
    tags=["commands"],
    summary="대기 명령 폴링",
    responses=NOT_FOUND,
)
def get_pending_commands(
    device_id: str,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> list[PendingCommandOut]:
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
    status: str = Field(description="ACK, NACK, FAILED, PENDING 중 하나")
    reason: str | None = Field(default=None, max_length=500, description="FAILED일 때 사유")


class CommandStatusOut(BaseModel):
    status: str
    reason: str | None = None

# 에이전트 send_api 별명: cmd_send (ACK / NACK / FAILED)
@app.patch(
    "/api/v1/commands/{command_id}",
    tags=["commands"],
    summary="명령 상태 갱신",
    responses=BAD_REQUEST_OR_NOT_FOUND,
)
def update_command(
    command_id: str,
    body: CommandUpdate,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> CommandStatusOut:
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
            """,
            (body.status, body.reason, command_id),
        )

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="command not found")

    logger.info("command_updated cmd_id=%s status=%s reason=%s", command_id, body.status, body.reason)
    return {"status": body.status, "reason": body.reason}

class ThresholdRule(BaseModel):
    threshold: float = Field(ge=0.0, le=100.0, description="온도 임계치 (0.0 ~ 100.0)")


class ThresholdOut(BaseModel):
    threshold: float | None = None

# 콘솔 svctl rule --threshold.
@app.put(
    "/api/v1/devices/{device_id}/rule",
    tags=["rules"],
    summary="임계치 규칙 설정",
    responses=NOT_FOUND,
)
def update_threshold_rule(
    device_id: str,
    body: ThresholdRule,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> ThresholdOut:

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

    threshold = _as_float(row["threshold"])
    logger.info("rule_updated device=%s threshold=%s", device_id, threshold)
    return {"threshold": threshold}


class RuleOut(BaseModel):
    threshold: float | None = None
    last_action: str


# 콘솔 svctl rule (조회)
@app.get(
    "/api/v1/devices/{device_id}/rule",
    tags=["rules"],
    summary="임계치 규칙 조회",
    responses=NOT_FOUND,
)
def get_threshold_rule(
    device_id: str,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> RuleOut:
    _require_device(conn, device_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT threshold, last_action FROM rules WHERE device_id = %s
            """,
            (device_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="rule not found")

    return {
        "threshold": _as_float(row["threshold"]),
        "last_action": row["last_action"],
    }


class DeviceListItem(BaseModel):
    id: str
    status: str
    latest_temperature: float | None = None
    latest_humidity: float | None = None
    last_heartbeat_time: datetime | None = None


# 콘솔 svctl devices
@app.get("/api/v1/devices", tags=["devices"], summary="장치 목록")
def get_devices_list(
    conn: psycopg.Connection = Depends(db.get_conn),
) -> list[DeviceListItem]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, latest_temperature, latest_humidity, last_heartbeat_time
            FROM devices
            ORDER BY id
            """
        )
        rows = cur.fetchall()

    return [
        {
            "id": row["id"],
            "status": row["status"],
            "latest_temperature": _as_float(row["latest_temperature"]),
            "latest_humidity": _as_float(row["latest_humidity"]),
            "last_heartbeat_time": row["last_heartbeat_time"],
        }
        for row in rows
    ]


RECENT_COMMAND_COUNT = 3


class CommandListItem(BaseModel):
    command_id: str
    source: str
    type: str
    value: str
    status: str
    reason: str | None = None
    created_at: datetime


class DeviceDetailOut(BaseModel):
    id: str
    status: str
    latest_temperature: float | None = None
    latest_humidity: float | None = None
    last_heartbeat_time: datetime | None = None
    threshold: float | None = None
    last_action: str | None = None
    recent_commands: list[CommandListItem]


# 콘솔 svctl status
@app.get(
    "/api/v1/devices/{device_id}",
    tags=["devices"],
    summary="장치 상세",
    responses=NOT_FOUND,
)
def get_device_info(
    device_id: str,
    conn: psycopg.Connection = Depends(db.get_conn),
) -> DeviceDetailOut:
    """장치 한 건과 규칙, 그리고 최근 명령 몇 건을 함께 돌려준다."""
    with conn.cursor() as cur:
        # 규칙이 없는 장치도 조회되어야 하므로 LEFT JOIN 이다.
        # 컬럼명이 겹치면 dict_row 가 하나로 합쳐버리므로 status 는 별칭을 준다.
        cur.execute(
            """
            SELECT
                d.id,
                d.status AS device_status,
                d.latest_temperature,
                d.latest_humidity,
                d.last_heartbeat_time,
                r.threshold,
                r.last_action
            FROM devices d
                LEFT JOIN rules r ON d.id = r.device_id
            WHERE d.id = %s
            """,
            (device_id,),
        )
        device = cur.fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")

        # 명령은 행 수가 다르므로 같은 SQL 에 조인하지 않고 따로 뽑는다.
        # 한 SQL 에 넣으면 텔레메트리/명령의 곱집합이 되어 LIMIT 이 의미를 잃는다.
        cur.execute(
            """
            SELECT cmd_id, source, type, value, status, reason, created_at
            FROM commands
            WHERE device_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (device_id, RECENT_COMMAND_COUNT),
        )
        command_rows = cur.fetchall()

    return {
        "id": device["id"],
        "status": device["device_status"],
        "latest_temperature": _as_float(device["latest_temperature"]),
        "latest_humidity": _as_float(device["latest_humidity"]),
        "last_heartbeat_time": device["last_heartbeat_time"],
        "threshold": _as_float(device["threshold"]),
        "last_action": device["last_action"],
        "recent_commands": [
            {
                "command_id": row["cmd_id"],
                "source": row["source"],
                "type": row["type"],
                "value": row["value"],
                "status": row["status"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in command_rows
        ],
    }

# 콘솔 svctl commands. pending 경로보다 덜 구체적이므로 그 아래에 둔다.
@app.get(
    "/api/v1/devices/{device_id}/commands",
    tags=["commands"],
    summary="명령 목록",
    responses=NOT_FOUND,
)
def list_commands(
    device_id: str,
    limit: int = Query(default=20, ge=1, le=200, description="최대 건수"),
    conn: psycopg.Connection = Depends(db.get_conn),
) -> list[CommandListItem]:
    _require_device(conn, device_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cmd_id, source, type, value, status, reason, created_at
            FROM commands
            WHERE device_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (device_id, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "command_id": row["cmd_id"],
            "source": row["source"],
            "type": row["type"],
            "value": row["value"],
            "status": row["status"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


class TelemetryPointOut(BaseModel):
    seq: int
    temperature: float | None = None
    humidity: float | None = None
    ts: datetime


# 콘솔 svctl history
@app.get(
    "/api/v1/devices/{device_id}/telemetry",
    tags=["telemetry"],
    summary="텔레메트리 조회",
    responses=BAD_REQUEST_OR_NOT_FOUND,
)
def get_telemetry(
    device_id: str,
    from_time: str = Query(..., alias="from", description="조회 시작 (YYYYMMDDHHMM, UTC)"),
    to_time: str = Query(..., alias="to", description="조회 끝 (YYYYMMDDHHMM, UTC)"),
    limit: int = Query(default=50, ge=1, le=500, description="최대 건수"),
    conn: psycopg.Connection = Depends(db.get_conn),
) -> list[TelemetryPointOut]:
    _require_device(conn, device_id)

    start = yyyymmddhhmm_to_utc(from_time, "from")
    end = yyyymmddhhmm_to_utc(to_time, "to")
    if start > end:
        # BETWEEN 은 양 끝을 포함하므로 두 값이 같은 경우는 허용한다.
        raise HTTPException(status_code=400, detail="from must not be later than to")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT seq, temperature, humidity, ts
            FROM telemetry
            WHERE device_id = %s AND ts BETWEEN %s AND %s
            ORDER BY ts DESC
            LIMIT %s
            """,
            (device_id, start, end, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "seq": row["seq"],
            "temperature": _as_float(row["temperature"]),
            "humidity": _as_float(row["humidity"]),
            "ts": row["ts"],
        }
        for row in rows
    ]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
