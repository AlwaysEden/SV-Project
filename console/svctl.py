import argparse
import os
from datetime import datetime

import requests
import time
BACKEND_URL = os.getenv("SVCTL_URL", "http://127.0.0.1:8000")
TIMEOUT_SEC = 5


def print_table(rows):
    """GET 응답(list[dict] 또는 dict 한 건)을 표로 출력한다."""
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        print("(없음)")
        return

    headers = list(rows[0].keys())
    cells = []
    for row in rows:
        line = []
        for h in headers:
            value = row.get(h)
            if value is None:
                line.append("")
            else:
                line.append(str(value))
        cells.append(line)

    widths = []
    for i in range(len(headers)):
        width = len(headers[i])
        for line in cells:
            if len(line[i]) > width:
                width = len(line[i])
        widths.append(width)

    header_line = ""
    sep_line = ""
    for i in range(len(headers)):
        if i > 0:
            header_line += "  "
            sep_line += "  "
        header_line += headers[i].ljust(widths[i])
        sep_line += "-" * widths[i]
    print(header_line)
    print(sep_line)
    for line in cells:
        row_line = ""
        for i in range(len(line)):
            if i > 0:
                row_line += "  "
            row_line += line[i].ljust(widths[i])
        print(row_line)


def read_json(response):
    """백엔드 응답을 JSON으로 돌려준다. 실패 응답은 이유를 찍고 종료한다."""
    if not 200 <= response.status_code < 300:
        try:
            detail = response.json().get("detail", response.text)# JSON 이 아닌 본문이 올 수 있기에 이를 대비하기 위함
        except ValueError: 
            detail = response.text
        raise SystemExit(f"요청 실패 ({response.status_code}): {detail}")
    return response.json()

def get_device_list():
    response = requests.get(f"{BACKEND_URL}/api/v1/devices", timeout=TIMEOUT_SEC)
    return read_json(response)

def get_device_info(device_id):
    response = requests.get(f"{BACKEND_URL}/api/v1/devices/{device_id}", timeout=TIMEOUT_SEC)
    return read_json(response)

def put_device_rule(device_id, threshold):
    response = requests.put(
        f"{BACKEND_URL}/api/v1/devices/{device_id}/rule",
        json={"threshold": threshold},
        timeout=TIMEOUT_SEC,
    )
    return read_json(response)

def get_rule_info(device_id):
    response = requests.get(f"{BACKEND_URL}/api/v1/devices/{device_id}/rule", timeout=TIMEOUT_SEC)
    return read_json(response)

def get_device_commands(device_id, limit):
    response = requests.get(
        f"{BACKEND_URL}/api/v1/devices/{device_id}/commands",
        params={"limit": limit},
        timeout=TIMEOUT_SEC,
    )
    return read_json(response)

def post_device_command(device_id, status):
    response = requests.post(
        f"{BACKEND_URL}/api/v1/devices/{device_id}/commands",
        json={"type": "SET_LED", "value": status, "source": "console"},
        timeout=TIMEOUT_SEC,
    )
    post_command = read_json(response)
    print_table(post_command)

    start = time.monotonic()
    while time.monotonic() - start < TIMEOUT_SEC:
        command_info = get_command_info(post_command["command_id"])
        if command_info["status"] in ("ACK","NACK","FAILED"):
            break
        time.sleep(1)

    return command_info

def get_command_info(command_id):
    response = requests.get(f"{BACKEND_URL}/api/v1/commands/{command_id}", timeout=TIMEOUT_SEC)
    return read_json(response)

def get_device_history(device_id, from_time, to_time, limit):
    response = requests.get(
        f"{BACKEND_URL}/api/v1/devices/{device_id}/telemetry",
        params={"from": from_time, "to": to_time, "limit": limit},
        timeout=TIMEOUT_SEC,
    )
    return read_json(response)


