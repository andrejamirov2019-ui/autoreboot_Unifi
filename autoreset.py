#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
import json
import signal
import threading
from datetime import datetime, timedelta

import urllib3
from dotenv import load_dotenv
from pyunifi.controller import Controller
import requests
import argparse
from typing import Union, Optional, Tuple, List, Dict, Set

# --- Инициализация ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# UniFi
UNIFI_HOST = os.getenv("UNIFI_HOST")
UNIFI_USER = os.getenv("UNIFI_USER")
UNIFI_PASS = os.getenv("UNIFI_PASS")
UNIFI_SITE = os.getenv("UNIFI_SITE", "default")

# --- Окно техобслуживания (ребут) ---
MAINTENANCE = threading.Event()   # True, когда идёт плановый ребут
REBOOT_TRACK_LOCK = threading.Lock()
REBOOT_TRACK: Set[str] = set()    # MAC'и AP, которые мы перезагружаем сейчас

# Планировщик ребута
REBOOT_ENABLED = os.getenv("REBOOT_ENABLED", "1") == "1"   # "1" включено по умолчанию
REBOOT_DOW = os.getenv("REBOOT_DOW")                # 0..6 или mon..sun; по умолчанию воскресенье
REBOOT_AT = os.getenv("REBOOT_AT")                # локальное время HH:MM

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Интервалы
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))          # сек
REBOOT_WAIT_TIMEOUT = int(os.getenv("REBOOT_WAIT_TIMEOUT", "300"))  # сек ожидания после ребута

# Файл состояния
STATE_FILE = os.getenv("STATE_FILE", "unifi_ap_state.json")

# --- Приглушение алертов во время ребута ---

SILENCE_UNTIL: Dict[str, float] = {}
SILENCE_LOCK = threading.Lock()

# upd 26.03.2026 прокси
TG_PROXY = os.getenv("TG_PROXY")

# --- Утилиты / Telegram ---
'''
def send_tg(text: str, disable_notification: bool = False) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы — пропускаю отправку TG")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": disable_notification
        }, timeout=10)
        if resp.status_code != 200:
            print(f"[WARN] Telegram HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[WARN] Telegram error: {e}")

def human_ts(ts: Optional[Union[float, int]] = None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
'''

# upd 26.03.2026
def send_tg(text: str, disable_notification: bool = False) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы — пропускаю отправку TG")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    proxies = None
    if TG_PROXY:
        proxies = {
            "http": TG_PROXY,
            "https": TG_PROXY,
        }

    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": disable_notification
            },
            proxies=proxies,
            timeout=15
        )
        if resp.status_code != 200:
            print(f"[WARN] Telegram HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[WARN] Telegram error: {e}")


# --- Подключение к контроллеру ---

def connect_controller() -> Controller:
    print(f"[INFO] Соединение с UniFi Controller {UNIFI_HOST} (site={UNIFI_SITE}) ...")
    return Controller(
        host=UNIFI_HOST,
        username=UNIFI_USER,
        password=UNIFI_PASS,
        site_id=UNIFI_SITE,
        ssl_verify=False
    )

# --- Работа с AP ---

def fetch_aps(c: Controller) -> List[Dict]:
    """Возвращает только валидные UAP (adopted=True, type='uap')."""
    aps = c.get_aps()
    out = []
    for ap in aps:
        if ap.get("adopted") and ap.get("type") == "uap":
            out.append(ap)
    return out

def ap_key(ap) -> str:
    return ap.get("mac")

def ap_ip(ap) -> str:
    return ap.get("ip") or "-"
    
def ap_online(ap) -> bool:
    # В UniFi state == 1 обычно значит connected
    return ap.get("state") == 1

def ap_uptime(ap) -> int:
    return int(ap.get("uptime") or 0)

def ap_model(ap) -> str:
    return ap.get("model") or ap.get("type") or "uap"

