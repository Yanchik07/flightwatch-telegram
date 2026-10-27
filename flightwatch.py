"""Local cached-fare monitor. Python 3.11+, standard library only."""

import argparse
import fcntl
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import logging
import math
import os
import re
from pathlib import Path
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
NAMES = {"IJK": "Ижевск", "SVX": "Екатеринбург", "KZN": "Казань", "PEE": "Пермь", "MOW": "Москва", "UFA": "Уфа", "CXR": "Нячанг"}
LOG = logging.getLogger("flightwatch")


def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, sep, value = line.partition("=")
                if not sep:
                    raise ValueError("Некорректная строка в .env")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path):
    cfg = json.loads(path.read_text())
    start, end = date.fromisoformat(cfg["departure_start"]), date.fromisoformat(cfg["departure_end"])
    if end < start:
        raise ValueError("Конец диапазона раньше начала")
    for code in [*cfg["origins"], cfg["destination"]]:
        if not isinstance(code, str) or len(code) != 3 or not code.isascii() or not code.isalpha() or not code.isupper():
            raise ValueError("Нужны трёхбуквенные IATA-коды в верхнем регистре")
    if not cfg["origins"] or not 1 <= cfg["top_n"] <= 50 or cfg["interval_seconds"] < 60:
        raise ValueError("Проверьте origins, top_n (1..50), interval_seconds (>=60)")
    if cfg["max_transfers"] < 0 or (cfg["max_price"] is not None and cfg["max_price"] <= 0):
        raise ValueError("Некорректное ограничение пересадок или цены")
    if not isinstance(cfg["currency"], str) or len(cfg["currency"]) != 3 or not cfg["currency"].isascii() or not cfg["currency"].isalpha():
        raise ValueError("Некорректная валюта")
    if not (0 <= cfg["quiet_start_hour"] < cfg["quiet_end_hour"] <= 24):
        raise ValueError("Некорректные часы ночного режима")
    if cfg["currency"].lower() != "rub" or cfg["night_price_threshold_rub"] <= 0:
        raise ValueError("Ночной порог требует валюту RUB и положительную сумму")
    return cfg


def request_json(url, headers=None, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=body, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            # Never log exception URLs: Telegram embeds the bot token in its URL.
            if exc.code == 400 and url.endswith("/editMessageText"):
                error = json.load(exc)
                if "message is not modified" in error.get("description", ""):
                    return {"ok": True, "unchanged": True}
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Ошибка HTTP {exc.code}") from None
            time.sleep(min(30, max(1, int(exc.headers.get("Retry-After", 2 ** attempt)))))
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("Сетевая ошибка: проверьте интернет и доступность API") from None
            time.sleep(2 ** attempt)


def months(start, end):
    current = date.fromisoformat(start).replace(day=1)
    last = date.fromisoformat(end)
    while current <= last:
        yield current.strftime("%Y-%m")
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)


def search_origins(cfg):
    origins = list(cfg["origins"])
    if "MOW" in origins:
        origins.extend(cfg.get("moscow_airports", []))
    return list(dict.fromkeys(origins))


def fetch_offers(cfg, token):
    offers, errors = [], []
    for origin, month, direct in (
        (origin, month, direct)
        for origin in search_origins(cfg)
        for month in months(cfg["departure_start"], cfg["departure_end"])
        for direct in ("true", "false")
    ):
            try:
                for page in range(1, 101):
                    query = urlencode(dict(origin=origin, destination=cfg["destination"],
                                           departure_at=month, currency=cfg["currency"],
                                           one_way="true", direct=direct, unique="false", sorting="price",
                                           limit=30, page=page, market="ru"))
                    result = request_json("https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + query,
                                          {"X-Access-Token": token})
                    if result.get("success") is not True or not isinstance(result.get("data"), list):
                        raise RuntimeError("API вернул ошибку или неожиданный формат")
                    rows = result["data"]
                    offers.extend(rows)
                    if len(rows) < 30:
                        break
                    time.sleep(0.3)
                else:
                    errors.append(f"{origin}/{month}/direct={direct}: достигнут предел страниц, выборка неполная")
            except RuntimeError as exc:
                errors.append(f"{origin}/{month}/direct={direct}: {exc}")
    return offers, errors


