#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот сповіщень про повітряні загрози для Telegram-каналу (v2).

Логіка зон: повідомлення тільки коли об'єкт входить у коло навколо міста:
  🟠 зона області (ZONE_OBLAST_KM, за замовч. 200 км)
  🔴 зона району  (ZONE_DISTRICT_KM, за замовч. 60 км)
  🚨 над містом   (ZONE_CITY_KM, за замовч. 20 км)
Кожна зона спрацьовує один раз на об'єкт. Одне зведене повідомлення за запуск.

Режими:
  python bot.py            # одна перевірка (cron / GitHub Actions)
  python bot.py --test     # тестове повідомлення
  python bot.py --now      # поточна обстановка (завжди надсилає)

Змінні середовища:
  BOT_TOKEN, CHANNEL_ID — обов'язкові
  WATCH_LAT, WATCH_LON, WATCH_NAME — точка стеження (за замовч.: Південноукраїнськ)
  ZONE_OBLAST_KM, ZONE_DISTRICT_KM, ZONE_CITY_KM — радіуси зон
  ALERT_DELAY_SEC — пауза між повідомленнями (за замовч. 1.3)
  ALERTS_TOKEN, OBLAST_UID — (опційно) статус тривоги по області
"""

import argparse
import json
import math
import os
import re
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


def _env_float(name, default):
    v = (os.environ.get(name) or "").strip()
    return float(v) if v else default


def _env_int(name, default):
    v = (os.environ.get(name) or "").strip()
    return int(v) if v else default


WATCH_LAT = _env_float("WATCH_LAT", 47.82)
WATCH_LON = _env_float("WATCH_LON", 31.18)
WATCH_NAME = (os.environ.get("WATCH_NAME") or "").strip() or "Південноукраїнська"
ZONE_OBLAST = _env_float("ZONE_OBLAST_KM", 200)
ZONE_DISTRICT = _env_float("ZONE_DISTRICT_KM", 60)
ZONE_CITY = _env_float("ZONE_CITY_KM", 20)
DELAY = _env_float("ALERT_DELAY_SEC", 1.3)

ALERTS_TOKEN = os.environ.get("ALERTS_TOKEN", "")
OBLAST_UID = os.environ.get("OBLAST_UID", "")

KIND_NAMES = {
    "drone_piston": "🛩 БпЛА",
    "drone_jet": "🛩 Реактивний БпЛА",
    "drone_fpv": "🛩 FPV-дрон",
    "missile_cruise": "🚀 Крилата ракета",
    "missile_ballistic": "🚀 Балістика",
    "bomb": "💣 КАБ",
}

# зони від найближчої до найвіддаленішої
ZONES = [(ZONE_CITY, "🚨", "НАД МІСТОМ"), (ZONE_DISTRICT, "🔴", "Зона району"),
         (ZONE_OBLAST, "🟠", "Зона області")]


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
    r = requests.get(API_URL, headers={"User-Agent": "tryvoga-bot/2.0"}, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_oblast_status():
    if not (ALERTS_TOKEN and OBLAST_UID):
        return None
    try:
        r = requests.get(
            f"https://api.alerts.in.ua/v2/iot/active_air_raid_alerts/{OBLAST_UID}.json",
            headers={"Authorization": f"Bearer {ALERTS_TOKEN}"}, timeout=15)
        code = r.json()[0] if r.ok else None
        return {"A": "🔴 активна", "R": "🔴 активна (червоний)", "Y": "🟠 активна (жовтий)",
                "P": "🟡 часткова", "N": "🟢 немає"}.get(code)
    except Exception:
        return None


def active_objects(data):
    return [o for o in data.get("objects", []) if o.get("status") == "active"]


def obj_dist_eta(o):
    lat = o.get("lat") or o.get("predicted_lat")
    lon = o.get("lon") or o.get("predicted_lon")
    if lat is None or lon is None:
        return None, None
    dist = hav_km(lat, lon, WATCH_LAT, WATCH_LON)
    speed = o.get("speed_kmh") or 0
    eta = dist / speed * 60 if speed > 0 else None
    return dist, eta


def count_by_kind(objects):
    counts = {}
    for o in objects:
        k = o.get("kind", "unknown")
        counts[k] = counts.get(k, 0) + (o.get("amount") or 1)
    return counts


def fmt_counts(counts):
    if not counts:
        return "об'єкти уточнюються"
    names = {"drone_piston": "БпЛА", "drone_jet": "реактивні БпЛА", "drone_fpv": "FPV",
             "missile_cruise": "крилаті ракети", "missile_ballistic": "балістика", "bomb": "КАБ"}
    return " · ".join(f"{names.get(k, k)}: {n}" for k, n in sorted(counts.items(), key=lambda x: -x[1]))


def fmt_duration(start_ts):
    if not start_ts:
        return ""
    mins = max(0, int(time.time()) - start_ts)
    h, m = divmod(mins // 60, 60)
    return f"{h} год {m} хв" if h else f"{m} хв"


def send(text):
    """Надіслати в Telegram; при 429 — пауза і повтор; при 400 — повтор без HTML."""
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Не задано BOT_TOKEN або CHANNEL_ID")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt, payload in enumerate((
            {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True},
            {"chat_id": CHANNEL_ID, "text": re.sub(r"</?b>", "", text),
             "disable_web_page_preview": True})):
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and attempt == 0:
            time.sleep(5)
            continue
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Telegram: повтор після 429 не вдався")


def build_attack_start(data, oblast):
    objects = active_objects(data)
    counts = count_by_kind(objects)
    total = sum(counts.values())
    lines = ["🚨 <b>ПОЧАЛАСЬ АТАКА</b>", "", fmt_counts(counts), f"🎯 Всього цілей: {total}"]
    near = nearest(objects)
    if near:
        o, dist, eta = near
        eta_txt = f" (приліт ~{int(eta)} хв)" if eta else ""
        lines.append(f"📏 Найближча до {WATCH_NAME}: <b>{int(dist)} км</b>{eta_txt}")
    if oblast:
        lines.append(f"🔔 Тривога в області: {oblast}")
    lines += ["", "⏱ Оновлення — автоматично.", "⚠️ За сигналом тривоги — одразу в укриття!"]
    return "\n".join(lines)


def build_attack_end(state, oblast):
    lines = ["✅ <b>ЗАГРОЗУ ЗНЯТО</b>", "", f"Атака тривала: {fmt_duration(state.get('attack_started'))}",
             f"Що було в небі: {fmt_counts(state.get('attack_counts', {}))}"]
    if oblast:
        lines.append(f"🔔 Тривога в області: {oblast}")
    return "\n".join(lines)


def nearest(objects):
    best = None
    for o in objects:
        dist, eta = obj_dist_eta(o)
        if dist is not None and (best is None or dist < best[1]):
            best = (o, dist, eta)
    return best


def build_zone_message(objects, state):
    """Одне зведене повідомлення про об'єкти, що ЩОЛЬЗНО увійшли в нову зону."""
    zones_state = state.setdefault("zones", {})
    entered = []  # (radius, emoji, name, obj, dist, eta)
    for o in objects:
        oid = str(o.get("id"))
        dist, eta = obj_dist_eta(o)
        if dist is None:
            continue
        done = zones_state.setdefault(oid, [])
        # найближча (найменша) зона, в якій зараз перебуває об'єкт
        innermost = next((r for r, emoji, name in ZONES if dist <= r), None)
        if innermost is not None and innermost not in done:
            done.append(innermost)
            z = next(z for z in ZONES if z[0] == innermost)
            entered.append((innermost, z[1], z[2], o, dist, eta))
    # прибрати об'єкти, що зникли з радарів
    alive = {str(o.get("id")) for o in objects}
    for oid in list(zones_state):
        if oid not in alive:
            del zones_state[oid]
    if not entered:
        return None
    entered.sort(key=lambda x: x[0])
    lines = ["⚠️ <b>ЗАГРОЗА НАБЛИЖАЄТЬСЯ</b>", ""]
    cur, shown, extra = None, 0, 0
    for r, emoji, name, o, dist, eta in entered:
        if r != cur:
            cur, shown = r, 0
            lines.append(f"{emoji} <b>{name}</b> (до {int(r)} км):")
        if shown < 8:
            kind = KIND_NAMES.get(o.get("kind"), "🎯 Об'єкт")
            title = (o.get("title") or "").strip()
            n = o.get("amount") or 1
            amt = f" ×{n}" if n > 1 else ""
            eta_txt = f", приліт ~{int(eta)} хв" if eta else ""
            lines.append(f"• {kind}{amt} {title} — {int(dist)} км{eta_txt}")
            shown += 1
        else:
            extra += 1
    if extra:
        lines.append(f"…і ще {extra} об'єктів")
    lines += ["", "Перевірте ліхтарики, павербанки, воду."]
    return "\n".join(lines)


