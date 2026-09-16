# 현장 장비 모니터링·제어 시스템

- 데모 영상: `[링크를 입력하세요]`
- 제출 커밋: `[커밋 해시를 입력하세요]`

## 1. 프로젝트 개요

온·습도 센서와 LED가 있는 현장 장치를 대상으로 다음 기능을 제공하는 미니 시스템입니다.
- **장치**: 온·습도 센서 데이터를 주기적으로 보고하며, 전달받은 명령을 수행하고 응답
- **장치 에이전트**: 장치 프로토콜을 읽고 텔레메트리를 백엔드에 업로드하며, 백엔드 명령을 장치에 전달
- **백엔드**: FastAPI REST API와 PostgreSQL을 이용한 장치·측정값·명령·규칙 관리
- **운영자 콘솔**: Python CLI를 통한 상태 조회, 이력 조회, LED 제어, 임계치 설정

## 2. 저장소 구조

```text
.
├── agent/
│   ├── config.yaml          # 에이전트 설정
│   ├── svagent.py           # 장치 통신·업로드·하트비트·명령 처리
│   └── test/                # 에이전트 단위 테스트
├── backend/
│   ├── app.py               # REST API 및 규칙·장애 감지
│   ├── db.py                # PostgreSQL 연결 풀
│   ├── Dockerfile
│   └── tests/               # 백엔드 단위 테스트
├── console/
│   └── svctl.py             # 운영자 CLI
├── tools/
│   └── device_sim.py        # 제공된 장치 시뮬레이터
├── db/init.sql              # PostgreSQL 스키마 및 인덱스
├── docs/
│   ├── DESIGN.md            # 구성도와 설계 결정
│   └── seq_diagrams.md      # 주요 동작 시퀀스
├── docker-compose.yml
└── requirements.txt
```

## 3. 사전 요구 사항

- Docker 및 Docker Compose
- Python 3.8 이상
- 로컬 포트 `5432`, `5555`, `5556`, `8000` 사용 가능

## 4. 설치 및 실행
### 4.1 환경 설정

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
### 4.2 실행 순서

터미널을 4개 사용합니다.

#### 터미널 1: 백엔드와 데이터베이스(docker로 동작)

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

정상 응답:

```json
{"status":"ok"}
```

백엔드를 중지하려면 다음을 실행합니다.

```bash
docker compose down
```

데이터까지 삭제하려면 다음을 실행합니다.

```bash
docker compose down -v
```

#### 터미널 2: 장치 시뮬레이터

```bash
python tools/device_sim.py --verbose
```

기본값은 데이터 포트 `5555`, 제어 포트 `5556`, 데이터 전송 주기 5초, 장치 ID `dev-001`입니다.

#### 터미널 3: 장치 에이전트

```bash
python agent/svagent.py
```

설정 파일에서 장치 주소, 백엔드 주소, 하트비트 주기, 명령 폴링 주기를 변경할 수 있습니다.

```yaml
device_id: "dev-001"
device_url: "socket://127.0.0.1:5555"
backend_url: "http://127.0.0.1:8000"
heartbeat_interval_sec: 10
command_poll_interval_sec: 2
device_ack_timeout_sec: 3
log_level: "INFO"
```

실물 시리얼 장치를 사용하는 경우 `device_url`을 예를 들어 `/dev/ttyUSB0`으로 바꿉니다.

#### 터미널 4: 운영자 콘솔

```bash
export SVCTL_URL=http://127.0.0.1:8000
python console/svctl.py devices
```

## 5. 운영자 콘솔 사용법

```bash
# 장치 목록
python console/svctl.py devices

# 장치 상세 정보와 최근 명령 3건
python console/svctl.py status dev-001

# 측정 이력
python console/svctl.py history dev-001 \
  --from 202609160000 --to 202609162359 --limit 50

# LED 제어 및 결과 대기
python console/svctl.py led dev-001 on
python console/svctl.py led dev-001 off

# 임계치 설정 또는 조회
python console/svctl.py rule dev-001 --threshold 30
python console/svctl.py rule dev-001

# 명령 이력
python console/svctl.py commands dev-001 --limit 20
```

`SVCTL_URL`을 지정하지 않으면 `http://127.0.0.1:8000`을 사용합니다.

## 6. REST API

백엔드가 실행된 뒤 다음 문서를 확인할 수 있습니다.

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

주요 엔드포인트는 다음과 같습니다.

| Method | Endpoint | 설명 |
| --- | --- | --- |
| `POST` | `/api/v1/devices` | 장치 등록 또는 재등록 |
| `GET` | `/api/v1/devices` | 장치 목록과 상태 |
| `GET` | `/api/v1/devices/{id}` | 장치 상세, 규칙, 최근 명령 |
| `POST` | `/api/v1/devices/{id}/heartbeat` | 하트비트 보고 |
| `POST` | `/api/v1/devices/{id}/telemetry` | 온·습도 측정값 적재 |
| `GET` | `/api/v1/devices/{id}/telemetry` | 측정 이력 조회 |
| `POST` | `/api/v1/devices/{id}/commands` | LED 명령 생성 |
| `GET` | `/api/v1/devices/{id}/commands/pending` | 에이전트용 대기 명령 조회 |
| `GET` | `/api/v1/devices/{id}/commands` | 명령 이력 조회 |
| `GET` | `/api/v1/commands/{command_id}` | 명령 단건 조회 |
| `PATCH` | `/api/v1/commands/{command_id}` | `ACK`/`NACK`/`FAILED` 결과 보고 |
| `GET`, `PUT` | `/api/v1/devices/{id}/rule` | 임계치 규칙 조회·설정 |

### 명령 상태

