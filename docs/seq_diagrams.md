# 시퀀스 다이어그램

참여자(객체)는 모든 그림에서 동일하다.

| 객체 | 역할 |
|---|---|
| 장치 | 시뮬레이터. HELLO / DATA / ACK / NACK 송신, CMD 수신 |
| 에이전트 | `svagent.py`. 시리얼과 백엔드 REST를 중계 |
| 백엔드 | `backend/app.py`. 등록, 하트비트, 텔레메트리, pending, 명령 상태 |
| 콘솔 | 운영자. 명령 생성, 상태 조회 (평가·제어 포함) |

---

## 1. 장치 등록

에이전트 기동 시 최초 1회 `POST /api/v1/devices`.

```mermaid
sequenceDiagram
    participant Device as 장치
    participant Agent as 에이전트
    participant Backend as 백엔드
    actor Console as 콘솔

    Agent->>Backend: POST /api/v1/devices
    Backend-->>Agent: 201 {device_id, device_url}
    Note over Backend: 콘솔이 장치관련 정보를 요청하니 장치정보를 객체로 관리하도록
```

---

## 2. 장치 연결 (HELLO)

에이전트가 데이터 포트에 붙으면 장치가 HELLO를 한 번 보낸다.

```mermaid
sequenceDiagram
    participant Device as 장치
    participant Agent as 에이전트
    participant Backend as 백엔드
    actor Console as 콘솔


    Agent->>Device: serial 연결
    Device-->>Agent: HELLO,device=...,fw=...
    Note over Agent: device_alive = True
```

---

## 3. 텔레메트리 (DATA)

장치가 5초마다 DATA를 보내고, 에이전트가 백엔드에 업로드한다.

```mermaid
sequenceDiagram
    participant Device as 장치
    participant Agent as 에이전트
    participant Backend as 백엔드
    actor Console as 콘솔

    Device->>Agent: DATA,seq=...,t=...,h=...
    Agent->>Backend: POST /api/v1/devices/{id}/telemetry
    Backend-->>Agent: 200 {accepted}
    Note over Device: 위 로직 5초마다 반복

```

---

## 4. 하트비트

에이전트가 10초마다 장치 생존을 백엔드에 보고한다. 
장치의 생존여부 판단기준: 장치가 5초마다 에이전트에게 보내는 DATA가 있을 떄 생존으로 판단. 하트비트 전송했을 때 다시 사망으로 간주하며 다음 하트비트 전송까지 DATA가 오면 생존으로 하트비트 전송, 아니라면 그대로 사망으로 판단하고 하트비트 전송X

```mermaid
sequenceDiagram
    participant Device as 장치
    participant Agent as 에이전트
    participant Backend as 백엔드
    actor Console as 콘솔

    Agent->>Backend: POST /api/v1/devices/{id}/heartbeat
    Backend-->>Agent: 200 {status, device_alive, timestamp}
```

---

## 5. 명령 전달

콘솔이 백엔드로 명령을 전달한다. 에이전트는 그 명령들을 2초 주기로 읽어 장치에게 명령현다. 
응답(ACK/NACK)대기 시간은 3초이고, 그 이상은 FAILED로 보고한다. 

```mermaid
sequenceDiagram
    participant Device as 장치
    participant Agent as 에이전트
    participant Backend as 백엔드
    actor Console as 콘솔

    Console->>Backend: 명령 생성 (LED on/off)
    Backend-->>Console: 201 {command_id, ...}
    Agent-->>Backend: 대기 명령 폴링(2초간격)
    Backend-->>Agent: 200 {commend_id, type, value}
    Agent-->>Device: CMD 요청
    Device-->>Agent: ACK/NACK 응답 or 3초간 무응답
    Agent-->>Backend: 결과보고(무응답은 FAILED로 보고)
    Backend-->>Agent: 200 {status, reason}
```

---

## 9. 장치 연결 끊김 / 재연결

시리얼이 끊기면 에이전트가 닫고 다시 붙는다.

```mermaid
sequenceDiagram
    participant Device as 장치
    participant Agent as 에이전트
    participant Backend as 백엔드
    actor Console as 콘솔

    Note over Console, Device: TODO: 메시지 채우기
    Device--xAgent: 연결 끊김
    Note over Agent: close_device, 재연결 대기
    Agent->>Device: serial 재연결
```
