"""Forgot-to-lock check for ดาวเหนือ, run every 15 min by GitHub Actions.

Reads the car status from the BYD cloud (read-only: no commands, no PIN) and sends a
push through ntfy when the car is switched off but unlocked or something is left open.
The iPhone forwards that push to the watch as an ordinary notification, so it works
even when iOS has closed the Zepp app.

Usage:
  python check.py          normal check
  python check.py --test   always send one push with the current status
Env: BYD_USERNAME, BYD_PASSWORD, NTFY_TOPIC (+ optional BYD_* settings for pyBYD)
"""

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

from pybyd import BydClient, BydConfig

CAR_NAME = "ดาวเหนือ"
STATE_FILE = Path(__file__).with_name("state") / "state.json"
# the problem must be seen on this many checks in a row (avoids alerts while loading the car)
CONFIRM_CHECKS = 2
# after this many failed checks in a row, say so once
FAIL_ALERT_AFTER = 4

# realtime field, Thai name, value meaning "open"
OPENINGS = [
    ("left_front_door", "ประตูหน้าซ้าย", 1),
    ("right_front_door", "ประตูหน้าขวา", 1),
    ("left_rear_door", "ประตูหลังซ้าย", 1),
    ("right_rear_door", "ประตูหลังขวา", 1),
    ("trunk_lid", "ฝาท้าย", 1),
    ("forehold", "ฝากระโปรงหน้า", 1),
    ("left_front_window", "กระจกหน้าซ้าย", 2),
    ("right_front_window", "กระจกหน้าขวา", 2),
    ("left_rear_window", "กระจกหลังซ้าย", 2),
    ("right_rear_window", "กระจกหลังขวา", 2),
    ("skylight", "ซันรูฟ", 2),
]
LOCKS = ["left_front_door_lock", "right_front_door_lock", "left_rear_door_lock", "right_rear_door_lock"]
POWER_ON = 3  # power_gear: 1 off, 3 on


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def push(title: str, message: str, priority: int = 4, tags=("car",)) -> None:
    body = json.dumps({
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": message,
        "priority": priority,
        "tags": list(tags),
    }).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


async def read_car() -> dict:
    async with BydClient(BydConfig.from_env(mqtt_enabled=False)) as client:
        await client.login()
        vehicles = await client.get_vehicles()
        if not vehicles:
            raise RuntimeError("no car on this account")
        rt = (await client.get_vehicle_realtime(vehicles[0].vin)).model_dump()
    locks = [as_int(rt.get(k)) for k in LOCKS]
    known_locks = [v for v in locks if v in (1, 2)]
    return {
        "powered_on": as_int(rt.get("power_gear")) == POWER_ON,
        "online": as_int(rt.get("online_state")) != 2,
        "locked": all(v == 2 for v in known_locks) if known_locks else None,
        "open": [name for key, name, open_value in OPENINGS if as_int(rt.get(key)) == open_value],
        "battery": rt.get("elec_percent"),
    }


def problem_text(car: dict) -> str:
    if car["powered_on"]:
        return ""
    parts = []
    if car["locked"] is False:
        parts.append("ยังไม่ได้ล็อค")
    if car["open"]:
        parts.append("เปิดอยู่: " + ", ".join(car["open"]))
    return " · ".join(parts)


def summary(car: dict) -> str:
    lock = {True: "ล็อคอยู่", False: "ไม่ได้ล็อค", None: "ไม่ทราบสถานะล็อค"}[car["locked"]]
    opened = ("เปิดอยู่: " + ", ".join(car["open"])) if car["open"] else "ปิดครบ"
    power = "ติดเครื่องอยู่" if car["powered_on"] else "ดับเครื่อง"
    batt = f"แบต {round(car['battery'])}%" if car["battery"] is not None else ""
    return " · ".join(p for p in (lock, opened, power, batt) if p)


def main() -> int:
    test = "--test" in sys.argv
    state = load_state()
    try:
        car = asyncio.run(read_car())
    except Exception as exc:  # noqa: BLE001 - any failure counts as one missed check
        state["fails"] = state.get("fails", 0) + 1
        print("check failed:", type(exc).__name__, exc)
        if state["fails"] == FAIL_ALERT_AFTER or test:
            push(CAR_NAME, f"ตัวเช็กรถติดต่อ BYD ไม่ได้ {state['fails']} ครั้งติดกัน ({type(exc).__name__})",
                 priority=3, tags=("warning",))
        save_state(state)
        return 0 if not test else 1

    state["fails"] = 0
    text = problem_text(car)
    streak = state.get("streak", 0) + 1 if text and text == state.get("problem") else (1 if text else 0)
    print("status:", summary(car), "| problem:", text or "-", "| streak:", streak)

    if test:
        push(CAR_NAME + " (ทดสอบ)", summary(car), priority=3, tags=("white_check_mark",))
    elif text and streak >= CONFIRM_CHECKS and state.get("alerted") != text:
        push(CAR_NAME, text, priority=5, tags=("warning", "car"))
        state["alerted"] = text
    if not text:
        state["alerted"] = ""
    state.update(problem=text, streak=streak)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