```text
PENDING ──> ACK
        ├─> NACK
        └─> FAILED
```

`ACK`와 `NACK`는 장치의 응답을 그대로 반영하며, 3초 안에 응답이 없으면 에이전트가 `FAILED(reason=device_timeout)`를 보고합니다.

## 7. 검증 시나리오

### 시나리오 1: 정상 수집

1. 5장의 실행 순서대로 백엔드, 시뮬레이터, 에이전트를 실행합니다.
2. 10초 이내에 장치가 온라인인지 확인합니다.

```bash
python console/svctl.py devices
python console/svctl.py history dev-001 \
  --from 202609160000 --to 202609162359 --limit 50
```

장치 목록에 `ONLINE`과 최신 온·습도가 표시되고, 측정 이력이 쌓여야 합니다.

### 시나리오 2: 원격 LED 제어

```bash
python console/svctl.py led dev-001 on
python tools/device_sim.py --ctl STATUS
```

콘솔 명령이 `PENDING`에서 `ACK`로 바뀌고, 시뮬레이터 상태에 `led=1`이 표시되어야 합니다.

### 시나리오 3: 자동 임계치 제어

```bash
python console/svctl.py rule dev-001 --threshold 30
python tools/device_sim.py --ctl "SET t=35"
python console/svctl.py commands dev-001 --limit 20
```

온도가 임계치를 초과하면 `source=rule`인 LED ON 명령이 한 번 생성됩니다. 온도가 계속 35℃여도 동일한 명령이 반복 생성되지 않아야 합니다.

```bash
python tools/device_sim.py --ctl "SET t=20"
python console/svctl.py commands dev-001 --limit 20
```

온도가 임계치 이하로 내려가면 LED OFF 명령이 한 번 생성되어야 합니다.

### 시나리오 4: OFFLINE 감지 및 복구

1. 에이전트 터미널에서 `Ctrl+C`를 누릅니다.
2. 30초 이상 기다린 뒤 장치 상태를 조회합니다.

```bash
python console/svctl.py devices
```

장치가 `OFFLINE`으로 표시되어야 합니다. 에이전트를 다시 실행하면 하트비트 이후 `ONLINE`으로 복구됩니다.

### 시나리오 5: 장치 연결 끊김 내성

```bash
python tools/device_sim.py --ctl "DROP 15"
```

에이전트가 종료되지 않고 재연결 로그를 남겨야 하며, 약 15초 뒤 장치 연결 및 데이터 수집을 재개해야 합니다. 연결 중 발생한 데이터 유실은 1단계 허용 범위입니다.

## 8. 테스트

프로젝트 루트에서 실행합니다.

```bash
source .venv/bin/activate
pytest -q
```

테스트는 장치 프로토콜 파싱, 잘못된 입력 처리, API 상태 판정, 텔레메트리 요청 모델을 검증합니다.

## 9. 구현 범위 및 미완성 항목

### 구현한 1단계 항목

- YAML 기반 에이전트 설정
- 장치 등록 및 재등록
- `HELLO`, `DATA`, `ACK`, `NACK` 프로토콜 처리
- 텔레메트리 적재 및 최근 값 조회
- 10초 주기 하트비트
- 2초 주기 명령 폴링
- 장치 명령 ACK/NACK 및 3초 타임아웃 처리
- 장치·백엔드 연결 실패 시 로그 기록 및 재연결 시도
- 장치 I/O와 백엔드 주기 작업의 스레드 분리
- 온도 임계치 기반 LED 자동 제어 및 연속 명령 중복 방지
- 30초 기준 ONLINE/OFFLINE 전환 및 이벤트 로그
- REST API, OpenAPI 문서, CLI 오류 메시지

### 현재 구현과 과제 예시의 차이

- 콘솔의 `history`는 과제 예시의 `--last 1h` 대신 현재 `--from YYYYMMDDHHMM --to YYYYMMDDHHMM` 형식을 사용합니다.
- `svctl`의 백엔드 주소는 현재 `SVCTL_URL` 환경변수로 설정합니다. 과제 예시의 전역 `--url` 옵션은 아직 지원하지 않습니다.
- 콘솔 LED 명령 대기 시간은 현재 5초입니다.
- 텔레메트리는 `(device_id, seq)` 멱등 제약 및 로컬 버퍼 재전송을 제공하지 않습니다.

## 10. 선택한 심화 과제

현재 선택한 심화 과제: **없음**

1단계 필수 요구사항의 재현성과 안정성 검증을 우선했습니다. 향후 확장 후보는 다음과 같습니다.

- A: 텔레메트리 로컬 버퍼 및 재전송·멱등성
- B: 히스테리시스와 지속 시간 조건을 포함한 규칙 엔진
- C: 명령 `SENT`/`TIMEOUT` 및 재시도 정책

## 11. 설계 문서

- [설계 결정 및 구성도](docs/DESIGN.md)
- [주요 시퀀스 다이어그램](docs/seq_diagrams.md)

## 12. AI 도구 및 오픈소스 사용

### AI 도구 사용 범위
- 에이전트의 config 세팅
- 표준 로깅 모듈 세팅
- docker-compose.yml, Dockerfile 초안 작성
- docs 문서 mermaid로 다이어그램 그리기
- Postgres 접근규약(backend/db.py) 작성
- API문서 작성

### 주요 라이브러리

- FastAPI, Uvicorn: REST API 서버
- psycopg: PostgreSQL 연결
- pyserial: 시리얼/TCP 장치 통신
- PyYAML: 에이전트 YAML 설정 파싱
- requests: 에이전트·콘솔의 HTTP 호출
- pytest: 단위 테스트

각 라이브러리의 라이선스와 저작권은 해당 프로젝트의 배포 조건을 따릅니다.
