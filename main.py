from __future__ import annotations

import argparse
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "payfornothing.db"
LOG_PATH = BASE_DIR / "bot.log"
PAGE_SIZE = 10
MESSAGE_LIMIT = 3900
DEFAULT_HEALTH_PORT = 8080

BTN_BUY = "🛒 Купить ничего"
BTN_SHOP = "🏪 Магазин"
BTN_TOP = "🏆 Топ легенд"
BTN_PROS = "📋 Список профи"
BTN_STATS = "📊 Статистика"
BTN_CABINET = "👤 Личный кабинет"
BTN_ABOUT = "ℹ️ О сервисе"
BTN_RULES = "📜 Правила"
BTN_INVITE = "🤝 Позвать друга"
BTN_ADMIN = "🛠️ Админка"
BTN_QUEUE = "💸 Очередь оплат"
BTN_PAID = "✅ Я платил"
BTN_BACK_TO_SHOP = "↩️ Вернуться в магазин"
BTN_APPROVE = "✅ Подтвердить"
BTN_REJECT = "❌ Отклонить"
BTN_REFRESH_STATS = "🔄 Обновить статистику сейчас"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def format_rub(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def human_name(first_name: str | None, last_name: str | None, username: str | None, user_id: int) -> str:
    parts = [first_name or "", last_name or ""]
    full_name = " ".join(part.strip() for part in parts if part and part.strip()).strip()
    if full_name:
        return full_name
    if username:
        return f"@{username}"
    return f"ID {user_id}"


def public_name(username: str | None, first_name: str | None, user_id: int) -> str:
    if username:
        return f"@{username}"
    if first_name:
        return first_name.strip()
    return f"Участник {user_id}"


def admin_name_from_row(row: sqlite3.Row) -> str:
    return human_name(row["first_name"], row["last_name"], row["username"], int(row["user_id"]))


def format_local_dt(value: str | None, tz: ZoneInfo) -> str:
    dt = parse_dt(value)
    if not dt:
        return "не задано"
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def build_reply_keyboard(rows: list[list[str]]) -> dict[str, Any]:
    return {
        "keyboard": [[{"text": item} for item in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def build_inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": callback} for text, callback in row]
            for row in rows
        ]
    }


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            chunks.append(current.rstrip())
        if len(line) <= limit:
            current = line
            continue
        start = 0
        while start < len(line):
            piece = line[start : start + limit]
            chunks.append(piece.rstrip())
            start += limit
        current = ""
    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]


def plan_icon(plan: "Plan") -> str:
    return {
        "basic_void": "🪙",
        "pro_void": "💠",
        "legend_void": "👑",
    }.get(plan.code, "✨")


@dataclass(slots=True)
class Plan:
    code: str
    title: str
    price_rub: int
    tagline: str
    confirmation_text: str


@dataclass(slots=True)
class Config:
    bot_token: str
    bot_name: str
    bot_username: str
    card_number: str
    card_holder: str
    support_contact: str
    support_text: str
    timezone: str
    admin_ids: set[int]
    admin_bootstrap_secret: str
    stats_broadcast_hours: int
    plans: dict[str, Plan]

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))

        bot_token = os.environ.get("PAYFORNOTHING_BOT_TOKEN", str(raw.get("bot_token", "")).strip())
        card_number = os.environ.get("PAYFORNOTHING_CARD_NUMBER", str(raw.get("card_number", "")).strip())
        support_contact = os.environ.get("PAYFORNOTHING_SUPPORT_CONTACT", str(raw.get("support_contact", "")).strip())
        admin_bootstrap_secret = os.environ.get(
            "PAYFORNOTHING_ADMIN_SECRET",
            str(raw.get("admin_bootstrap_secret", "")).strip(),
        )
        if not bot_token:
            raise ValueError("Не заполнен bot_token в config.json или PAYFORNOTHING_BOT_TOKEN.")
        if not card_number:
            raise ValueError("Не заполнен card_number в config.json или PAYFORNOTHING_CARD_NUMBER.")

        plan_defaults = [
            {
                "code": "basic_void",
                "title": "Обычное nothing",
                "price_rub": 100,
                "tagline": "Минимальный вход в элиту абсурда.",
                "confirmation_text": (
                    "Ты выбрал обычное nothing.\n"
                    "Да, это реальная попытка заплатить 100 ₽ буквально за ничего.\n"
                    "Большинство бы уже закрыло бот. Ты пока держишься."
                ),
            },
            {
                "code": "pro_void",
                "title": "Продвинутое nothing",
                "price_rub": 500,
                "tagline": "Для тех, кто решил зайти дальше здравого смысла.",
                "confirmation_text": (
                    "Ты выбрал продвинутое nothing.\n"
                    "500 ₽ за ничего звучит как ошибка, но это уже почти искусство.\n"
                    "Остался один перевод до статуса профи."
                ),
            },
            {
                "code": "legend_void",
                "title": "Легендарное nothing",
                "price_rub": 1000,
                "tagline": "Максимальный уровень мемного абсурда.",
                "confirmation_text": (
                    "Ты выбрал легендарное nothing.\n"
                    "1000 ₽ за ничего уже не объясняют. Этим только гордятся.\n"
                    "После подтверждения ты официально войдешь в зал легенд."
                ),
            },
        ]
        plan_rows = raw.get("plans") or plan_defaults
        plans: dict[str, Plan] = {}
        for item in plan_rows:
            plan = Plan(
                code=str(item["code"]).strip(),
                title=str(item["title"]).strip(),
                price_rub=int(item["price_rub"]),
                tagline=str(item.get("tagline", "")).strip(),
                confirmation_text=str(item.get("confirmation_text", "")).strip(),
            )
            plans[plan.code] = plan

        return cls(
            bot_token=bot_token,
            bot_name=str(raw.get("bot_name", "Pay For Nothing")).strip() or "Pay For Nothing",
            bot_username=str(raw.get("bot_username", "")).strip(),
            card_number=card_number,
            card_holder=str(raw.get("card_holder", "")).strip(),
            support_contact=support_contact,
            support_text=str(
                raw.get(
                    "support_text",
                    "Если застрял на оплате или хочешь обсудить природу пустоты, напиши администратору.",
                )
            ).strip(),
            timezone=str(raw.get("timezone", "Europe/Moscow")).strip() or "Europe/Moscow",
            admin_ids={int(item) for item in raw.get("admin_ids", [])},
            admin_bootstrap_secret=admin_bootstrap_secret,
            stats_broadcast_hours=max(1, int(raw.get("stats_broadcast_hours", 24))),
            plans=plans,
        )


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, payload: dict[str, Any] | None = None, retries: int = 2) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="ignore")
            parsed = {}
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                pass
            retry_after = parsed.get("parameters", {}).get("retry_after")
            if exc.code == 429 and retry_after and retries > 0:
                time.sleep(int(retry_after))
                return self._request(method, payload, retries=retries - 1)
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {raw}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Не удалось связаться с Telegram API: {exc}") from exc

        parsed = json.loads(raw)
        if not parsed.get("ok"):
            retry_after = parsed.get("parameters", {}).get("retry_after")
            if retry_after and retries > 0:
                time.sleep(int(retry_after))
                return self._request(method, payload, retries=retries - 1)
            raise RuntimeError(f"Telegram API error: {parsed}")
        return parsed.get("result")

    def delete_webhook(self) -> None:
        self._request("deleteWebhook", {"drop_pending_updates": False})

    def set_webhook(
        self,
        url: str,
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "url": url,
            "drop_pending_updates": drop_pending_updates,
        }
        if allowed_updates:
            payload["allowed_updates"] = allowed_updates
        self._request("setWebhook", payload)

    def get_me(self) -> dict[str, Any]:
        return self._request("getMe", {})

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._request("getUpdates", payload)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload)

    def send_photo(self, chat_id: int, photo: str, caption: str) -> dict[str, Any]:
        return self._request(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
            },
        )

    def send_document(self, chat_id: int, document: str, caption: str) -> dict[str, Any]:
        return self._request(
            "sendDocument",
            {
                "chat_id": chat_id,
                "document": document,
                "caption": caption,
            },
        )

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._request("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str, show_alert: bool = False) -> None:
        self._request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language_code TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    referred_by_user_id INTEGER,
                    referral_count INTEGER NOT NULL DEFAULT 0,
                    approved_total_amount INTEGER NOT NULL DEFAULT 0,
                    approved_payments_count INTEGER NOT NULL DEFAULT 0,
                    highest_tier_code TEXT,
                    highest_tier_title TEXT,
                    highest_tier_amount INTEGER NOT NULL DEFAULT 0,
                    last_payment_at TEXT,
                    last_approved_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    tier_code TEXT NOT NULL,
                    tier_title TEXT NOT NULL,
                    tier_amount_rub INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    card_number TEXT NOT NULL,
                    proof_text TEXT,
                    proof_file_id TEXT,
                    proof_kind TEXT,
                    created_at TEXT NOT NULL,
                    submitted_at TEXT,
                    approved_at TEXT,
                    rejected_at TEXT,
                    admin_user_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_users_total ON users(approved_total_amount DESC, approved_payments_count DESC);
                CREATE INDEX IF NOT EXISTS idx_users_last_approved ON users(last_approved_at DESC);
                CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, submitted_at DESC, approved_at DESC);
                CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, id DESC);
                """
            )

    def upsert_user(self, user: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO users (
                    user_id, username, first_name, last_name, language_code,
                    first_seen_at, last_seen_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    language_code = excluded.language_code,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user["id"]),
                    user.get("username"),
                    user.get("first_name"),
                    user.get("last_name"),
                    user.get("language_code"),
                    now,
                    now,
                    now,
                    now,
                ),
            )

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def link_referrer(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id:
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT referred_by_user_id FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None or row["referred_by_user_id"] is not None:
                return False
            self._conn.execute(
                "UPDATE users SET referred_by_user_id = ?, updated_at = ? WHERE user_id = ?",
                (referrer_id, utc_now().isoformat(), user_id),
            )
            referrer = self._conn.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)).fetchone()
            if referrer is not None:
                self._conn.execute(
                    """
                    UPDATE users
                    SET referral_count = referral_count + 1, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (utc_now().isoformat(), referrer_id),
                )
        return True

    def grant_admin(self, user: dict[str, Any]) -> None:
        self.upsert_user(user)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET is_admin = 1, updated_at = ? WHERE user_id = ?",
                (utc_now().isoformat(), int(user["id"])),
            )

    def is_admin(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row["is_admin"])

    def list_granted_admin_ids(self) -> set[int]:
        with self._lock:
            rows = self._conn.execute("SELECT user_id FROM users WHERE is_admin = 1").fetchall()
        return {int(row["user_id"]) for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def create_or_update_pending_payment(
        self,
        user: dict[str, Any],
        plan: Plan,
        card_number: str,
    ) -> sqlite3.Row:
        now = utc_now().isoformat()
        user_id = int(user["id"])
        with self._lock, self._conn:
            open_row = self._conn.execute(
                """
                SELECT * FROM payments
                WHERE user_id = ? AND status IN ('pending', 'submitted')
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if open_row is not None and open_row["status"] == "pending":
                payment_id = int(open_row["id"])
                self._conn.execute(
                    """
                    UPDATE payments
                    SET username = ?, first_name = ?, last_name = ?,
                        tier_code = ?, tier_title = ?, tier_amount_rub = ?, card_number = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        user.get("username"),
                        user.get("first_name"),
                        user.get("last_name"),
                        plan.code,
                        plan.title,
                        plan.price_rub,
                        card_number,
                        now,
                        payment_id,
                    ),
                )
            else:
                cursor = self._conn.execute(
                    """
                    INSERT INTO payments (
                        user_id, username, first_name, last_name,
                        tier_code, tier_title, tier_amount_rub, status, card_number, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        user_id,
                        user.get("username"),
                        user.get("first_name"),
                        user.get("last_name"),
                        plan.code,
                        plan.title,
                        plan.price_rub,
                        card_number,
                        now,
                    ),
                )
                payment_id = int(cursor.lastrowid)
        payment = self.get_payment(payment_id)
        if payment is None:
            raise RuntimeError("Не удалось создать заявку.")
        return payment

    def submit_latest_payment(
        self,
        user_id: int,
        proof_text: str | None,
        proof_file_id: str | None,
        proof_kind: str | None,
    ) -> sqlite3.Row | None:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            payment = self._conn.execute(
                """
                SELECT * FROM payments
                WHERE user_id = ? AND status IN ('pending', 'submitted')
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if payment is None:
                return None
            submitted_at = payment["submitted_at"] or now
            self._conn.execute(
                """
                UPDATE payments
                SET status = 'submitted',
                    proof_text = COALESCE(?, proof_text),
                    proof_file_id = COALESCE(?, proof_file_id),
                    proof_kind = COALESCE(?, proof_kind),
                    submitted_at = ?
                WHERE id = ?
                """,
                (proof_text, proof_file_id, proof_kind, submitted_at, int(payment["id"])),
            )
            payment_id = int(payment["id"])
        return self.get_payment(payment_id)

    def get_payment(self, payment_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()

    def get_latest_payment(self, user_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()

    def approve_payment(self, payment_id: int, admin_user_id: int) -> tuple[sqlite3.Row | None, bool]:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            payment = self._conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if payment is None:
                return None, False
            if payment["status"] != "submitted":
                return payment, False
            self._conn.execute(
                """
                UPDATE payments
                SET status = 'approved', approved_at = ?, admin_user_id = ?
                WHERE id = ?
                """,
                (now, admin_user_id, payment_id),
            )
            self._conn.execute(
                """
                UPDATE users
                SET approved_total_amount = approved_total_amount + ?,
                    approved_payments_count = approved_payments_count + 1,
                    highest_tier_code = CASE
                        WHEN highest_tier_amount < ? THEN ?
                        ELSE highest_tier_code
                    END,
                    highest_tier_title = CASE
                        WHEN highest_tier_amount < ? THEN ?
                        ELSE highest_tier_title
                    END,
                    highest_tier_amount = CASE
                        WHEN highest_tier_amount < ? THEN ?
                        ELSE highest_tier_amount
                    END,
                    last_payment_at = ?,
                    last_approved_at = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    int(payment["tier_amount_rub"]),
                    int(payment["tier_amount_rub"]),
                    payment["tier_code"],
                    int(payment["tier_amount_rub"]),
                    payment["tier_title"],
                    int(payment["tier_amount_rub"]),
                    int(payment["tier_amount_rub"]),
                    now,
                    now,
                    now,
                    int(payment["user_id"]),
                ),
            )
        return self.get_payment(payment_id), True

    def reject_payment(self, payment_id: int, admin_user_id: int) -> tuple[sqlite3.Row | None, bool]:
        now = utc_now().isoformat()
        with self._lock, self._conn:
            payment = self._conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if payment is None:
                return None, False
            if payment["status"] != "submitted":
                return payment, False
            self._conn.execute(
                """
                UPDATE payments
                SET status = 'rejected', rejected_at = ?, admin_user_id = ?
                WHERE id = ?
                """,
                (now, admin_user_id, payment_id),
            )
        return self.get_payment(payment_id), True

    def get_pending_submissions(self, limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM payments
                WHERE status = 'submitted'
                ORDER BY submitted_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return list(rows)

    def get_pending_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'submitted'").fetchone()[0])

    def get_total_users_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def get_paid_users_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM users WHERE approved_payments_count > 0"
                ).fetchone()[0]
            )

    def get_total_approved_amount(self) -> int:
        with self._lock:
            value = self._conn.execute(
                "SELECT COALESCE(SUM(approved_total_amount), 0) FROM users"
            ).fetchone()[0]
        return int(value or 0)

    def get_total_approved_payments(self) -> int:
        with self._lock:
            value = self._conn.execute(
                "SELECT COALESCE(SUM(approved_payments_count), 0) FROM users"
            ).fetchone()[0]
        return int(value or 0)

    def get_legendary_approved_payments(self) -> int:
        with self._lock:
            value = self._conn.execute(
                "SELECT COUNT(*) FROM payments WHERE status = 'approved' AND tier_code = ?",
                ("legend_void",),
            ).fetchone()[0]
        return int(value or 0)

    def get_top_users(self, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM users
                WHERE approved_total_amount > 0
                ORDER BY approved_total_amount DESC, approved_payments_count DESC, user_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return list(rows)

    def get_paid_users_recent(self, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM users
                WHERE approved_payments_count > 0
                ORDER BY last_approved_at DESC, approved_total_amount DESC, user_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return list(rows)

    def get_user_rank(self, user_id: int) -> int | None:
        with self._lock:
            current = self._conn.execute(
                """
                SELECT approved_total_amount, approved_payments_count
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None or int(current["approved_total_amount"]) <= 0:
                return None
            better = self._conn.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE approved_total_amount > ?
                   OR (
                        approved_total_amount = ?
                        AND approved_payments_count > ?
                   )
                   OR (
                        approved_total_amount = ?
                        AND approved_payments_count = ?
                        AND user_id < ?
                   )
                """,
                (
                    int(current["approved_total_amount"]),
                    int(current["approved_total_amount"]),
                    int(current["approved_payments_count"]),
                    int(current["approved_total_amount"]),
                    int(current["approved_payments_count"]),
                    user_id,
                ),
            ).fetchone()[0]
        return int(better) + 1

    def build_live_stats(self, now: datetime) -> dict[str, Any]:
        return {
            "total_users": self.get_total_users_count(),
            "paid_users": self.get_paid_users_count(),
            "approved_payments": self.get_total_approved_payments(),
            "legendary_approved_payments": self.get_legendary_approved_payments(),
            "approved_amount_rub": self.get_total_approved_amount(),
            "pending_payments": self.get_pending_count(),
            "top_users": self.get_top_users(limit=5, offset=0),
            "updated_at": now.isoformat(),
            "last_plan_refresh_at": self.get_setting("last_stats_refresh_at"),
        }

    def refresh_stats_snapshot(self, now: datetime) -> dict[str, Any]:
        stats = self.build_live_stats(now)
        self.set_setting(
            "stats_snapshot_json",
            json.dumps(
                {
                    "total_users": stats["total_users"],
                    "paid_users": stats["paid_users"],
                    "approved_payments": stats["approved_payments"],
                    "legendary_approved_payments": stats["legendary_approved_payments"],
                    "approved_amount_rub": stats["approved_amount_rub"],
                    "pending_payments": stats["pending_payments"],
                    "updated_at": stats["updated_at"],
                },
                ensure_ascii=False,
            ),
        )
        self.set_setting("last_stats_refresh_at", now.isoformat())
        return stats

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class BotApp:
    def __init__(self, config: Config, storage: Storage, api: TelegramClient) -> None:
        self.config = config
        self.storage = storage
        self.api = api
        self.local_tz = ZoneInfo(config.timezone)
        self.stop_event = threading.Event()
        self.scheduler = threading.Thread(target=self._scheduler_loop, name="stats-scheduler", daemon=True)
        self.bot_username = config.bot_username.lstrip("@")
        self.health_server: ThreadingHTTPServer | None = None
        self.health_thread: threading.Thread | None = None
        self.webhook_path = f"/telegram/{hashlib.sha256(config.bot_token.encode('utf-8')).hexdigest()[:24]}"

    def run(self) -> None:
        self.start_health_server()
        webhook_url = self.get_webhook_url()
        if webhook_url:
            self.api.set_webhook(
                webhook_url,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )
            logging.info("Webhook mode enabled: %s", webhook_url)
        else:
            self.api.delete_webhook()
            logging.info("Webhook mode disabled, using long polling.")
        try:
            me = self.api.get_me()
            username = str(me.get("username", "")).strip()
            if username:
                self.bot_username = username
        except Exception:
            logging.exception("Не удалось получить getMe при старте.")

        self.refresh_stats(force=False, notify_admins=False)
        self.scheduler.start()

        if webhook_url:
            while not self.stop_event.wait(1):
                pass
            return

        offset: int | None = None
        backoff = 2
        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(offset=offset, timeout=30)
                backoff = 2
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except KeyboardInterrupt:
                self.stop_event.set()
                raise
            except Exception:
                logging.exception("Ошибка polling loop")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def get_webhook_url(self) -> str:
        base_url = (
            os.environ.get("PAYFORNOTHING_WEBHOOK_BASE_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        )
        if not base_url:
            return ""
        return f"{base_url.rstrip('/')}{self.webhook_path}"

    def start_health_server(self) -> None:
        host = "0.0.0.0"
        port_raw = os.environ.get("PORT", "").strip()
        try:
            port = int(port_raw) if port_raw else DEFAULT_HEALTH_PORT
        except ValueError:
            port = DEFAULT_HEALTH_PORT

        app = self

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path not in {"/", "/health", "/healthz", "/ready", "/readyz"}:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                payload = {
                    "ok": True,
                    "service": "pay-for-nothing-bot",
                    "time": utc_now().isoformat(),
                    "stopping": app.stop_event.is_set(),
                }
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def do_POST(self) -> None:
                if self.path != app.webhook_path:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0

                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    update = json.loads(raw.decode("utf-8")) if raw else {}
                    if update:
                        app.handle_update(update)
                except Exception:
                    logging.exception("Webhook update processing failed")
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b'{"ok": false}')
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, format: str, *args: Any) -> None:
                return

        try:
            self.health_server = ThreadingHTTPServer((host, port), HealthHandler)
        except OSError:
            logging.exception("Не удалось поднять health server на %s:%s", host, port)
            return

        self.health_thread = threading.Thread(
            target=self.health_server.serve_forever,
            name="health-server",
            daemon=True,
        )
        self.health_thread.start()
        logging.info("Health server запущен на %s:%s", host, port)

    def _scheduler_loop(self) -> None:
        while not self.stop_event.wait(300):
            try:
                self.refresh_stats(force=False, notify_admins=True)
            except Exception:
                logging.exception("Ошибка фонового обновления статистики")

    def refresh_stats(self, force: bool, notify_admins: bool) -> bool:
        now = utc_now()
        last = parse_dt(self.storage.get_setting("last_stats_refresh_at"))
        due = force or last is None or now - last >= timedelta(hours=self.config.stats_broadcast_hours)
        if not due:
            return False

        stats = self.storage.refresh_stats_snapshot(now)
        if notify_admins:
            text = self.render_stats_text(stats, header="Ежедневное обновление статистики")
            for admin_id in sorted(self.admin_ids()):
                self.safe_send_text(admin_id, text)
        return True

    def admin_ids(self) -> set[int]:
        return set(self.config.admin_ids) | self.storage.list_granted_admin_ids()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_ids or self.storage.is_admin(user_id)

    def main_menu(self, user_id: int) -> dict[str, Any]:
        rows = [
            [BTN_BUY, BTN_SHOP],
            [BTN_TOP, BTN_PROS],
            [BTN_STATS, BTN_CABINET],
            [BTN_ABOUT, BTN_RULES],
            [BTN_INVITE],
        ]
        if self.is_admin(user_id):
            rows.append([BTN_ADMIN, BTN_QUEUE])
        return build_reply_keyboard(rows)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
            return
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message: dict[str, Any]) -> None:
        user = message.get("from")
        chat = message.get("chat")
        if not user or not chat:
            return

        self.storage.upsert_user(user)
        user_id = int(user["id"])
        chat_id = int(chat["id"])
        text = (message.get("text") or "").strip()

        if text.startswith("/grant_admin"):
            self.handle_grant_admin(chat_id, user, text)
            return

        if text.startswith("/start"):
            payload = ""
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                payload = parts[1].strip()
            self.handle_start(chat_id, user, payload)
            return

        if text in {"/help", "Помощь"}:
            self.show_about(chat_id, user_id)
            return

        if text in {"/stats", "Статистика", BTN_STATS}:
            self.show_stats(chat_id, user_id)
            return

        if text in {"/me", "Личный кабинет", BTN_CABINET}:
            self.show_cabinet(chat_id, user_id)
            return

        if text in {"/buy", "Купить ничего", "Магазин", BTN_BUY, BTN_SHOP}:
            self.show_shop(chat_id, user_id)
            return

        if text in {"Топ легенд", BTN_TOP}:
            self.show_top(chat_id, page=0)
            return

        if text in {"Список профи", BTN_PROS}:
            self.show_pros(chat_id, page=0)
            return

        if text in {"О сервисе", BTN_ABOUT}:
            self.show_about(chat_id, user_id)
            return

        if text in {"Правила", BTN_RULES}:
            self.show_rules(chat_id, user_id)
            return

        if text in {"Позвать друга", BTN_INVITE}:
            self.show_invite(chat_id, user_id)
            return

        if text in {"Админка", BTN_ADMIN, "/admin"}:
            self.show_admin_panel(chat_id, user_id)
            return

        if text in {"Очередь оплат", BTN_QUEUE, "/pending"}:
            self.show_pending_queue(chat_id, user_id)
            return

        if text in {"Я платил", BTN_PAID}:
            self.submit_proof(chat_id, user_id, proof_text=None, proof_file_id=None, proof_kind=None)
            return

        if message.get("photo"):
            photo = message["photo"][-1]
            caption = (message.get("caption") or "").strip() or None
            self.submit_proof(chat_id, user_id, caption, str(photo["file_id"]), "photo")
            return

        if message.get("document"):
            document = message["document"]
            caption = (message.get("caption") or "").strip() or None
            self.submit_proof(chat_id, user_id, caption, str(document["file_id"]), "document")
            return

        if text and not text.startswith("/"):
            if self.try_use_text_as_payment_note(chat_id, user_id, text):
                return

        self.send_welcome(chat_id, user_id)

    def handle_callback(self, callback_query: dict[str, Any]) -> None:
        data = callback_query.get("data", "")
        callback_id = callback_query["id"]
        user = callback_query.get("from")
        message = callback_query.get("message")
        if not user:
            self.api.answer_callback_query(callback_id, "Пользователь не определен.", show_alert=True)
            return
        self.storage.upsert_user(user)
        user_id = int(user["id"])
        chat_id = int(message["chat"]["id"]) if message else user_id

        if data.startswith("plan:"):
            plan_code = data.split(":", 1)[1]
            plan = self.config.plans.get(plan_code)
            if plan is None:
                self.api.answer_callback_query(callback_id, "Уровень пустоты не найден.", show_alert=True)
                return
            payment = self.storage.create_or_update_pending_payment(user, plan, self.config.card_number)
            self.show_plan_confirmation(chat_id, user_id, payment, plan)
            self.api.answer_callback_query(callback_id, "Выбор принят.")
            return

        if data == "shop":
            self.show_shop(chat_id, user_id)
            self.api.answer_callback_query(callback_id, "Открываю магазин пустоты.")
            return

        if data == "claim":
            self.submit_proof(chat_id, user_id, proof_text=None, proof_file_id=None, proof_kind=None)
            self.api.answer_callback_query(callback_id, "Заявка отправлена.")
            return

        if data.startswith("top:"):
            page = max(0, int(data.split(":", 1)[1]))
            self.show_top(chat_id, page=page, edit_message=message)
            self.api.answer_callback_query(callback_id, "Легенды на месте.")
            return

        if data.startswith("pros:"):
            page = max(0, int(data.split(":", 1)[1]))
            self.show_pros(chat_id, page=page, edit_message=message)
            self.api.answer_callback_query(callback_id, "Профи загружены.")
            return

        if data == "refresh_stats":
            if not self.is_admin(user_id):
                self.api.answer_callback_query(callback_id, "Только для админа.", show_alert=True)
                return
            self.refresh_stats(force=True, notify_admins=False)
            self.show_admin_panel(chat_id, user_id)
            self.api.answer_callback_query(callback_id, "Статистика обновлена.")
            return

        if data.startswith("approve:"):
            payment_id = int(data.split(":", 1)[1])
            self.process_admin_decision(callback_id, user, message, payment_id, approve=True)
            return

        if data.startswith("reject:"):
            payment_id = int(data.split(":", 1)[1])
            self.process_admin_decision(callback_id, user, message, payment_id, approve=False)
            return

        self.api.answer_callback_query(callback_id, "Неизвестное действие.", show_alert=True)

    def handle_start(self, chat_id: int, user: dict[str, Any], payload: str) -> None:
        user_id = int(user["id"])
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload.split("_", 1)[1])
            except ValueError:
                referrer_id = 0
            if referrer_id:
                linked = self.storage.link_referrer(user_id, referrer_id)
                if linked:
                    self.safe_send_text(
                        referrer_id,
                        f"По твоей ссылке зашел новый кандидат в элиту nothing: {human_name(user.get('first_name'), user.get('last_name'), user.get('username'), user_id)}",
                    )
        self.send_welcome(chat_id, user_id)

    def handle_grant_admin(self, chat_id: int, user: dict[str, Any], text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not self.config.admin_bootstrap_secret:
            self.safe_send_text(
                chat_id,
                "Команда выглядит так: /grant_admin <секрет>. Секрет лежит в config.json.",
                reply_markup=self.main_menu(int(user["id"])),
            )
            return
        if parts[1].strip() != self.config.admin_bootstrap_secret:
            self.safe_send_text(chat_id, "Неверный секрет администратора.")
            return
        self.storage.grant_admin(user)
        self.safe_send_text(
            chat_id,
            "Права администратора выданы. Теперь тебе доступны админка и очередь оплат.",
            reply_markup=self.main_menu(int(user["id"])),
        )
        self.refresh_stats(force=True, notify_admins=False)

    def send_welcome(self, chat_id: int, user_id: int) -> None:
        text = (
            f"Добро пожаловать в {self.config.bot_name}.\n\n"
            "Ты попал в самый честный сервис в Telegram.\n"
            "Здесь нет скрытых функций, нет пользы и нет смысла.\n"
            "Ты можешь добровольно заплатить за ничего и получить в ответ ровно ничего.\n\n"
            "Никаких бонусов. Никаких товаров. Никакой обещанной выгоды.\n"
            "Только факт участия, мемный статус и попадание в статистику.\n\n"
            "Выбери, что хочешь сделать."
        )
        self.safe_send_text(chat_id, text, reply_markup=self.main_menu(user_id))

    def show_shop(self, chat_id: int, user_id: int) -> None:
        lines = [
            "🛒 Выбери уровень пустоты:",
            "",
        ]
        keyboard_rows: list[list[tuple[str, str]]] = []
        for plan in self.config.plans.values():
            lines.append(f"{plan_icon(plan)} {plan.title} — {format_rub(plan.price_rub)}")
            lines.append(f"  {plan.tagline}")
            keyboard_rows.append([(f"{plan_icon(plan)} {plan.title} · {format_rub(plan.price_rub)}", f"plan:{plan.code}")])
        lines.extend(
            [
                "",
                "Да, все цены уже в рублях.",
                "Да, смысл по-прежнему отсутствует.",
            ]
        )
        self.safe_send_text(
            chat_id,
            "\n".join(lines),
            reply_markup=build_inline_keyboard(keyboard_rows),
        )

    def show_plan_confirmation(self, chat_id: int, user_id: int, payment: sqlite3.Row, plan: Plan) -> None:
        lines = [
            plan.confirmation_text,
            "",
            f"Твой выбор: {plan.title}",
            f"Сумма: {format_rub(plan.price_rub)}",
            "",
            f"Переведи деньги на карту: {payment['card_number']}",
        ]
        if self.config.card_holder:
            lines.append(f"Получатель: {self.config.card_holder}")
        lines.extend(
            [
                "",
                f"После перевода нажми «{BTN_PAID}» и отправь сюда скриншот, фото чека или сумму платежа.",
                "Бот никогда не просит CVV, SMS-коды, пароли или seed-фразы.",
            ]
        )
        markup = build_inline_keyboard(
            [
                [(BTN_PAID, "claim")],
                [(BTN_BACK_TO_SHOP, "shop")],
            ]
        )
        self.safe_send_text(chat_id, "\n".join(lines), reply_markup=markup)
        self.safe_send_text(
            chat_id,
            "Личный кабинет и общая статистика доступны в главном меню.",
            reply_markup=self.main_menu(user_id),
        )

    def submit_proof(
        self,
        chat_id: int,
        user_id: int,
        proof_text: str | None,
        proof_file_id: str | None,
        proof_kind: str | None,
    ) -> None:
        payment = self.storage.submit_latest_payment(user_id, proof_text, proof_file_id, proof_kind)
        if payment is None:
            self.safe_send_text(chat_id, f"Сначала выбери уровень пустоты через кнопку «{BTN_BUY}».")
            return
        self.safe_send_text(
            chat_id,
            "Заявка на оплату отправлена администратору. После подтверждения ты попадешь в статистику клуба nothing.",
        )
        self.notify_admins_about_submission(payment)

    def try_use_text_as_payment_note(self, chat_id: int, user_id: int, text: str) -> bool:
        latest = self.storage.get_latest_payment(user_id)
        if latest is None or latest["status"] not in {"pending", "submitted"}:
            return False
        payment = self.storage.submit_latest_payment(user_id, proof_text=text, proof_file_id=None, proof_kind="text")
        if payment is None:
            return False
        self.safe_send_text(
            chat_id,
            "Комментарий к оплате сохранен и передан администратору.",
        )
        self.notify_admins_about_submission(payment)
        return True

    def notify_admins_about_submission(self, payment: sqlite3.Row) -> None:
        admins = sorted(self.admin_ids())
        if not admins:
            logging.info("Нет администраторов для обработки заявки #%s", payment["id"])
            return
        detail = self.render_payment_submission(payment)
        buttons = build_inline_keyboard(
            [[(BTN_APPROVE, f"approve:{payment['id']}"), (BTN_REJECT, f"reject:{payment['id']}")]]
        )
        proof_caption = f"Подтверждение оплаты по заявке #{payment['id']}"
        for admin_id in admins:
            try:
                if payment["proof_file_id"] and payment["proof_kind"] == "photo":
                    self.api.send_photo(admin_id, str(payment["proof_file_id"]), proof_caption)
                elif payment["proof_file_id"] and payment["proof_kind"] == "document":
                    self.api.send_document(admin_id, str(payment["proof_file_id"]), proof_caption)
                self.api.send_message(admin_id, detail, reply_markup=buttons)
            except Exception:
                logging.exception("Не удалось отправить заявку админу %s", admin_id)

    def show_stats(self, chat_id: int, user_id: int) -> None:
        stats = self.storage.build_live_stats(utc_now())
        self.safe_send_text(chat_id, self.render_stats_text(stats, header="📊 Статистика NothingBot:"), reply_markup=self.main_menu(user_id))

    def show_top(self, chat_id: int, page: int, edit_message: dict[str, Any] | None = None) -> None:
        offset = page * PAGE_SIZE
        rows = self.storage.get_top_users(limit=PAGE_SIZE, offset=offset)
        total = self.storage.get_paid_users_count()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = ["🏆 Топ легенд", ""]
        if not rows:
            lines.append("Пока здесь пусто. Легенды еще не появились.")
        else:
            for index, row in enumerate(rows, start=offset + 1):
                badge = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"{index}."
                lines.append(
                    f"{badge} {public_name(row['username'], row['first_name'], int(row['user_id']))} — "
                    f"{format_rub(int(row['approved_total_amount']))} "
                    f"({int(row['approved_payments_count'])} оплат)"
                )
        lines.extend(["", f"Страница {page + 1} из {pages}"])
        buttons: list[tuple[str, str]] = []
        if page > 0:
            buttons.append(("← Назад", f"top:{page - 1}"))
        if page + 1 < pages:
            buttons.append(("Дальше →", f"top:{page + 1}"))
        markup = build_inline_keyboard([buttons]) if buttons else None
        self.send_or_edit_text(chat_id, "\n".join(lines), markup, edit_message)

    def show_pros(self, chat_id: int, page: int, edit_message: dict[str, Any] | None = None) -> None:
        offset = page * PAGE_SIZE
        rows = self.storage.get_paid_users_recent(limit=PAGE_SIZE, offset=offset)
        total = self.storage.get_paid_users_count()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = ["📋 Список профи", ""]
        if not rows:
            lines.append("Пока никто не прошел проверку оплат.")
        else:
            for row in rows:
                lines.append(
                    f"• {public_name(row['username'], row['first_name'], int(row['user_id']))} — "
                    f"{row['highest_tier_title'] or 'Участник nothing'} · "
                    f"{format_rub(int(row['approved_total_amount']))}"
                )
        lines.extend(["", f"Страница {page + 1} из {pages}"])
        buttons: list[tuple[str, str]] = []
        if page > 0:
            buttons.append(("← Назад", f"pros:{page - 1}"))
        if page + 1 < pages:
            buttons.append(("Дальше →", f"pros:{page + 1}"))
        markup = build_inline_keyboard([buttons]) if buttons else None
        self.send_or_edit_text(chat_id, "\n".join(lines), markup, edit_message)

    def show_about(self, chat_id: int, user_id: int) -> None:
        lines = [
            "ℹ️ О сервисе",
            "",
            "Это ироничный бот про честную бессмысленную оплату.",
            "Здесь нет товара, нет услуги и нет скрытого бонуса.",
            "Ты платишь буквально за ничего: ради мема, статуса, участия и социального эксперимента.",
            "Факт перевода и попадание в статистику и есть весь продукт этой шутки.",
            "",
            self.config.support_text,
        ]
        if self.config.support_contact:
            lines.append(f"Контакт: {self.config.support_contact}")
        self.safe_send_text(chat_id, "\n".join(lines), reply_markup=self.main_menu(user_id))

    def show_rules(self, chat_id: int, user_id: int) -> None:
        lines = [
            "📜 Правила",
            "",
            "1. Платеж добровольный и символический.",
            "2. Бот не обещает доход, выгоду, доступ, товар или реальную услугу.",
            "3. Оплата подтверждается администратором вручную.",
            "4. Для подтверждения достаточно скрина, фото чека или комментария с суммой.",
            "5. Бот никогда не просит CVV, SMS-коды, пароли, seed-фразы и другие секретные данные.",
            "6. Если ошибся с переводом или есть спорный случай, пиши в поддержку.",
        ]
        if self.config.support_contact:
            lines.append(f"Поддержка: {self.config.support_contact}")
        self.safe_send_text(chat_id, "\n".join(lines), reply_markup=self.main_menu(user_id))

    def show_invite(self, chat_id: int, user_id: int) -> None:
        username = self.bot_username or self.config.bot_username.lstrip("@")
        if not username:
            self.safe_send_text(chat_id, "Ссылку пока не могу собрать: у бота не определен username.")
            return
        link = f"https://t.me/{username}?start=ref_{user_id}"
        user_row = self.storage.get_user(user_id)
        invited = int(user_row["referral_count"]) if user_row else 0
        text = "\n".join(
            [
                "🤝 Позвать друга",
                "",
                "Отправь эту ссылку тому, кто тоже готов платить за ничего:",
                link,
                "",
                f"По твоей ссылке уже пришло: {invited}",
                "Награды нет. Только уважение и статистика.",
            ]
        )
        self.safe_send_text(chat_id, text, reply_markup=self.main_menu(user_id))

    def show_cabinet(self, chat_id: int, user_id: int) -> None:
        user_row = self.storage.get_user(user_id)
        latest = self.storage.get_latest_payment(user_id)
        if user_row is None:
            self.send_welcome(chat_id, user_id)
            return

        rank = self.storage.get_user_rank(user_id)
        lines = [
            "👤 Личный кабинет",
            "",
            f"Профиль: {public_name(user_row['username'], user_row['first_name'], user_id)}",
            f"Ты в боте с: {format_local_dt(user_row['first_seen_at'], self.local_tz)}",
            f"Подтвержденных оплат: {int(user_row['approved_payments_count'])}",
            f"Потрачено на nothing: {format_rub(int(user_row['approved_total_amount']))}",
            f"Текущий статус: {user_row['highest_tier_title'] or 'Наблюдатель'}",
            f"Место в топе: #{rank}" if rank else "Место в топе: пока вне рейтинга",
            f"Приглашено друзей: {int(user_row['referral_count'])}",
        ]
        if latest is not None:
            status_map = {
                "pending": "тариф выбран, перевод еще не подтвержден",
                "submitted": "заявка на проверке",
                "approved": "оплата подтверждена",
                "rejected": "заявка отклонена",
            }
            lines.extend(
                [
                    "",
                    "Последняя заявка:",
                    f"• {latest['tier_title']} — {format_rub(int(latest['tier_amount_rub']))}",
                    f"• Статус: {status_map.get(str(latest['status']), str(latest['status']))}",
                ]
            )
        self.safe_send_text(chat_id, "\n".join(lines), reply_markup=self.main_menu(user_id))

    def show_admin_panel(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            self.safe_send_text(chat_id, f"Кнопка «{BTN_ADMIN}» доступна только администратору.")
            return
        stats = self.storage.build_live_stats(utc_now())
        text = "\n".join(
            [
                "🛠️ Админка",
                "",
                f"Всего пользователей: {stats['total_users']}",
                f"Оплативших пользователей: {stats['paid_users']}",
                f"Подтвержденных оплат: {stats['approved_payments']}",
                f"Сумма оплат: {format_rub(int(stats['approved_amount_rub']))}",
                f"На проверке: {stats['pending_payments']}",
                f"Последний плановый апдейт: {format_local_dt(stats['last_plan_refresh_at'], self.local_tz)}",
            ]
        )
        markup = build_inline_keyboard([[(BTN_REFRESH_STATS, "refresh_stats")]])
        self.safe_send_text(chat_id, text, reply_markup=markup)
        self.safe_send_text(chat_id, f"Для ручной очереди нажми кнопку «{BTN_QUEUE}».", reply_markup=self.main_menu(user_id))

    def show_pending_queue(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            self.safe_send_text(chat_id, f"Кнопка «{BTN_QUEUE}» доступна только администратору.")
            return
        rows = self.storage.get_pending_submissions(limit=10, offset=0)
        if not rows:
            self.safe_send_text(chat_id, "Очередь пуста. Мир пока не настолько иррационален.")
            return
        self.safe_send_text(chat_id, f"В очереди {len(rows)} последних заявок. Отправляю карточки на проверку.")
        for row in rows:
            buttons = build_inline_keyboard(
                [[(BTN_APPROVE, f"approve:{row['id']}"), (BTN_REJECT, f"reject:{row['id']}")]]
            )
            self.safe_send_text(chat_id, self.render_payment_submission(row), reply_markup=buttons)

    def process_admin_decision(
        self,
        callback_id: str,
        admin_user: dict[str, Any],
        message: dict[str, Any] | None,
        payment_id: int,
        approve: bool,
    ) -> None:
        admin_id = int(admin_user["id"])
        if not self.is_admin(admin_id):
            self.api.answer_callback_query(callback_id, "Это действие доступно только администратору.", show_alert=True)
            return

        if approve:
            payment, changed = self.storage.approve_payment(payment_id, admin_id)
            if payment is None:
                self.api.answer_callback_query(callback_id, "Заявка не найдена.", show_alert=True)
                return
            if not changed:
                self.api.answer_callback_query(callback_id, "Эта заявка уже обработана.", show_alert=True)
                return
            self.api.answer_callback_query(callback_id, "Оплата подтверждена.")
            self.refresh_stats(force=True, notify_admins=False)
            paid_user_id = int(payment["user_id"])
            self.safe_send_text(
                paid_user_id,
                self.render_user_approval(payment),
                reply_markup=self.main_menu(paid_user_id),
            )
            self.safe_send_text(
                paid_user_id,
                self.render_post_approval_update(paid_user_id),
                reply_markup=self.main_menu(paid_user_id),
            )
            result_text = self.render_admin_decision(payment, approved=True, admin_user=admin_user)
        else:
            payment, changed = self.storage.reject_payment(payment_id, admin_id)
            if payment is None:
                self.api.answer_callback_query(callback_id, "Заявка не найдена.", show_alert=True)
                return
            if not changed:
                self.api.answer_callback_query(callback_id, "Эта заявка уже обработана.", show_alert=True)
                return
            self.api.answer_callback_query(callback_id, "Заявка отклонена.")
            self.safe_send_text(
                int(payment["user_id"]),
                "Заявка не подтверждена. Проверь перевод и при необходимости отправь подтверждение заново.",
                reply_markup=self.main_menu(int(payment["user_id"])),
            )
            result_text = self.render_admin_decision(payment, approved=False, admin_user=admin_user)

        if message:
            try:
                self.api.edit_message_text(
                    int(message["chat"]["id"]),
                    int(message["message_id"]),
                    result_text,
                )
            except Exception:
                logging.exception("Не удалось обновить админское сообщение по заявке #%s", payment_id)

    def render_payment_submission(self, payment: sqlite3.Row) -> str:
        proof_state = "файл приложен" if payment["proof_file_id"] else "без файла"
        note = payment["proof_text"] or "без комментария"
        return "\n".join(
            [
                f"Заявка #{payment['id']}",
                f"Пользователь: {human_name(payment['first_name'], payment['last_name'], payment['username'], int(payment['user_id']))}",
                f"Username: @{payment['username']}" if payment["username"] else "Username: не указан",
                f"ID: {payment['user_id']}",
                f"Уровень: {payment['tier_title']}",
                f"Сумма: {format_rub(int(payment['tier_amount_rub']))}",
                f"Создана: {format_local_dt(payment['created_at'], self.local_tz)}",
                f"Отправлена на проверку: {format_local_dt(payment['submitted_at'], self.local_tz)}",
                f"Подтверждение: {proof_state}",
                f"Комментарий: {note}",
            ]
        )

    def render_user_approval(self, payment: sqlite3.Row) -> str:
        return "\n".join(
            [
                "Оплата подтверждена.",
                f"Ты официально купил {payment['tier_title']} за {format_rub(int(payment['tier_amount_rub']))}.",
                "Да, ты действительно заплатил за ничего.",
                "Да, теперь это навсегда записано в статистике.",
            ]
        )

    def render_post_approval_update(self, user_id: int) -> str:
        user_row = self.storage.get_user(user_id)
        if user_row is None:
            return "Твоя оплата подтверждена и уже учтена в системе."

        rank = self.storage.get_user_rank(user_id)
        stats = self.storage.build_live_stats(utc_now())

        lines = [
            "Обновление после подтверждения",
            "",
            "Твои данные уже попали в списки.",
            f"Подтвержденных оплат: {int(user_row['approved_payments_count'])}",
            f"Всего потрачено на nothing: {format_rub(int(user_row['approved_total_amount']))}",
            f"Текущий статус: {user_row['highest_tier_title'] or 'Участник'}",
            f"Место в топе: #{rank}" if rank else "Место в топе: пока вне рейтинга",
            "",
            "Свежая статистика бота:",
            f"Всего пользователей: {stats['total_users']}",
            f"Оплативших: {stats['paid_users']}",
            f"Общая сумма: {format_rub(int(stats['approved_amount_rub']))}",
            f"Последнее обновление: {format_local_dt(stats['updated_at'], self.local_tz)}",
        ]
        return "\n".join(lines)

    def render_admin_decision(self, payment: sqlite3.Row, approved: bool, admin_user: dict[str, Any]) -> str:
        verdict = "подтверждена" if approved else "отклонена"
        return "\n".join(
            [
                f"Заявка #{payment['id']} {verdict}.",
                f"Пользователь: {human_name(payment['first_name'], payment['last_name'], payment['username'], int(payment['user_id']))}",
                f"Уровень: {payment['tier_title']}",
                f"Сумма: {format_rub(int(payment['tier_amount_rub']))}",
                f"Решение принял: {human_name(admin_user.get('first_name'), admin_user.get('last_name'), admin_user.get('username'), int(admin_user['id']))}",
            ]
        )

    def render_stats_text(self, stats: dict[str, Any], header: str) -> str:
        lines = [
            header,
            "",
            f"💸 Всего куплено: {stats['approved_payments']} раз",
            f"👥 Участников: {stats['paid_users']}",
            f"🏅 Легендарных покупок: {stats['legendary_approved_payments']}",
            "С каждым днем элита растет.",
            "Ты уже внутри или все еще думаешь?",
            "",
            f"🕒 Последнее обновление: {format_local_dt(stats.get('last_plan_refresh_at') or stats['updated_at'], self.local_tz)}",
            f"(Данные обновляются каждые {self.config.stats_broadcast_hours}ч)",
        ]
        return "\n".join(lines)

    def send_or_edit_text(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None,
        edit_message: dict[str, Any] | None,
    ) -> None:
        if edit_message is None:
            self.safe_send_text(chat_id, text, reply_markup=reply_markup)
            return
        try:
            self.api.edit_message_text(
                chat_id,
                int(edit_message["message_id"]),
                text,
                reply_markup=reply_markup,
            )
        except Exception:
            logging.exception("Не удалось изменить сообщение, отправляю новое.")
            self.safe_send_text(chat_id, text, reply_markup=reply_markup)

    def safe_send_text(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_text(text)
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            try:
                self.api.send_message(chat_id, chunk, reply_markup=markup)
            except Exception:
                logging.exception("Не удалось отправить сообщение в чат %s", chat_id)

    def run_self_check(self) -> None:
        stats = self.storage.build_live_stats(utc_now())
        print("OK")
        print(f"Название бота: {self.config.bot_name}")
        print(f"Уровней nothing: {len(self.config.plans)}")
        print(f"Всего пользователей в базе: {stats['total_users']}")
        print(f"Оплативших: {stats['paid_users']}")
        print(f"Сумма оплат: {stats['approved_amount_rub']}")


def setup_logging() -> None:
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pay For Nothing Telegram bot.")
    parser.add_argument("--check", action="store_true", help="Проверить конфиг и базу без запуска polling.")
    args = parser.parse_args()

    setup_logging()
    try:
        config = Config.load(CONFIG_PATH)
        storage = Storage(DB_PATH)
        api = TelegramClient(config.bot_token)
        app = BotApp(config, storage, api)
        if args.check:
            app.run_self_check()
            return 0
        logging.info("Бот запущен.")
        app.run()
        return 0
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")
        return 0
    except Exception:
        traceback.print_exc()
        logging.exception("Критическая ошибка при запуске")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