def foreign_connection(row, airport_countries):
    # The link format is undocumented: accept only an unambiguous three-airport route.
    ticket = parse_qs(urlsplit(row.get("link", "")).query).get("t", [""])[0].split("_")[0]
    match = re.fullmatch(r"[A-Z0-9]{2}[0-9]+([A-Z]{9})", ticket)
    if not match:
        return None
    route = [match[1][i:i + 3] for i in range(0, 9, 3)]
    if route[0] != row.get("origin_airport") or route[-1] != row.get("destination_airport"):
        return None
    country = airport_countries.get(route[1])
    return route[1] if country and country != "RU" else None


def select_offers(rows, cfg, today=None):
    today = today or datetime.now(timezone.utc).date()
    airport_countries = {}
    if cfg.get("exclude_russian_connections"):
        airports = json.loads((ROOT / "resources" / "airports.json").read_text())
        airport_countries = {airport["code"]: airport.get("country_code") for airport in airports}
    unique = {}
    for row in rows:
        try:
            if row.get("origin_airport") in ("SVO", "DME", "VKO") or row.get("origin") in ("SVO", "DME", "VKO"):
                row = dict(row, origin="MOW")
            departure = date.fromisoformat(row["departure_at"][:10])
            price = float(row["price"])
            transfers = int(row["transfers"])
            origin_matches = any(code in cfg["origins"] for code in
                                 (row.get("origin"), row.get("origin_airport")))
            destination_matches = cfg["destination"] in (row.get("destination"), row.get("destination_airport"))
            if (not origin_matches or not destination_matches
                    or row.get("return_at") or departure < today
                    or not cfg["departure_start"] <= departure.isoformat() <= cfg["departure_end"]
                    or not math.isfinite(price) or price <= 0 or transfers < 0
                    or transfers > cfg["max_transfers"]
                    or (cfg["max_price"] is not None and price > cfg["max_price"])):
                continue
            link = row.get("link", "")
            if not link.startswith("/search/") or urlsplit(link).netloc:
                continue
            connection = None
            if transfers and cfg.get("exclude_russian_connections"):
                connection = foreign_connection(row, airport_countries) if transfers == 1 else None
                if not connection:
                    continue
            key = hashlib.sha256(json.dumps([row["origin"], row.get("destination_airport") or row["destination"],
                datetime.fromisoformat(row["departure_at"]).isoformat(), row.get("airline"), str(row.get("flight_number", "")).lstrip("0"),
                row.get("origin_airport"), row.get("destination_airport"), transfers,
                row.get("duration"), connection], ensure_ascii=True).encode()).hexdigest()
            item = dict(row, price=price, key=key, url="https://www.aviasales.ru" + link,
                        connection_airport=connection)
            if key not in unique or price < unique[key]["price"]:
                unique[key] = item
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(unique.values(), key=lambda row: (row["transfers"] != 0, row["price"]))


def open_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS prices (key TEXT, currency TEXT, price REAL, seen TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS prices_lookup ON prices(key, currency)")
    db.execute("CREATE INDEX IF NOT EXISTS prices_period ON prices(key, currency, seen)")
    return db


def record_offers(db, offers, currency):
    previous = {}
    with db:
        for row in offers:
            if row.get("unavailable"):
                continue
            old = db.execute("SELECT price FROM prices WHERE key=? AND currency=? ORDER BY rowid DESC LIMIT 1",
                             (row["key"], currency)).fetchone()
            previous[row["key"]] = old[0] if old else None
            db.execute("INSERT INTO prices VALUES (?, ?, ?, ?)",
                       (row["key"], currency, row["price"], datetime.now(timezone.utc).isoformat()))
    return previous


def annotate_minimums(db, offers, currency, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=7)).isoformat()
    for row in offers:
        row.pop("minimum_7d", None)
        if row.get("unavailable"):
            continue
        minimum, count = db.execute(
            "SELECT MIN(price), COUNT(*) FROM prices WHERE key=? AND currency=? AND seen>=? AND seen<=?",
            (row["key"], currency, cutoff, now.isoformat())).fetchone()
        if count >= 2:
            row["minimum_7d"] = minimum


