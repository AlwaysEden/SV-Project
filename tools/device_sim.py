#!/usr/bin/env python3
"""
device_sim.py - 현장 장비(센서 노드) 시뮬레이터
STRADVISION VPT 채용 과제 제공 자료입니다. 수정하지 말고 그대로 사용하세요.


실행: python device_sim.py [--port 5555] [--ctl-port 5556] [--interval 5] [--device-id dev-001]
제어: python device_sim.py --ctl "SET t=35" (실행 중인 시뮬레이터에 제어 명령 전송)
연결: 에이전트는 pyserial의 serial.serial_for_url("socket://127.0.0.1:5555", timeout=1) 로
        실제 시리얼 포트("/dev/ttyUSB0")와 같은 방식으로 읽고 쓸 수 있습니다.
        일반 TCP 소켓으로 직접 연결해도 됩니다. 모든 메시지는 줄 단위 텍스트이며 '\n'으로 끝납니다.


장치 프로토콜 (데이터 포트, 기본 5555)
    장치 -> 에이전트 : HELLO,device=<id>,fw=<ver> 연결 직후 1회
                    DATA,seq=<int>,t=<float>,h=<float> 주기 보고
                    ACK,id=<cmd_id>,led=<0|1> 명령 성공
                    NACK,id=<cmd_id>,reason=<str> 명령 실패
    에이전트 -> 장치 : CMD,id=<cmd_id>,led=<0|1> LED 제어


제어 프로토콜 (제어 포트, 기본 5556, 평가·시나리오 재현용)
    STATUS           현재 상태 조회
    SET t=35 h=70    온도/습도를 고정값으로 설정
    AUTO             고정 해제, 랜덤 워크로 복귀
    PAUSE / RESUME   DATA 보고 중단 / 재개
    DROP [초]         데이터 연결을 끊고 지정 시간 동안 재연결 거부 (케이블 분리 흉내, 기본 10초)
"""
import argparse
import random
import socket
import sys
import threading
import time


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S") + " [sim] " + msg + "\n")
    sys.stderr.flush()


class DeviceState:
    def __init__(self, device_id, interval, reset_seq_on_connect, verbose):
        self.device_id = device_id
        self.interval = interval
        self.reset_seq_on_connect = reset_seq_on_connect
        self.verbose = verbose
        self.lock = threading.Lock()
        self.seq = 0
        self.t = 24.0
        self.h = 60.0
        self.fixed_t = False
        self.fixed_h = False
        self.led = 0
        self.paused = False
        self.drop_until = 0.0
        self.client = None

    def next_sample(self):
        with self.lock:
            if not self.fixed_t:
                self.t = round(min(60.0, max(-10.0, self.t + random.uniform(-0.3, 0.3))), 1)
            if not self.fixed_h:
                self.h = round(min(100.0, max(0.0, self.h + random.uniform(-1.0, 1.0))), 1)
            self.seq += 1
            return self.seq, self.t, self.h

    def handle_line(self, line):
        """에이전트가 보낸 한 줄을 처리하고 응답 줄을 돌려준다. 알 수 없는 줄은 무시(None)."""
        parts = line.strip().split(",")
        if not parts or parts[0] != "CMD":
            return None
        kv = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[k.strip()] = v.strip()
        cid = kv.get("id", "")
        if not cid:
            return "NACK,id=,reason=missing_id"
        if "led" in kv:
            if kv["led"] in ("0", "1"):
                with self.lock:
                    self.led = int(kv["led"])
                log("CMD id=%s led=%s -> ACK" % (cid, kv["led"]))
                return "ACK,id=%s,led=%s" % (cid, kv["led"])
            return "NACK,id=%s,reason=bad_value" % cid
        return "NACK,id=%s,reason=unknown_target" % cid

    def control(self, cmd):
        parts = cmd.split()
        if not parts:
            return "ERR empty"
        op = parts[0].upper()
        if op == "STATUS":
            with self.lock:
                return ("OK t=%s h=%s led=%d seq=%d paused=%d connected=%d fixed_t=%d fixed_h=%d"
                        % (self.t, self.h, self.led, self.seq, int(self.paused),
                           int(self.client is not None), int(self.fixed_t), int(self.fixed_h)))
        if op == "SET":
            if len(parts) < 2:
                return "ERR usage: SET t=35 h=70"
            for p in parts[1:]:
                k, _, v = p.partition("=")
                try:
                    val = round(float(v), 1)
                except ValueError:
                    return "ERR bad value %s" % p
                with self.lock:
                    if k == "t":
                        self.t, self.fixed_t = val, True
                    elif k == "h":
                        self.h, self.fixed_h = val, True
                    else:
                        return "ERR unknown key %s" % k
            return "OK"
        if op == "AUTO":
            with self.lock:
                self.fixed_t = self.fixed_h = False
            return "OK"
        if op == "PAUSE":
            self.paused = True
            return "OK"
        if op == "RESUME":
            self.paused = False
            return "OK"
        if op == "DROP":
            try:
                sec = float(parts[1]) if len(parts) > 1 else 10.0
            except ValueError:
                return "ERR bad seconds"
            self.drop_until = time.time() + sec
            with self.lock:
                conn = self.client
            if conn is not None:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            log("DROP for %gs" % sec)
            return "OK dropped for %gs" % sec
        return "ERR unknown command"


