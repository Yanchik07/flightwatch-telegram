"""Telegram command listener for the configured chat only."""

import logging
import os
import sqlite3
import subprocess
import sys
import time

import flightwatch as fw

LOG = logging.getLogger("flightwatch.bot")


def is_search(update, chat_id, username):
    message = update.get("message", {})
    if str(message.get("chat", {}).get("id", "")) != str(chat_id):
        return False
    text = message.get("text", "").strip()
    if not text:
        return False
    command = text.split()[0].lower()
    return command in ("/search", f"/search@{username.lower()}")


def refresh_message_id(update, chat_id):
    callback = update.get("callback_query", {})
    message = callback.get("message", {})
    if (callback.get("data") == "refresh_prices"
            and str(message.get("chat", {}).get("id", "")) == str(chat_id)):
        return message.get("message_id")
    return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fw.load_env(fw.ROOT / ".env")
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    endpoint = f"https://api.telegram.org/bot{token}/"
    username = fw.request_json(endpoint + "getMe")["result"]["username"]
    (fw.ROOT / "data").mkdir(exist_ok=True)
    db = sqlite3.connect(fw.ROOT / "data" / "bot.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, offset INTEGER)")
    stored = db.execute("SELECT offset FROM state WHERE id=1").fetchone()
    offset = stored[0] if stored else 0
    LOG.info("Обработчик /search запущен")
    while True:
        try:
            result = fw.request_json(endpoint + "getUpdates", payload={
                "offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]})
            if not result.get("ok"):
                raise RuntimeError("Не удалось получить команды Telegram")
            updates = result.get("result", [])
            requested = any(is_search(update, chat, username) for update in updates)
            refresh_ids = set()
            for update in updates:
                message_id = refresh_message_id(update, chat)
                callback = update.get("callback_query")
                if callback:
                    fw.request_json(endpoint + "answerCallbackQuery", payload={
                        "callback_query_id": callback["id"],
                        "text": "Обновляю данные Aviasales…" if message_id else "Эта кнопка недоступна в этом чате"})
                if message_id:
                    refresh_ids.add(message_id)
            if updates:
                offset = max(update["update_id"] for update in updates) + 1
                with db:
                    db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (offset,))
            # Several queued requests share one search and one digest.
            if requested:
                LOG.info("Получена команда /search")
                process = subprocess.run([sys.executable, str(fw.ROOT / "flightwatch.py"),
                                          "--send", "--manual"], timeout=1200, check=False)
                if process.returncode:
                    fw.send_telegram(["Поиск не завершён из-за ошибки. Попробуйте /search позже."])
            for message_id in refresh_ids:
                LOG.info("Обновление сообщения %s", message_id)
                process = subprocess.run([sys.executable, str(fw.ROOT / "flightwatch.py"),
                                          "--send", "--manual", "--edit-message-id", str(message_id)],
                                         timeout=1200, check=False)
                if process.returncode:
                    LOG.error("Не удалось обновить сообщение %s", message_id)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            LOG.error("Ошибка обработки команд; повтор через 10 секунд")
            time.sleep(10)


if __name__ == "__main__":
    main()