def visible_length(message):
    class TextParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.length = 0

        def handle_data(self, data):
            self.length += len(data.encode("utf-16-le")) // 2

    parser = TextParser()
    parser.feed(message)
    return parser.length


def connection_duration(row):
    try:
        total, flying = float(row["duration"]), float(row["duration_to"])
        minutes = total - flying
        if not (math.isfinite(minutes) and flying > 0 and minutes > 0 and minutes.is_integer()):
            return "время уточняется"
        hours, remainder = divmod(int(minutes), 60)
        return " ".join(part for part in (f"{hours} ч" if hours else "", f"{remainder} мин" if remainder else "") if part)
    except (KeyError, TypeError, ValueError):
        return "время уточняется"


def digest_offers(offers, cfg):
    selected = []
    for origin in cfg["origins"]:
        for transfers in (0, 1):
            candidates = [row for row in offers if row["transfers"] == transfers
                          and origin in (row.get("origin"), row.get("origin_airport"))]
            selected.extend(sorted(candidates, key=lambda row: (row["price"], row["departure_at"])))
    return selected


def render_messages(offers, cfg, previous, errors=(), demo=False):
    airports = {airport["code"]: airport for airport in
                json.loads((ROOT / "resources" / "airports.json").read_text())}
    airport_cities = {}
    if any(row.get("connection_airport") for row in offers):
        cities = {city["code"]: city["name"] for city in
                  json.loads((ROOT / "resources" / "cities.json").read_text())}
        airport_cities = {airport["code"]: cities.get(airport.get("city_code"), airport["code"])
                          for airport in json.loads((ROOT / "resources" / "airports.json").read_text())}
    blocks = ["ДЕМО: ВЫМЫШЛЕННЫЕ ЦЕНЫ"] if demo else []
    if errors:
        blocks.append("Внимание: подборка неполная, часть источников недоступна.")
    if not offers:
        blocks.append("Подходящих предложений в полученных данных нет. Это не означает, что билетов нет.")
    group_name = None
    ordered = digest_offers(offers, cfg)
    for index, row in enumerate(ordered):
        departure = datetime.fromisoformat(row["departure_at"])
        if departure.tzinfo is None:
            raise ValueError("Время вылета от API не содержит часовой пояс")
        origin_airport = row.get("origin_airport") or row["origin"]
        airport_zone = airports.get(origin_airport, {}).get("time_zone")
        if airport_zone:
            departure = departure.astimezone(ZoneInfo(airport_zone))
        departure_text = departure.strftime("%d.%m %H:%M")
        destination_airport = row.get("destination_airport") or cfg["destination"]
        name = NAMES.get(row["origin"], row["origin"])
        route = origin_airport
        connection = row.get("connection_airport")
        if connection:
            route += f" → {airport_cities.get(connection, connection)} ({connection}, {connection_duration(row)})"
        title = escape(route + f" → {destination_airport}")
        parts = urlsplit(row["url"])
        query = parse_qs(parts.query)
        if query.get("t"):
            # Keep the ticket identity; omit optional attribution and fare-preview parameters.
            url = f"https://www.aviasales.ru{parts.path}?" + urlencode({"t": query["t"][0]})
        else:
            url = row["url"] if len(row["url"]) < 2200 else "https://www.aviasales.ru/search/"
        symbol = "✈️" if row["transfers"] == 0 else "🔁"
        price = f"{row['price']:,.0f}".replace(",", " ")
        price_marker = " 🆕" if row.get("price_changed") else ""
        if row.get("price_changed") and row.get("previous_price") is not None:
            direction = "🟢" if row["price"] < row["previous_price"] else "🔴"
            old_price = f"{row['previous_price']:,.0f}".replace(",", " ")
            price_marker += f" {direction} ({old_price} {'₽' if cfg['currency'].lower() == 'rub' else cfg['currency'].upper()})"
        line = (f'{symbol} <a href="{escape(url, quote=True)}"><b><u>{price} '
                f"{'₽' if cfg['currency'].lower() == 'rub' else cfg['currency'].upper()}</u></b></a>"
                f" · {departure_text} · {title}")
        if row.get("minimum_7d") is not None:
            minimum = f"{row['minimum_7d']:,.0f}".replace(",", " ")
            line += f" · мин. 7д: {minimum} ₽"
            if row["price"] == row["minimum_7d"]:
                line += " ✓"
        if row.get("unavailable"):
            line += " · нет в кэше, цена не подтверждена"
        line += price_marker
        new_group = group_name != name
        candidate = blocks + [f"<b>{escape(name)}</b>\n{line}"] if new_group else blocks[:-1] + [blocks[-1] + "\n" + line]
        if visible_length("\n\n".join(candidate)) > 3950:
            blocks.append(f"Не поместилось в лимит Telegram: {len(ordered) - index} рейсов.")
            break
        blocks = candidate
        group_name = name
    stamp = datetime.now(ZoneInfo("Europe/Samara")).strftime("%d.%m.%Y %H:%M:%S")
    blocks.append(f"Обновлено: {stamp} (Самара)")
    return ["\n\n".join(blocks)]