def send(state, line):
    with state.lock:
        conn = state.client
    if conn is None:
        return False
    try:
        conn.sendall((line + "\n").encode("utf-8"))
        return True
    except OSError:
        with state.lock:
            if state.client is conn:
                state.client = None
        return False


def reader(state, conn):
    buf = b""
    try:
        while True:
            chunk = conn.recv(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                resp = state.handle_line(line.decode("utf-8", "replace"))
                if resp:
                    send(state, resp)
    except OSError:
        pass
    finally:
        with state.lock:
            if state.client is conn:
                state.client = None
        try:
            conn.close()
        except OSError:
            pass
        log("agent disconnected")


def emitter(state):
    while True:
        time.sleep(state.interval)
        if state.paused:
            continue
        seq, t, h = state.next_sample()
        line = "DATA,seq=%d,t=%s,h=%s" % (seq, t, h)
        sent = send(state, line)
        if state.verbose:
            log(("sent " if sent else "no agent, dropped ") + line)


def data_server(state, host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    log("device %s data port %s:%d, interval %gs" % (state.device_id, host, port, state.interval))
    while True:
        conn, addr = srv.accept()
        if time.time() < state.drop_until:
            conn.close()
            continue
        with state.lock:
            if state.client is not None:
                busy = True
            else:
                busy = False
                state.client = conn
                if state.reset_seq_on_connect:
                    state.seq = 0
        if busy:
            log("rejected extra connection from %s:%d (port busy)" % addr)
            conn.close()
            continue
        log("agent connected from %s:%d" % addr)
        threading.Thread(target=reader, args=(state, conn), daemon=True).start()
        send(state, "HELLO,device=%s,fw=sim-1.0" % state.device_id)


def ctl_handle(state, conn):
    try:
        conn.settimeout(5)
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(1024)
            if not chunk:
                break
            data += chunk
        cmd = data.decode("utf-8", "replace").strip()
        resp = state.control(cmd)
        log("ctl %r -> %s" % (cmd, resp))
        conn.sendall((resp + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        conn.close()


def ctl_server(state, host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    log("control port %s:%d" % (host, port))
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=ctl_handle, args=(state, conn), daemon=True).start()


def ctl_client(host, port, cmd):
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall((cmd + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"): #프로토콜 상으로 응답은 1줄임을 보장했기에 \n 을 기준으로 끊어줌
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        print(data.decode("utf-8", "replace").strip())


def main():
    ap = argparse.ArgumentParser(description="현장 장비(센서 노드) 시뮬레이터")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555, help="데이터 포트 (기본 5555)")
    ap.add_argument("--ctl-port", type=int, default=5556, help="제어 포트 (기본 5556)")
    ap.add_argument("--interval", type=float, default=5.0, help="DATA 보고 주기(초), 기본 5")
    ap.add_argument("--device-id", default="dev-001")
    ap.add_argument("--reset-seq-on-connect", action="store_true",
                    help="연결될 때마다 seq를 0부터 다시 시작 (장치 재부팅 흉내)")
    ap.add_argument("--verbose", action="store_true", help="DATA 전송도 로그에 남김")
    ap.add_argument("--ctl", metavar="CMD", help='제어 명령 전송 후 종료. 예: --ctl "SET t=35"')

    args = ap.parse_args()

    # 검증 시나리오 재현을 위해 ctl 인자가 있으면 제어 명령 전송 후 종료
    if args.ctl:
        ctl_client(args.host, args.ctl_port, args.ctl)
        return

    state = DeviceState(args.device_id, args.interval, args.reset_seq_on_connect, args.verbose)
    # CONTROL 포트 서버 시작
    threading.Thread(target=ctl_server, args=(state, args.host, args.ctl_port), daemon=True).start()
    # DATA 보고 스레드 시작
    threading.Thread(target=emitter, args=(state,), daemon=True).start()
    try:
        # DATA 포트 서버 시작
        data_server(state, args.host, args.port)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
