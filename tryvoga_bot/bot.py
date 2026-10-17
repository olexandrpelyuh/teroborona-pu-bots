#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот сповіщень про повітряні загрози для Telegram-каналу.

Джерела даних:
  - mapa.ua API (без токена): активні БпЛА/ракети з координатами, курсом, швидкістю
  - (опційно) alerts.in.ua API: статус тривоги по області (потрібен токен)

Режими:
  python bot.py            # одна перевірка (cron / GitHub Actions)
  python bot.py --test     # тестове повідомлення
  python bot.py --now      # повідомлення з поточною обстановкою (завжди надсилає)

Змінні середовища:
  BOT_TOKEN, CHANNEL_ID  — обов'язкові
  WATCH_LAT, WATCH_LON   — точка, для якої рахуємо приліт (за замовч.: Південноукраїнськ)
  WATCH_NAME             — назва міста в тексті повідомлень
  WARN_ETA_MIN           — перше попередження, хв (за замовч.: 60)
  CRITICAL_ETA_MIN       — критичне попередження, хв (за замовч.: 15)
  ALERTS_TOKEN           — (опційно) токен api.alerts.in.ua
  OBLAST_UID             — (опційно) UID області для статусу тривоги
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "https://mapa.ua/api/v1/current"
STATE_FILE = Path(__file__).parent / "state.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

WATCH_LAT = float(os.environ.get("WATCH_LAT", "47.82"))   # Південноукраїнськ
WATCH_LON = float(os.environ.get("WATCH_LON", "31.18"))
WATCH_NAME = os.environ.get("WATCH_NAME", "Південноукраїнська")
WARN_ETA = int(os.environ.get("WARN_ETA_MIN", "60"))
CRITICAL_ETA = int(os.environ.get("CRITICAL_ETA_MIN", "15"))

ALERTS_TOKEN = os.environ.get("ALERTS_TOKEN", "")
OBLAST_UID = os.environ.get("OBLAST_UID", "")

KIND_NAMES = {
    "drone_piston": "🛩 БпЛА (Shahed/розвід.)",
    "drone_jet": "🛩 Реактивний БпЛА",
    "drone_fpv": "🛩 FPV-дрон",
    "missile_cruise": "🚀 Крилата ракета",
    "missile_ballistic": "🚀 Балістика",
    "bomb": "💣 КАБ",
}