def notification_offers(offers, cfg, now=None):
    if not cfg.get("quiet_hours_enabled", True):
        return offers, False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Для проверки ночного режима нужен часовой пояс")
    hour = now.astimezone(ZoneInfo("Europe/Samara")).hour
    quiet = cfg["quiet_start_hour"] <= hour < cfg["quiet_end_hour"]
    if quiet:
        return [row for row in offers if row["price"] < cfg["night_price_threshold_rub"]], True
    return offers, False


def refresh_offers(previous, current):
    def identity(row):
        return (row.get("origin_airport"), row.get("destination_airport"),
                row.get("airline"), str(row.get("flight_number", "")).lstrip("0"),
                row["departure_at"][:10], row.get("connection_airport"), row["transfers"])

    refreshed = []
    for old in previous:
        exact = [row for row in current if row["key"] == old["key"]]
        matches = exact or [row for row in current if identity(row) == identity(old)]
        if len(matches) == 1:
            refreshed.append(dict(matches[0], price_changed=matches[0]["price"] != old["price"],
                                  previous_price=old["price"]))
        else:
            refreshed.append(dict(old, unavailable=True, price_changed=False, previous_price=None))
    return refreshed


def send_telegram(messages, message_id=None, snapshot=None):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise ValueError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
    for message in messages:
        payload = {"chat_id": chat, "text": message, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if snapshot is not None:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Обновить цены", "callback_data": "refresh_prices"}]]}
        if message_id is not None:
            payload["message_id"] = message_id
        method = "editMessageText" if message_id is not None else "sendMessage"
        result = request_json(f"https://api.telegram.org/bot{token}/{method}", payload=payload)
        if not result.get("ok"):
            raise RuntimeError("Telegram отклонил сообщение")
        if snapshot is not None:
            saved_id = message_id if message_id is not None else result["result"]["message_id"]
            with sqlite3.connect(ROOT / "data" / "messages.sqlite3") as db:
                db.execute("CREATE TABLE IF NOT EXISTS messages (chat TEXT, id INTEGER, snapshot TEXT, PRIMARY KEY(chat,id))")
                db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?)", (chat, saved_id, json.dumps(snapshot)))


def latest_message(chat):
    path = ROOT / "data" / "messages.sqlite3"
    if not path.exists():
        return None
    with sqlite3.connect(path) as db:
        saved = db.execute("SELECT id, snapshot FROM messages WHERE chat=? ORDER BY id DESC LIMIT 1", (str(chat),)).fetchone()
    return (saved[0], json.loads(saved[1])) if saved else None


def mark_current_prices(previous, current):
    matched = {row["key"]: row for row in refresh_offers(previous, current) if not row.get("unavailable")}
    return [matched.get(row["key"], row) for row in current]