def parse_yyyymmddhhmm(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M")
    except ValueError:
        raise argparse.ArgumentTypeError(
            "시간은 YYYYMMDDHHMM 형식이어야 합니다. 예: 202609152000"
        ) from None

def main() -> None:
    ap = argparse.ArgumentParser(description="현장 장비 제어 콘솔")

    # 서브커맨드를 사용하는 구조로 변환합니다.
    subparsers = ap.add_subparsers(dest="svctl", required=True, help="명령어")

    # "svctl devices": 등록된 장치 목록 출력
    subparsers.add_parser("devices", help="등록된 장치 목록을 출력")

    # "svctl status {device_id}": 특정 장치 정보 출력
    sp_status = subparsers.add_parser("status", help="특정 장치의 상세 정보를 출력")
    sp_status.add_argument("device_id", help="장치 ID")

    # "svctl history {device_id} --from YYYYMMDDHHMM --to YYYYMMDDHHMM [--limit 50]"
    # 백엔드가 from/to 를 필수로 받으므로 여기서도 required 로 둔다.
    sp_history = subparsers.add_parser("history", help="특정 장치의 히스토리를 출력")
    sp_history.add_argument("device_id", help="장치 ID")
    sp_history.add_argument("--from", dest="from_time", type=parse_yyyymmddhhmm, required=True, help="시작 시간(YYYYMMDDHHMM)")
    sp_history.add_argument("--to", dest="to_time", type=parse_yyyymmddhhmm, required=True, help="종료 시간(YYYYMMDDHHMM)")
    sp_history.add_argument("--limit", type=int, default=50, help="최대 개수 (기본: 50)")

    # "svctl led {device_id} on|off": 특정 장치의 LED 상태를 설정
    sp_led = subparsers.add_parser("led", help="특정 장치의 LED 상태를 설정")
    sp_led.add_argument("device_id", help="장치 ID")
    sp_led.add_argument("state", choices=["on", "off"], help="LED 상태")

    # "svctl rule {device_id} [--threshold 30]": 특정 장치의 임계값을 설정
    sp_rule = subparsers.add_parser("rule", help="특정 장치의 임계값을 설정")
    sp_rule.add_argument("device_id", help="장치 ID")
    sp_rule.add_argument("--threshold", type=float, help="임계값")

    # "svctl commands {device_id} [--limit 20]"
    sp_commands = subparsers.add_parser("commands", help="특정 장치의 명령 목록을 출력")
    sp_commands.add_argument("device_id", help="장치 ID")
    sp_commands.add_argument("--limit", type=int, default=20, help="최대 개수 (기본: 20)")

    args = ap.parse_args()

    try:
        run(ap, args)
    except requests.RequestException as e:
        raise SystemExit(f"백엔드({BACKEND_URL})에 연결할 수 없습니다: {e}") from None


def run(ap, args) -> None:

    if args.svctl == "devices":
        print_table(get_device_list())
    elif args.svctl == "status":
        info = get_device_info(args.device_id)
        # recent_commands 는 중첩 배열이라 한 칸에 넣으면 읽을 수 없다. 표를 나눠 찍는다.
        commands = info.pop("recent_commands")
        print_table(info)
        print()
        print("최근 명령")
        print_table(commands)
    elif args.svctl == "led":
        print_table(post_device_command(args.device_id, args.state))
    elif args.svctl == "rule":
        if args.threshold is not None:
            print_table(put_device_rule(args.device_id, args.threshold))
        else:
            print_table(get_rule_info(args.device_id))
    elif args.svctl == "commands":
        print_table(get_device_commands(args.device_id, args.limit))
    elif args.svctl == "history":
        from_time = args.from_time.strftime("%Y%m%d%H%M")
        to_time = args.to_time.strftime("%Y%m%d%H%M")
        print_table(get_device_history(args.device_id, from_time, to_time, args.limit))
    else:
        ap.print_help()



if __name__ == "__main__":
    main()