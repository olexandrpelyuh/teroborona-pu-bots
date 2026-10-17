#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот моніторингу графіків відключень ДПЕМ «Атомсервіс» (info.dpem.mk.ua).

Режими:
  python bot.py                      # перевірка змін (для cron / GitHub Actions)
  python bot.py --digest "☀️ Графік на сьогодні"   # дайджест (завжди надсилає)
  python bot.py --learn              # показати кольори комірок (налаштування color_map.json)
  python bot.py --test               # тестове повідомлення в канал

Змінні середовища:
  BOT_TOKEN, CHANNEL_ID — обов'язкові
  WATCH_QUEUES          — напр. "2-І,2-ІІ" (лише ці підчерги); порожньо = всі
  GRAFIK_URL            — сторінка графіка (за замовчуванням — сьогоднішня)
  DIGEST_URL            — сторінка для дайджеста "на завтра" (якщо відрізняється)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = os.environ.get("GRAFIK_URL", "https://info.dpem.mk.ua/off_graf.html")
STATE_FILE = Path(__file__).parent / "state.json"
COLOR_MAP_FILE = Path(__file__).parent / "color_map.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")  # @nazva_kanalu або -100xxxxxxxxxx

WATCH_QUEUES = os.environ.get("WATCH_QUEUES", "")

SLOTS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]  # 48 слотів по 30 хв

DEFAULT_COLOR_MAP = {
    # "red":       "off",
    # "#ff0000":   "off",
    # "img:blink.gif": "off",
    # "yellow":    "switch",
    # "green":     "on",
}

EMOJI = {"on": "🟩", "off": "🟥", "possible": "🟧", "switch": "🟨", "empty": "⬜", "unknown": "⬛"}


def load_color_map():
    mapping = dict(DEFAULT_COLOR_MAP)
    if COLOR_MAP_FILE.exists():
        mapping.update(json.loads(COLOR_MAP_FILE.read_text(encoding="utf-8")))
    return mapping


def cell_status(td, color_map):
    """Статус комірки за <img> / bgcolor / style / class."""
    key = None
    bgcolor = (td.get("bgcolor") or "").strip().lower()
    style = (td.get("style") or "").lower()
    classes = " ".join(td.get("class") or [])
    img = td.find("img")
    if img is not None:
        src = (img.get("src") or img.get("data-src") or "").strip()
        key = "img:" + src.split("/")[-1] if src else "img"
    elif bgcolor:
        key = bgcolor
    elif "background" in style:
        for part in style.split(";"):
            if "background" in part and ":" in part:
                val = part.split(":", 1)[1].strip()
                if "url(" in val:
                    m = re.search(r"url\(['\"]?(.*?)['\"]?\)", val)
                    val = "img:" + (m.group(1).split("/")[-1] if m else val)
                key = val
                break
    elif classes:
        key = classes
    if key is None:
        return "empty", None
    key_l = key.lower()
    for k, v in color_map.items():
        if k.lower() == key_l:
            return v, key
    return "unknown", key


def fetch_grid(url=None):
    """Завантажити сторінку й розпарсити таблицю графіка."""
    url = url or URL
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (grafik-bot/1.0)"}, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    date_text = ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "div", "p", "span", "b"]):
        if "Графік відключень на" in el.get_text():
            date_text = el.get_text(" ", strip=True)
            break

    table = None
    for t in soup.find_all("table"):
        header_cells = [c.get_text(strip=True) for c in t.find_all(["th", "td"])[:60]]
        joined = " ".join(header_cells)
        if "00:00" in joined and "23:30" in joined:
            table = t
            break
    if table is None:
        raise RuntimeError("Не знайшов таблицю графіка на сторінці (можливо, змінилася верстка)")

    color_map = load_color_map()
    grid = {}
    unknown = {}
    current_queue = ""
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        first = cells[0].get_text(" ", strip=True)
        if "черга" in first.lower() and "підчерга" not in first.lower():
            current_queue = first.replace("черга", "").strip()
            continue
        if "підчерга" in first.lower():
            sub = first.replace("підчерга", "").strip()
            label = f"{current_queue}-{sub}" if current_queue and "-" not in sub else sub
            statuses = []
            for td in cells[1:][: len(SLOTS)]:
                st, raw = cell_status(td, color_map)
                statuses.append(st)
                if st == "unknown" and raw is not None:
                    unknown[raw] = unknown.get(raw, 0) + 1
            grid[label] = statuses
    return date_text, grid, unknown


def windows(statuses, target=("off", "possible")):
    """Склеїти послідовні слоти зі статусом із target: «HH:MM–HH:MM»."""
    result, start = [], None
    for i, st in enumerate(list(statuses) + ["end"]):
        if st in target and start is None:
            start = i
        elif st not in target and start is not None:
            result.append(f"{SLOTS[start]}–{(i * 30) // 60 % 24:02d}:{(i * 30) % 60:02d}")
            start = None
    return result


def timeline(statuses):
    """Візуальна стрічка доби: 24 блоки (по 1 год), потім деталізація 2×24."""
    day1 = "".join(EMOJI.get(st, "⬛") for st in statuses[:24])
    day2 = "".join(EMOJI.get(st, "⬛") for st in statuses[24:])
    return f"🌙 00–12  {day1}\n☀️ 12–24  {day2}"


def watched_labels(grid):
    watch = [w.strip() for w in WATCH_QUEUES.split(",") if w.strip()]
    labels = sorted(grid, key=lambda x: [int(p) if p.isdigit() else p for p in
                 x.replace("ІІ", "2").replace("І", "1").split("-")])
    return [l for l in labels if not watch or l in watch]