def build_now_message(data, oblast):
    attack = data.get("attack")
    objects = active_objects(data)
    lines = [f"📍 <b>Обстановка на {datetime.now(ZoneInfo('Europe/Kyiv')):%H:%M}</b>", ""]
    if not attack or attack.get("status") != "active":
        lines.append("✅ Активної атаки не зафіксовано")
    else:
        lines.append(f"🚨 <b>ЙДЕ АТАКА</b> — триває {fmt_duration(attack.get('started_at'))}")
        lines.append(fmt_counts(count_by_kind(objects)))
        near = nearest(objects)
        if near:
            o, dist, eta = near
            eta_txt = f" (приліт ~{int(eta)} хв)" if eta else ""
            lines.append(f"📏 Найближча до {WATCH_NAME}: {int(dist)} км{eta_txt}")
    if oblast:
        lines.append(f"\n🔔 Тривога в області: {oblast}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--now", action="store_true")
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
    objects = active_objects(data)
    now_id = attack.get("id") if attack and attack.get("status") == "active" else None
    msgs = []

    if now_id and state.get("attack_id") != now_id:
        msgs.append(build_attack_start(data, oblast))
        state["attack_id"] = now_id
        state["attack_started"] = attack.get("started_at")
        state["attack_counts"] = count_by_kind(objects)
    elif not now_id and state.get("attack_id"):
        msgs.append(build_attack_end(state, oblast))
        state["attack_id"] = None
        state["attack_started"] = None
        state["attack_counts"] = None

    zone_msg = build_zone_message(objects, state)
    if zone_msg:
        msgs.append(zone_msg)

    save_state(state)

    if not msgs:
        print(f"Спокійно ({datetime.now(ZoneInfo('Europe/Kyiv')):%H:%M}).")
        return
    for m in msgs:
        try:
            res = send(m)
            print("Надіслано, message_id:", res.get("result", {}).get("message_id"))
        except Exception as e:
            print(f"ПОМИЛКА відправки: {e}", file=sys.stderr)
        time.sleep(DELAY)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ПОМИЛКА: {e}", file=sys.stderr)
        sys.exit(1)