def ap_display(ap, mac: str) -> str:
    """Формирует красивую подпись: Имя (IP) [MAC]"""
    nm = ap.get("name") or mac
    ip = ap.get("ip")
    if ip:
        return f"<b>{nm}</b> ({ip}) [{mac}]"
    return f"<b>{nm}</b> [{mac}]"
    
    
def trigger_spectrum_scan(c: Controller, mac: str, name: str):
    """Запускает RF-скан (аналог 'Scan Channels' в UniFi UI)."""
    try:
        c._run_command("spectrum-scan", {"mac": mac}, "devmgr")
        print(f"[INFO] Spectrum scan started on {name} [{mac}]")
        return True
    except Exception as e:
        print(f"[WARN] Spectrum scan failed on {name} [{mac}]: {e}")
        return False
        
        
    
# --- Хранение состояния ---

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Не могу сохранить состояние: {e}")

def snapshot_status(c: Controller) -> dict:
    """Карта mac -> {'name','ip','online','uptime','model'}"""
    snap: Dict[str, Dict] = {}
    for ap in fetch_aps(c):
        mac = ap_key(ap)
        snap[mac] = {
            "name": ap.get("name") or mac,  # без IP
            "ip": ap.get("ip") or "-",      # отдельно IP
            "online": ap_online(ap),
            "uptime": ap_uptime(ap),
            "model": ap_model(ap),
        }
    return snap


# --- Ребут и ожидание ---

def reboot_all(c: Controller) -> List[str]:
    aps = fetch_aps(c)
    elig = [ap for ap in aps if ap_online(ap)]
    if not elig:
        send_tg("⚠️ Не найдено ни одной доступной AP для перезагрузки.")
        return []
        
    # Открываем окно техработ: заморозим мониторинг и baseline
    elig_macs = [ap_key(ap) for ap in elig]
    with REBOOT_TRACK_LOCK:
        REBOOT_TRACK = set(elig_macs)
    MAINTENANCE.set()
    
    lines = [f"🔄 Запускаю ребут {len(elig)} AP:"]
    for ap in sorted(elig, key=lambda x: (x.get('name') or ap_key(x)).lower()):
        mac = ap_key(ap)
        rec = {
            "name": ap.get("name") or mac,
            "ip": ap.get("ip") or "-",
        }
        lines.append(f" • {ap_display(rec, mac)} ({ap_model(ap)})")
    send_tg("\n".join(lines))

    ok: List[str] = []
    errs: List[Tuple[str, str]] = []
    for ap in elig:
        mac = ap_key(ap)
        try:
            c.restart_ap(mac)
            ok.append(mac)
        except Exception as e:
            errs.append((mac, str(e)))
            print(f"[ERR] restart {ap_name(ap)} {mac}: {e}")

    if errs:
        lines = ["⚠️ Ошибка отправки перезагрузки"]
        for mac, msg in errs:
            nm = next((ap_name(a) for a in elig if ap_key(a) == mac), mac)
            lines.append(f" • {nm} [{mac}] — {msg}")
        send_tg("\n".join(lines))
    else:
        send_tg("✅ Отправил в перезагрузку все доступные AP.")

    return ok
            
            