def demo_offers(cfg):
    return [dict(origin=origin, destination=cfg["destination"],
                 origin_airport=origin, destination_airport=cfg["destination"],
                 departure_at=cfg["departure_start"] + "T12:00:00+05:00",
                 price=25000 + index * 3500, transfers=1, airline="DEMO", flight_number="000",
                 link=f"/search/{origin}1111CXR1?t=XX123{origin}CAN{cfg['destination']}_demo") for index, origin in enumerate(cfg["origins"])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--demo", action="store_true", help="Вымышленные данные, без сети и сохранения истории")
    parser.add_argument("--send", action="store_true", help="Отправить реальную подборку в Telegram")
    parser.add_argument("--watch", action="store_true", help="Повторять по расписанию")
    parser.add_argument("--manual", action="store_true", help="Ручной запрос: отправить подборку и ночью")
    parser.add_argument("--edit-message-id", type=int, help="Обновить существующую подборку")
    parser.add_argument("--update-latest", action="store_true", help="Обновлять последнюю подборку вместо новой отправки")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(ROOT / ".env")
    if args.demo and (args.send or args.watch):
        parser.error("Деморежим нельзя совмещать с --send или --watch")
    try:
        if args.send:
            (ROOT / "data").mkdir(exist_ok=True)
            run_lock = (ROOT / "data" / "search.lock").open("a")
            fcntl.flock(run_lock, fcntl.LOCK_EX)
        cfg = load_config(args.config)
        previous_snapshot = None
        if args.edit_message_id:
            with sqlite3.connect(ROOT / "data" / "messages.sqlite3") as db:
                saved = db.execute("SELECT snapshot FROM messages WHERE chat=? AND id=?",
                                   (os.environ.get("TELEGRAM_CHAT_ID"), args.edit_message_id)).fetchone()
            if not saved:
                raise ValueError("Исходная подборка не найдена")
            previous_snapshot = json.loads(saved[0])
            cfg = previous_snapshot["config"]
        if not args.demo and not os.getenv("TRAVELPAYOUTS_TOKEN"):
            raise ValueError("Заполните TRAVELPAYOUTS_TOKEN в .env; для проверки без ключа: --demo")
        if args.send and not all(os.getenv(key) for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")):
            raise ValueError("Для отправки заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        while True:
            started = time.monotonic()
            try:
                rows, errors = (demo_offers(cfg), []) if args.demo else fetch_offers(cfg, os.environ["TRAVELPAYOUTS_TOKEN"])
                if errors and not rows:
                    raise RuntimeError("Не удалось получить данные: " + "; ".join(errors))
                selection_cfg = dict(cfg, max_price=None) if previous_snapshot else cfg
                offers = select_offers(rows, selection_cfg)
                if previous_snapshot:
                    offers = refresh_offers(previous_snapshot["offers"], offers)
                previous = {}
                if not args.demo:
                    with open_database(ROOT / "data" / "prices.sqlite3") as db:
                        previous = record_offers(db, offers, cfg["currency"])
                        annotate_minimums(db, offers, cfg["currency"])
                messages = render_messages(offers, cfg, previous, errors, args.demo)
                for message in messages:
                    print(message, flush=True)
                for error in errors:
                    LOG.warning("%s", error)
                if args.send:
                    to_send, quiet = (offers, False) if args.manual or args.edit_message_id else notification_offers(offers, cfg)
                    if quiet and not to_send:
                        LOG.info("Ночной режим: билетов дешевле порога нет, отправка пропущена")
                    else:
                        message_id = args.edit_message_id
                        if args.update_latest:
                            latest = latest_message(os.environ["TELEGRAM_CHAT_ID"])
                            if latest:
                                message_id, old_snapshot = latest
                                to_send = mark_current_prices(old_snapshot["offers"], to_send)
                        rendered = render_messages(to_send, cfg, previous, errors)
                        displayed = rendered[0].count('<a href=')
                        snapshot = {"config": cfg, "offers": digest_offers(to_send, cfg)[:displayed]}
                        send_telegram(rendered, message_id=message_id, snapshot=snapshot)
                        LOG.info("Подборка обновлена в Telegram" if message_id is not None else "Подборка отправлена в Telegram")
            except (RuntimeError, ValueError, OSError) as exc:
                LOG.error("%s", exc)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            time.sleep(max(1, cfg["interval_seconds"] - (time.monotonic() - started)))
    except (ValueError, KeyError, OSError) as exc:
        LOG.error("Ошибка настройки: %s", exc)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Мониторинг остановлен.")