def hav_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_current():
    r = requests.get(API_URL, headers={"User-Agent": "tryvoga-bot/1.0"}, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_oblast_status():
    """Статус тривоги по області (якщо задано ALERTS_TOKEN + OBLAST_UID)."""
    if not (ALERTS_TOKEN and OBLAST_UID):
        return None
    try:
        r = requests.get(
            f"https://api.alerts.in.ua/v2/iot/active_air_raid_alerts/{OBLAST_UID}.json",
            headers={"Authorization": f"Bearer {ALERTS_TOKEN}"}, timeout=15)
        code = r.json()[0] if r.ok else None
        return {"A": "🔴 активна", "R": "🔴 активна (червоний)", "Y": "🟠 активна (жовтий)",
                "P": "🟡 часткова", "N": "🟢 немає"}.get(code, None)
    except Exception:
        return None


def count_by_kind(objects):
    counts = {}
    for o in objects:
        if o.get("status") != "active":
            continue
        k = o.get("kind", "unknown")
        counts[k] = counts.get(k, 0) + (o.get("amount") or 1)
    return counts


def fmt_counts(counts):
    if not counts:
        return "об'єкти уточнюються"
    return " · ".join(f"{KIND_NAMES.get(k, k)}: {n}" for k, n in sorted(counts.items(), key=lambda x: -x[1]))


def fmt_duration(start_ts):
    if not start_ts:
        return ""
    mins = int(time.time()) - start_ts
    h, m = divmod(mins // 60, 60) if mins >= 3600 else (0, mins // 60)
    return f"{h} год {m} хв" if h else f"{m} хв"


def send(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Не задано BOT_TOKEN або CHANNEL_ID")
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30)
    r.raise_for_status()
    return r.json()


def approach_messages(objects, state):
    """Попередження про наближення до WATCH_POINT. Повертає список повідомлень."""
    msgs = []
    alerted = state.setdefault("alerted", {})
    for o in objects:
        if o.get("status") != "active":
            continue
        oid = str(o.get("id"))
        lat = o.get("lat") or o.get("predicted_lat")
        lon = o.get("lon") or o.get("predicted_lon")
        speed = o.get("speed_kmh") or 0
        if lat is None or lon is None or speed <= 0:
            continue
        dist = hav_km(lat, lon, WATCH_LAT, WATCH_LON)
        eta = dist / speed * 60  # хвилин
        done = alerted.setdefault(oid, [])
        for threshold, emoji, note in ((WARN_ETA, "⚠️", "Перевірте ліхтарики, павербанки, воду."),
                                       (CRITICAL_ETA, "🚨", "Готуйтеся до укриття!")):
            if eta <= threshold and threshold not in done:
                done.append(threshold)
                kind = KIND_NAMES.get(o.get("kind"), "🎯 Об'єкт")
                where = o.get("title") or ""
                eta_txt = f"~{int(eta)} хв" if eta >= 1 else "менше хвилини"
                msgs.append(
                    f"{emoji} <b>ЗАГРОЗА НАБЛИЖАЄТЬСЯ</b>\n\n"
                    f"{kind} {where}\n"
                    f"📏 Відстань до {WATCH_NAME}: <b>{int(dist)} км</b>\n"
                    f"⏱ Орієнтовний приліт: <b>{eta_txt}</b>\n"
                    f"💨 Швидкість: {int(speed)} км/г\n\n"
                    f"{note}")
    # прибрати зі стану об'єкти, що зникли
    alive = {str(o.get("id")) for o in objects if o.get("status") == "active"}
    for oid in list(alerted):
        if oid not in alive:
            del alerted[oid]
    return msgs


def build_now_message(data, oblast):
    attack = data.get("attack")
    objects = data.get("objects", [])
    lines = [f"📍 <b>Обстановка на {datetime.now(ZoneInfo('Europe/Kyiv')):%H:%M}</b>", ""]
    if not attack or attack.get("status") != "active":
        lines.append("✅ Активної атаки не зафіксовано")
    else:
        counts = count_by_kind(objects)
        lines.append(f"🚨 <b>ЙДЕ АТАКА</b> — триває {fmt_duration(attack.get('started_at'))}")
        lines.append(fmt_counts(counts))
        cities = sorted({o.get("to_city") for o in objects if o.get("to_city")})
        if cities:
            lines.append("🧭 Напрямки: " + ", ".join(cities))
    if oblast:
        lines.append(f"\n🔔 Повітряна тривога ({WATCH_NAME}): {oblast}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--now", action="store_true", help="надіслати поточну обстановку")
    args = ap.parse_args()

    if args.test:
        print(send("✅ Тест: бот сповіщень про загрози працює."))
        return

    data = fetch_current()
    oblast = fetch_oblast_status()
    state = load_state()

    if args.now:
        print(send(build_now_message(data, oblast)))
        return

    attack = data.get("attack")
    objects = data.get("objects", [])
    now_id = attack.get("id") if attack and attack.get("status") == "active" else None
    msgs = []

    if now_id and state.get("attack_id") != now_id:
        # НОВА АТАКА
        counts = count_by_kind(objects)
        cities = sorted({o.get("to_city") for o in objects if o.get("to_city")})
        lines = ["🚨 <b>ПОЧАЛАСЬ АТАКА</b>", "", fmt_counts(counts)]
        if cities:
            lines.append("🧭 Напрямки: " + ", ".join(cities))
        if oblast:
            lines.append(f"🔔 Тривога в області: {oblast}")
        lines.append("")
        lines.append("⏱ Оновлення — щойно з'являться нові дані.")
        lines.append("⚠️ За сигналом тривоги — одразу в укриття!")
        msgs.append("\n".join(lines))
        state["attack_id"] = now_id
        state["attack_started"] = attack.get("started_at")
        state["attack_counts"] = counts
    elif not now_id and state.get("attack_id"):
        # ЗАГРОЗУ ЗНЯТО
        dur = fmt_duration(state.get("attack_started"))
        msgs.append(
            "✅ <b>ЗАГРОЗУ ЗНЯТО</b>\n\n"
            f"Атака тривала: {dur}\n"
            f"Що було в небі: {fmt_counts(state.get('attack_counts', {}))}"
            + (f"\n🔔 Тривога в області: {oblast}" if oblast else ""))
        state["attack_id"] = None
        state["attack_started"] = None
        state["attack_counts"] = None

    msgs += approach_messages(objects, state)
    save_state(state)

    if msgs:
        for m in msgs:
            print(send(m))
    else:
        print(f"Спокійно. Активних загроз не зафіксовано ({datetime.now(ZoneInfo('Europe/Kyiv')):%H:%M}).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ПОМИЛКА: {e}", file=sys.stderr)
        sys.exit(1)
