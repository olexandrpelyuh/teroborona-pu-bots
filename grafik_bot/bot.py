#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот графіків відключень ДПЕМ «Атомсервіс» (v3 — парсинг records).

Джерело: на сторінці info.dpem.mk.ua/off_graf.html у <script> вбудовано
масив records = [{date, saved_at, data[576]}], де data — 12 черг × 48 слотів
по 30 хв, значення: 0=світло, 1=перемикання, 2=відключення, 3=перемикання,
4=можливе відключлення.

Режими:
  python bot.py                      # перевірка змін (кожні 10 хв)
  python bot.py --digest "☀️ <b>ГРАФІК НА СЬОГОДНІ</b>"          # ранковий дайджест
  python bot.py --digest "🌙 <b>ГРАФІК НА ЗАВТРА</b>" --tomorrow  # вечірній дайджест
  python bot.py --learn              # діагностика: що є у records
  python bot.py --test               # тестове повідомлення

Змінні середовища: BOT_TOKEN, CHANNEL_ID (обов'язкові), WATCH_QUEUES (опційно).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = os.environ.get("GRAFIK_URL", "https://info.dpem.mk.ua/off_graf.html")
STATE_FILE = Path(__file__).parent / "state.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
WATCH_QUEUES = os.environ.get("WATCH_QUEUES", "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SLOTS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
QUEUE_LABELS = [f"{q}-{r}" for q in range(1, 7) for r in ("І", "ІІ")]
VALUE_MAP = {"0": "on", "1": "switch", "2": "off", "3": "switch", "4": "possible"}
EMOJI = {"on": "🟩", "off": "🟥", "possible": "🟧", "switch": "🟨", "unknown": "⬛"}


def _utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def _split_text(text, limit=3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if _utf16_len(cur) + _utf16_len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [text]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_records(url=None):
    """Завантажити сторінку й витягнути масив records."""
    resp = requests.get(url or URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    big = ""
    for sc in soup.find_all("script"):
        t = sc.string or sc.get_text() or ""
        if "records" in t and len(t) > len(big):
            big = t
    if not big:
        raise RuntimeError("Не знайшов records на сторінці (верстка змінилася?)")
    matches = re.findall(r"records\s*=\s*(\[.*?\])\s*;", big, re.S)
    if not matches:
        raise RuntimeError("Не вдалося розпарсити records")
    recs = json.loads(max(matches, key=len))
    recs = [r for r in recs if isinstance(r, dict) and r.get("date") and r.get("data")]
    recs.sort(key=lambda r: r["date"], reverse=True)
    if not recs:
        raise RuntimeError("records порожній")
    return recs


def kyiv_today():
    return datetime.now(ZoneInfo("Europe/Kyiv")).date()


def record_for(records, day):
    """day: 'today' | 'tomorrow' | 'latest'. Повертає (record, note)."""
    today = kyiv_today()
    if day == "latest":
        return records[0], ""
    target = today if day == "today" else today + timedelta(days=1)
    tstr = target.isoformat()
    for r in records:
        if r["date"] == tstr:
            return r, ""
    if day == "today":
        # сьогоднішнього немає → беремо найсвіжіший не пізніше сьогодні
        for r in records:
            if r["date"] <= tstr:
                return r, f"⚠️ Графіка на сьогодні ({tstr}) ще немає — показую останній доступний: {r['date']}"
        return records[0], ""
    return None, f"⚠️ Дані на {tstr} ще не надходили. Очікуємо публікацію від ДПЕМ."


def build_grid(record):
    data = [str(v) for v in record["data"]]
    if len(data) < 576:
        raise RuntimeError(f"Несподівана довжина data: {len(data)}")
    grid = {}
    for i, label in enumerate(QUEUE_LABELS):
        grid[label] = [VALUE_MAP.get(v, "unknown") for v in data[i * 48:(i + 1) * 48]]
    return grid


def date_text(record):
    txt = f"Графік відключень на: {record['date']}"
    if record.get("saved_at"):
        txt += f" (оновлено {record['saved_at']})"
    return txt


def windows(statuses, target=("off", "possible")):
    result, start = [], None
    for i, st in enumerate(list(statuses) + ["end"]):
        if st in target and start is None:
            start = i
        elif st not in target and start is not None:
            result.append(f"{SLOTS[start]}–{(i * 30) // 60 % 24:02d}:{(i * 30) % 60:02d}")
            start = None
    return result


def timeline(statuses):
    return (f"🌙 00–12  {''.join(EMOJI.get(s, '⬛') for s in statuses[:24])}\n"
            f"☀️ 12–24  {''.join(EMOJI.get(s, '⬛') for s in statuses[24:])}")


def off_hours(statuses):
    n = sum(1 for st in statuses if st in ("off", "possible"))
    h, m = divmod(n * 30, 60)
    return f"{h} год" + (f" {m} хв" if m else "")


def next_off_line(statuses, now=None):
    now = now or datetime.now(ZoneInfo("Europe/Kyiv"))
    idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    cur = statuses[idx] if idx < len(statuses) else "empty"
    if cur in ("off", "possible"):
        end = idx
        while end < len(statuses) and statuses[end] in ("off", "possible"):
            end += 1
        if end < len(statuses):
            eh, em = divmod(end * 30, 60)
            return f"⚡ <b>Зараз без світла</b> — очікується до {eh:02d}:{em:02d}"
        return "⚡ <b>Зараз без світла</b>"
    nxt = None
    for i in range(idx, len(statuses)):
        if statuses[i] in ("off", "possible"):
            nxt = i
            break
    if nxt is None:
        return "✅ До кінця доби світло без відключень"
    delta_min = nxt * 30 - (now.hour * 60 + now.minute)
    hh, mm = divmod(nxt * 30, 60)
    end = nxt
    while end < len(statuses) and statuses[end] in ("off", "possible"):
        end += 1
    eh, em = divmod(end * 30, 60)
    if delta_min <= 0:
        when = "вже скоро"
    elif delta_min < 60:
        when = f"через {delta_min} хв"
    else:
        when = f"через {delta_min // 60} год" + (f" {delta_min % 60} хв" if delta_min % 60 else "")
    return f"⏰ Наступне: <b>{hh:02d}:{mm:02d}–{eh:02d}:{em:02d}</b> ({when})"


def watched_labels(grid):
    watch = [w.strip() for w in WATCH_QUEUES.split(",") if w.strip()]
    labels = [l for l in QUEUE_LABELS if l in grid]
    return [l for l in labels if not watch or l in watch]


def queue_block(label, statuses, with_next=True):
    wins = windows(statuses)
    lines = [f"<b>Черга {label}</b>", timeline(statuses)]
    if wins:
        lines.append(f"🔴 Відключення: {', '.join(wins)} · разом {off_hours(statuses)}")
    else:
        lines.append("🟢 Без відключень ✅")
    if with_next:
        lines.append(next_off_line(statuses))
    return lines


def build_alert_message(record, grid):
    lines = ["⚡ <b>ГРАФІК ВІДКЛЮЧЕНЬ ЗМІНЕНО!</b>", "", date_text(record), ""]
    labels = watched_labels(grid)
    labels.sort(key=lambda l: 0 if windows(grid[l]) else 1)
    any_off = any(windows(grid[l]) for l in labels)
    for label in labels:
        lines += queue_block(label, grid[label])
        lines.append("")
    if not any_off:
        lines.append("✅ Відключень за графіком немає")
    return "\n".join(lines).rstrip()


def build_digest_message(title, record, grid, note=""):
    lines = [title, ""]
    if record is None:
        lines.append(note or "⚠️ Дані ще не надходили.")
        return "\n".join(lines)
    lines.append(date_text(record))
    if note:
        lines.append(note)
    lines.append("")
    labels = watched_labels(grid)
    labels.sort(key=lambda l: 0 if windows(grid[l]) else 1)
    for label in labels:
        lines += queue_block(label, grid[label])
        lines.append("")
    lines.append("🟩 світло · 🟥 відключення · 🟧 можливе · 🟨 перемикання")
    lines.append("⏳ На перемикання черг — до 30 хвилин")
    return "\n".join(lines).rstrip()


def send_telegram(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Не задано BOT_TOKEN або CHANNEL_ID")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    result = None
    for part in _split_text(text):
        for attempt, payload in enumerate((
                {"chat_id": CHANNEL_ID, "text": part, "parse_mode": "HTML",
                 "disable_web_page_preview": True},
                {"chat_id": CHANNEL_ID, "text": re.sub(r"</?b>", "", part),
                 "disable_web_page_preview": True})):
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 200:
                result = r.json()
                break
            if r.status_code == 400 and attempt == 0:
                continue
            raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")
        else:
            raise RuntimeError("Telegram: не вдалося надіслати повідомлення")
        time.sleep(0.5)
    return result


def signature(record):
    payload = json.dumps({"date": record["date"], "saved_at": record.get("saved_at"),
                          "data": record["data"]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--digest", metavar="TITLE")
    ap.add_argument("--tomorrow", action="store_true")
    args = ap.parse_args()

    if args.test:
        print(send_telegram("✅ Тест: бот графіків підключений і працює."))
        return

    records = fetch_records()

    if args.learn:
        from collections import Counter
        print(f"RECORDS: {len(records)} записів")
        for r in records[:10]:
            c = dict(Counter(str(v) for v in r["data"]))
            print(f"  date={r['date']} saved_at={r.get('saved_at')} cells={len(r['data'])} values={c}")
        rec, note = record_for(records, "today")
        grid = build_grid(rec)
        print(f"\nПоточний запис: {rec['date']} | черг: {len(grid)} | підчерги: {', '.join(grid)}")
        for lbl in QUEUE_LABELS[:4]:
            print(f"  {lbl}: {windows(grid[lbl]) or 'без відключень'}")
        return

    if args.digest:
        rec, note = record_for(records, "tomorrow" if args.tomorrow else "today")
        grid = build_grid(rec) if rec else None
        msg = build_digest_message(args.digest, rec, grid, note)
        print(f"Дайджест: date={rec['date'] if rec else '-'}, довжина={len(msg)}")
        print(send_telegram(msg))
        return

    # режим перевірки змін
    rec, note = record_for(records, "today")
    sig = signature(rec)
    state = load_state()
    if state.get("signature") == sig:
        print(f"Без змін ({rec['date']}, перевірено {len(QUEUE_LABELS)} підчерг).")
        return
    msg = build_alert_message(rec, build_grid(rec))
    if state:
        print(send_telegram(msg))
    else:
        print("Перший запуск: стан збережено, повідомлення не надіслано.")
        print("\n--- Попередній перегляд ---\n" + msg)
    save_state({"signature": sig, "date": rec["date"], "saved_at": rec.get("saved_at")})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ПОМИЛКА: {e}", file=sys.stderr)
        sys.exit(1)