def off_hours(statuses):
    """Скільки годин без світла за добу (off + possible)."""
    n = sum(1 for st in statuses if st in ("off", "possible"))
    h, m = divmod(n * 30, 60)
    return f"{h} год" + (f" {m} хв" if m else "")


def next_off_line(statuses, now=None):
    """Стан зараз + наступне відключення: найкорисніший рядок для людини."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo("Europe/Kyiv"))
    idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    cur = statuses[idx] if idx < len(statuses) else "empty"
    # кінець поточного вікна
    if cur in ("off", "possible"):
        end = idx
        while end < len(statuses) and statuses[end] in ("off", "possible"):
            end += 1
        if end < len(statuses):
            eh, em = divmod(end * 30, 60)
            return f"⚡ <b>Зараз без світла</b> — очікується до {eh:02d}:{em:02d}"
        return "⚡ <b>Зараз без світла</b>"
    # наступне вікно
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


def queue_block(label, statuses, now=None):
    """Красивий блок однієї черги."""
    wins = windows(statuses)
    lines = [f"<b>Черга {label}</b>", timeline(statuses)]
    if wins:
        lines.append(f"🔴 Відключення: {', '.join(wins)} · разом {off_hours(statuses)}")
    else:
        lines.append("🟢 Без відключень ✅")
    if now is not None:
        lines.append(next_off_line(statuses, now))
    return lines


def build_alert_message(date_text, grid):
    """Повідомлення про ЗМІНУ графіка."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    lines = ["⚡ <b>ГРАФІК ВІДКЛЮЧЕНЬ ЗМІНЕНО!</b>", ""]
    if date_text:
        lines.append(f"📅 {date_text}")
    lines.append("")
    labels = watched_labels(grid)
    labels.sort(key=lambda l: 0 if windows(grid[l]) else 1)  # з відключеннями — першими
    any_off = False
    for label in labels:
        if windows(grid[label]):
            any_off = True
        lines += queue_block(label, grid[label], now)
        lines.append("")
    if not any_off:
        lines.append("✅ Відключень за графіком немає")
    return "\n".join(lines).rstrip()


def build_digest_message(title, date_text, grid):
    """Щоденний дайджест (завжди надсилається)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    lines = [title, ""]
    if date_text:
        lines.append(f"📅 {date_text}")
    lines.append("")
    labels = watched_labels(grid)
    labels.sort(key=lambda l: 0 if windows(grid[l]) else 1)
    for label in labels:
        lines += queue_block(label, grid[label], now)
        lines.append("")
    lines.append("🟩 світло · 🟥 відключення · 🟧 можливе · 🟨 перемикання")
    lines.append("⏳ На перемикання черг — до 30 хвилин")
    return "\n".join(lines).rstrip()


def _utf16_len(s):
    return len(s.encode("utf-16-le")) // 2  # Telegram рахує ліміт у UTF-16 одиницях


def _split_text(text, limit=3800):
    """Розбити довге повідомлення на частини по межах рядків (ліміт UTF-16)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        add = _utf16_len(cur) + _utf16_len(line) + 1
        if add > limit and cur:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [text]


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
                continue  # повтор без HTML-розмітки
            raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")
        else:
            raise RuntimeError("Telegram: не вдалося надіслати повідомлення")
        time.sleep(0.5)
    return result

def grid_signature(date_text, grid):
    payload = json.dumps({"date": date_text, "grid": grid}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--learn", action="store_true", help="показати знайдені кольори комірок")
    ap.add_argument("--test", action="store_true", help="надіслати тестове повідомлення")
    ap.add_argument("--digest", metavar="TITLE", help="режим дайджеста: надіслати повідомлення завжди")
    args = ap.parse_args()

    if args.test:
        print(send_telegram("✅ Тест: бот графіків підключений і працює."))
        return

    # Дайджест «на завтра» може жити на іншій сторінці (DIGEST_URL)
    url = os.environ.get("DIGEST_URL", "") if args.digest else ""
    date_text, grid, unknown = fetch_grid(url or None)
    if not grid:
        raise RuntimeError("Таблицю знайдено, але підчерги не розпізнано")

    if args.learn:
        print(f"URL: {url or URL}")
        print(f"Дата графіка: {date_text}")
        print(f"Знайдено підчерг: {len(grid)}: {', '.join(sorted(grid))}")
        cm = load_color_map()
        print(f"Відомі кольори (color_map.json): {cm or '— порожньо —'}")
        if unknown:
            print("\nНЕРОЗПІЗНАНІ кольори/стилі комірок (додай їх у color_map.json):")
            for k, v in sorted(unknown.items(), key=lambda x: -x[1]):
                print(f"  {k!r}: {v} комірок")
        else:
            print("\nНевідомих кольорів немає — мапа кольорів повна.")
        return

    if args.digest:
        msg = build_digest_message(args.digest, date_text, grid)
        print(f"Діагностика: підчерг={len(grid)}, довжина={len(msg)}, дата='{date_text}'")
        print(send_telegram(msg))
        return

    sig = grid_signature(date_text, grid)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if state.get("signature") == sig:
        print(f"Без змін (перевірено {len(grid)} підчерг).")
        return

    msg = build_alert_message(date_text, grid)
    if state:
        res = send_telegram(msg)
        print("Надіслано повідомлення про зміну:", json.dumps(res, ensure_ascii=False)[:200])
    else:
        print("Перший запуск: стан збережено, повідомлення не надіслано.")
        print("\n--- Попередній перегляд повідомлення ---\n" + msg)

    STATE_FILE.write_text(json.dumps({"signature": sig, "date": date_text}, ensure_ascii=False),
                          encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ПОМИЛКА: {e}", file=sys.stderr)
        sys.exit(1)