def report_current_offline(c: Controller) -> None:
    """Отчет какие точки в оффлайн"""
    snap = snapshot_status(c)
    offline = [(mac, ap) for mac, ap in snap.items() if not ap.get("online")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if offline:
        lines = [f"🔻 Оффлайн AP сейчас (после ребута), {ts}:"]
        for mac, ap in sorted(offline, key=lambda x: (x[1].get("name") or x[0]).lower()):
            lines.append(f" • {ap_display(ap, mac)} (uptime {ap.get('uptime', 0)}s)")
        send_tg("\n".join(lines))
    else:
        send_tg(f"🟢 Все AP онлайн по состоянию на {ts}.")

        

def wait_after_reboot(c: Controller, rebooted_macs: List[str], timeout_s: int = REBOOT_WAIT_TIMEOUT) -> None:
    """
    После отправки команд на ребут:
      - ждёт 3 минуты (точки уходят и поднимаются),
      - выполняет RF-сканирование на онлайн-точках,
      - ждёт до REBOOT_WAIT_TIMEOUT и отправляет итоговый отчёт.
    Всё это время мониторинг молчит (MAINTENANCE активен).
    """
    if not rebooted_macs:
        return

    started = time.time()
    send_tg(
        f"⏳ Начато техобслуживание: перезагрузка {len(rebooted_macs)} AP. "
        f"Итоговый отчёт будет через {timeout_s // 60} мин.",
        disable_notification=True
    )

    # --- первая пауза (ждём загрузку после ребута) ---
    time.sleep(180)  # 3 минуты

    # --- сканирование на тех, кто уже онлайн ---
    snap = snapshot_status(c)
    for mac, ap in snap.items():
        if ap.get("online"):
            try:
                c._run_command("spectrum-scan", {"mac": mac}, "devmgr")
                print(f"[INFO] Spectrum scan started on {ap.get('name', mac)} [{mac}]")
            except Exception as e:
                print(f"[WARN] Spectrum scan failed on {ap.get('name', mac)} [{mac}]: {e}")

    # --- оставшееся ожидание до финального отчёта ---
    elapsed = int(time.time() - started)
    remaining = max(0, timeout_s - elapsed)
    if remaining > 0:
        time.sleep(remaining)

    # --- финальный отчёт ---
    snap = snapshot_status(c)
    offline = [(mac, ap) for mac, ap in snap.items() if not ap.get("online")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if offline:
        lines = [f"🔴 Техобслуживание завершено ({ts}). Оффлайн {len(offline)} AP:"]
        for mac, ap in sorted(offline, key=lambda x: (x[1].get('name') or x[0]).lower()):
            lines.append(f" • {ap_display(ap, mac)} (uptime {ap.get('uptime', 0)}s)")
        send_tg("\n".join(lines))
    else:
        send_tg(f"✅ Техобслуживание завершено ({ts}): все {len(snap)} AP онлайн.")

    # --- возвращаем мониторинг ---
    MAINTENANCE.clear()
    with REBOOT_TRACK_LOCK:
        REBOOT_TRACK.clear()

    
# --- Мониторинг ---

def monitor_loop(c: Controller, stop_event: threading.Event) -> None:
    """
    Непрерывный мониторинг состояния AP:
      - online -> offline  => 🟠 «упала»
      - offline -> online  => 🟢 «поднялась»

    ВО ВРЕМЯ окна техработ (MAINTENANCE.is_set()):
      - не шлём алерты,
      - не трогаем baseline (prev) и файл состояния,
      - просто ждём окончания окна.

    ПОСЛЕ окна техработ:
      - делаем "мягкий" сброс baseline: prev = актуальный снапшот,
        чтобы не полетели массовые переходы.
    """
    # начальное состояние
    prev = load_state()
    snap = snapshot_status(c)
    save_state(snap)
    prev = snap

    send_tg(
        f"🛎 Мониторинг UniFi запущен. Интервал опроса: {POLL_INTERVAL} сек. Контроллер: {UNIFI_HOST}",
        disable_notification=True
    )

    was_maintenance = MAINTENANCE.is_set()

    while not stop_event.is_set():
        try:
            # Если идёт окно техработ — просто "спим", baseline не трогаем, алерты не шлём
            if MAINTENANCE.is_set():
                was_maintenance = True
                stop_event.wait(POLL_INTERVAL)
                continue

            # Если мы ТОЛЬКО ЧТО вышли из техработ — мягко сбрасываем baseline
            if was_maintenance and not MAINTENANCE.is_set():
                prev = snapshot_status(c)
                save_state(prev)
                was_maintenance = False
                # можно уведомить, что мониторинг снова активен
                send_tg("🔔 Окно техработ завершено. Мониторинг возобновлён.", disable_notification=True)

            # Обычный мониторинг
            curr = snapshot_status(c)

            for mac, cur in curr.items():
                name = cur["name"]
                was = prev.get(mac)

                if was:
                    # online -> offline
                    if was.get("online") and not cur.get("online"):
                        send_tg(f"🟠 Упала AP: {ap_display(cur, mac)} (была online, теперь offline)")
                    # offline -> online
                    if (not was.get("online")) and cur.get("online"):
                        send_tg(f"🟢 Поднялась AP: {ap_display(cur, mac)} (uptime {cur.get('uptime', 0)}s)")
                else:
                    # новая точка
                    state_emoji = "🟢" if cur.get("online") else "⚫️"
                    send_tg(
                        f"{state_emoji} Обнаружена новая AP: {ap_display(cur, mac)} "
                        f"online={cur.get('online')}, model={cur.get('model')}",
                        disable_notification=True
                    )

            # исчезнувшие устройства
            removed = set(prev.keys()) - set(curr.keys())
            for mac in removed:
                was = prev[mac]
                send_tg(f"⚫️ AP пропала из инвентаря контроллера: {ap_display(was, mac)}.")

            # сохраняем текущее состояние и ждём следующую итерацию
            save_state(curr)
            prev = curr

        except Exception as e:
            print(f"[WARN] monitor iteration error: {e}")

        stop_event.wait(POLL_INTERVAL)

# --- Еженедельный планировщик ---

def _parse_dow(s: Union[str, int]) -> int:
    mapping = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
    try:
        return int(s)
    except Exception:
        return mapping.get(str(s).strip().lower(), 6)  # по умолчанию Sunday

def _next_run_delta_seconds(dow: int, at_hhmm: str) -> Tuple[float, datetime]:
    hh, mm = map(int, at_hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days_ahead = (dow - now.weekday()) % 7
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    run_dt = target + timedelta(days=days_ahead)
    return (run_dt - now).total_seconds(), run_dt

def weekly_rebooter(c: Controller, stop_event: threading.Event) -> None:
    """Фоновый планировщик: раз в неделю запускает reboot_all + ожидание подъёма."""
    if not REBOOT_ENABLED:
        print("[INFO] Weekly rebooter disabled (REBOOT_ENABLED=0)")
        return
    dow = _parse_dow(REBOOT_DOW)
    while not stop_event.is_set():
        sleep_s, when = _next_run_delta_seconds(dow, REBOOT_AT)
        send_tg(f"🕒 Следующая плановая перезагрузка точек WiFi: {when.strftime('%Y-%m-%d %H:%M')}", disable_notification=True)

        # спим до времени старта, но уважаем stop_event
        waited = 0
        while waited < sleep_s and not stop_event.is_set():
            step = min(300, sleep_s - waited)  # ≤5 минутные шаги
            stop_event.wait(step)
            waited += step

        if stop_event.is_set():
            break

        rebooted = reboot_all(c)
        if rebooted:
            wait_after_reboot(c, rebooted, timeout_s=REBOOT_WAIT_TIMEOUT)

# --- Точка входа ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reboot-once", action="store_true",
                        help="Запустить reboot_all один раз и выйти (без мониторинга/планировщика)")
    args = parser.parse_args()
    parser.add_argument(
        "--reboot-at-start",
        action="store_true",
        help="Сделать плановую перезагрузку сразу при старте, затем продолжить работу (мониторинг + планировщик)"
)


    stop_event = threading.Event()

    def _stop(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    c = connect_controller()
    
    if args.reboot_once:
        rebooted = reboot_all(c)
        if rebooted:
            wait_after_reboot(c, rebooted, timeout_s=REBOOT_WAIT_TIMEOUT)
        return

    # мониторинг и еженедельный планировщик — параллельно
    t_monitor = threading.Thread(target=monitor_loop, args=(c, stop_event), daemon=True)
    t_monitor.start()

    t_weekly = threading.Thread(target=weekly_rebooter, args=(c, stop_event), daemon=True)
    t_weekly.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        send_tg("🛑 Мониторинг UniFi остановлен.", disable_notification=True)

if __name__ == "__main__":
    main()
