"""
Полная однофайловая версия Telegram D-COINS бота. Python 3.12+.

Установка зависимостей (локально или Railway Build Command):
python -m pip install aiogram==3.31.0 aiosqlite==0.22.1 "cachetools>=5.5,<7" "python-dotenv>=1.0,<2" "pytz>=2025.2" "filelock>=3.18,<4"

Запуск / Railway Start Command: python bot.py
Если используется Dockerfile из прежнего архива, его нужно заменить сборкой,
копирующей bot.py, либо переключить сервис на Python/Railpack и убрать прежние
Dockerfile и railway.json. Старый Dockerfile не копирует этот файл в контейнер.
Обязательные переменные: BOT_TOKEN, ADMIN_ID, MAIN_CHAT_ID.
На Railway: Volume /data, DB_PATH=/data/activity.db, одна реплика.
Railway самостоятельно задаёт RAILWAY_VOLUME_MOUNT_PATH. HTTP healthcheck не нужен.
Существующую БД нужно сохранить и перенести до замены старого деплоя.
Сохраняются activity.db и activity.db.inbox; Volume не переносит старую БД автоматически.

Другие переменные: LOG_CHAT_ID, GAME_LOG_CHAT_ID, CHANNEL_CHAT_ID,
REQUIRED_CHANNEL, REQUIRED_CHANNEL_URL, PROMO_CHANNEL_ID.
Локально поддерживается .env; на Railway используйте Variables.

Обслуживание при остановленном боте:
python bot.py backup /путь/к/новой/резервной-копии.db
python bot.py check
python bot.py retry-update 12345

В этом файле объединены все модули проекта, Railway launcher и manage.py.
Другие Python-файлы проекта не требуются. SQLite-файлы создаются во время работы.
Таблицы призов и Gift ID сохранены; актуальность Gift ID требует проверки.
Платёжные и игровые операции последовательны. Реальные операции Telegram не тестировались.
"""

import argparse
import asyncio
import copy
import json
import logging
import math
import os
import secrets
import signal
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import wraps
from itertools import count as counter_sequence
from math import comb
from pathlib import Path
from random import Random, SystemRandom

import aiosqlite
import pytz
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetUpdates,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    Update,
)
from cachetools import TTLCache
from dotenv import load_dotenv
from filelock import FileLock

logger = logging.getLogger(__name__)


def configure_database(environ):
    mount = environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if not mount:
        raise RuntimeError("Подключите Railway Volume к сервису бота, например в /data.")
    volume = Path(mount).resolve()
    if not volume.is_dir():
        raise RuntimeError("Railway Volume не смонтирован. Проверьте Mount Path сервиса.")
    database = Path(environ.get("DB_PATH", "").strip() or str(volume / "activity.db")).resolve()
    if not database.is_relative_to(volume) or database == volume:
        raise RuntimeError(
            "DB_PATH должен указывать на файл внутри Railway Volume, например /data/activity.db."
        )
    environ["DB_PATH"] = str(database)
    return database


# ==================== CONFIG ====================


load_dotenv()
if (
    os.getenv("RAILWAY_ENVIRONMENT_ID")
    or os.getenv("RAILWAY_SERVICE_ID")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
):
    configure_database(os.environ)
TOKEN = os.getenv("BOT_TOKEN", "")
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0"))
GAME_LOG_CHAT_ID = int(os.getenv("GAME_LOG_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_CHAT_ID = int(os.getenv("CHANNEL_CHAT_ID", "0") or 0)
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@d_coins_channel")
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/d_coins_channel")
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", REQUIRED_CHANNEL)
COOLDOWN_SECONDS = 1
START_CHANCE = 0.1
STEP = 0.002
MAX_CHANCE = 100.0
GIFT_WIN_ROLL_MAX = 4000.0
BONUS_COOLDOWN = 43200
COINS_START = 10
COINS_PER_MSG = 1
COINS_VIP_PER_MSG = 2
COINS_BONUS = 20
COINS_VIP_BONUS = 30
COINS_BONUS_CD = 43200
CASINO_MIN_BET = 5
CASINO_TIMEOUT = 300
CASINO_BET_COOLDOWN = 10
CASE_OPEN_COOLDOWN = 5
GAME_BET_PRESETS = (100, 500, 1000, 2500, 5000, 10000, 50000, 100000)
JACKPOT_PERCENT = 2
JACKPOT_TICKET_STEP = 5000
JACKPOT_TICKET_LIMIT = 20
SCRATCH_SYMBOLS = ("🌈", "🔥", "🍓", "🍒", "🍋", "💎")
SCRATCH_WIN_CHANCE = 0.2
SCRATCH_MULTIPLIER = 3
LOTTERY_MULTIPLIERS = [3.0] + [2.5] + [2.0] * 2 + [1.5] * 4 + [1.0] * 2 + [0.5] * 2 + [0.0] * 13
SLOT_SYMBOLS = ("🍒", "🍋", "🍊", "🍇", "⭐", "💎", "🍉", "🔔")
ROULETTE_WHEEL = ("red",) * 16 + ("black",) * 16 + ("green",) * 5
PANDORA_COOLDOWN = 5 * 86400
PANDORA_REWARDS = (
    ("coins", 1000, 22),
    ("coins", 3000, 18),
    ("coins", 5000, 14),
    ("coins", 10000, 10),
    ("coins", 25000, 5),
    ("coins", 50000, 1),
    ("key", "school", 8),
    ("key", "student", 6),
    ("key", "blood", 5),
    ("key", "spider_man", 4),
    ("key", "pantera", 3),
    ("key", "excellent", 1),
    ("chance", 1.0, 2),
    ("gift", 15, 0.6),
    ("gift", 25, 0.25),
    ("gift", 50, 0.1),
    ("gift", 100, 0.04),
    ("premium", 1, 0.01),
)


def jackpot_day_key(now: float | None = None) -> str:
    stamp = time.time() if now is None else now
    return datetime.fromtimestamp(stamp, pytz.timezone("Europe/Moscow")).date().isoformat()


DUEL_MIN_BET = 5
DUEL_MAX_BET = 100000
DUEL_COOLDOWN = 10
DUEL_TIMEOUT = 300
EXCHANGE_CHANCE = 5000
EXCHANGE_GIFT_15 = 40000
EXCHANGE_GIFT_25 = 70000
EXCHANGE_GIFT_50 = 140000
EXCHANGE_GIFT_100 = 280000
EXCHANGE_PREMIUM_1_MONTH = 500000
STAR_DC_PACKAGES = {amount: amount // 100000 * 200 for amount in range(100000, 1000001, 100000)}
CASES = {
    "blood": {
        "title": "BLOOD",
        "price": 5000,
        "rewards": [
            ("coins", 300, 10),
            ("coins", 500, 12),
            ("coins", 700, 14),
            ("coins", 1000, 16),
            ("coins", 1500, 17),
            ("coins", 2500, 14),
            ("coins", 4000, 8),
            ("coins", 7000, 6),
            ("gift", 15, 2),
            ("gift", 25, 0.7),
            ("gift", 50, 0.3),
        ],
    },
    "pantera": {
        "title": "PANTERA",
        "price": 10000,
        "rewards": [
            ("coins", 1000, 16),
            ("coins", 5000, 21),
            ("coins", 6000, 21),
            ("coins", 8000, 18),
            ("coins", 9000, 13),
            ("coins", 12000, 6),
            ("coins", 15000, 3),
            ("gift", 15, 1),
            ("gift", 25, 0.5),
            ("gift", 50, 0.3),
            ("gift", 100, 0.2),
        ],
    },
    "spider_man": {
        "title": "SPIDER MAN",
        "price": 7500,
        "rewards": [
            ("coins", 500, 12),
            ("coins", 1000, 18),
            ("coins", 1500, 20),
            ("coins", 2500, 20),
            ("coins", 3500, 15),
            ("coins", 5000, 9),
            ("coins", 7500, 4),
            ("coins", 10000, 1),
            ("coins", 15000, 0.2),
            ("gift", 15, 0.6),
            ("gift", 25, 0.15),
            ("gift", 50, 0.05),
        ],
    },
    "school": {
        "title": "ШКОЛЬНЫЙ",
        "price": 3000,
        "rewards": [
            ("coins", 250, 22.3),
            ("coins", 500, 25.25),
            ("coins", 1000, 24),
            ("coins", 1500, 15),
            ("coins", 2500, 8),
            ("coins", 4000, 3.5),
            ("coins", 7000, 1.7),
            ("gift", 15, 0.2),
            ("gift", 25, 0.04),
            ("gift", 50, 0.01),
        ],
    },
    "student": {
        "title": "СТУДЕНЧЕСКИЙ",
        "price": 12000,
        "rewards": [
            ("coins", 1000, 22.4),
            ("coins", 3000, 22.3),
            ("coins", 5000, 20),
            ("coins", 7500, 15),
            ("coins", 10000, 10),
            ("coins", 15000, 6),
            ("coins", 25000, 2.5),
            ("coins", 40000, 1.4),
            ("gift", 15, 0.3),
            ("gift", 25, 0.07),
            ("gift", 50, 0.02),
            ("gift", 100, 0.01),
        ],
    },
    "excellent": {
        "title": "КЕЙС ОТЛИЧНИКА",
        "price": None,
        "key_only": True,
        "rewards": [
            ("coins", 3000, 18),
            ("coins", 8000, 20),
            ("coins", 12000, 20),
            ("coins", 20000, 15),
            ("coins", 30000, 10),
            ("coins", 45000, 7),
            ("coins", 70000, 4),
            ("coins", 100000, 2),
            ("gift", 15, 2),
            ("gift", 25, 1),
            ("gift", 50, 0.6),
            ("gift", 100, 0.4),
        ],
    },
}
ECONOMY_DEFAULTS = {
    "chance_price": EXCHANGE_CHANCE,
    "gift_15": EXCHANGE_GIFT_15,
    "gift_25": EXCHANGE_GIFT_25,
    "gift_50": EXCHANGE_GIFT_50,
    "gift_100": EXCHANGE_GIFT_100,
    "premium_1m": EXCHANGE_PREMIUM_1_MONTH,
}
ECONOMY = dict(ECONOMY_DEFAULTS)
BAN_MESSAGE = "🚫 Вы заблокированы и не можете участвовать в розыгрышах в боте."
POPOLNIT_AMOUNT = 50
GIFT_IDS = {
    5: ["5170145012310081615", "5170233102089322756"],
    10: ["5170250947678437525", "5168103777563050263"],
    15: [
        "5170144170496491616",
        "5170314324215857265",
        "5170564780938756245",
        "6028601630662853006",
    ],
    20: ["5168043875654172773", "5170690322832818290", "5170521118301225164"],
}
WIN_GIFT_IDS = ["5170233102089322756", "5170233102089322756"]
QUESTS = {
    "messages15": ("📚 Написать 15 сообщений", "messages", 15, 1500, 50),
    "messages30": ("✍️ Написать 30 сообщений", "messages", 30, 3000, 100),
    "bonus": ("🎁 Забрать ежедневный бонус", "bonus", 1, 1000, 30),
    "duels": ("⚔️ Сыграть 2 дуэли", "duels", 2, 2000, 75),
    "duelwin": ("🏆 Победить в дуэли", "duelwin", 1, 3000, 120),
    "games": ("🎰 Завершить 3 игры", "games", 3, 1500, 60),
    "gamewin": ("🍀 Выиграть в игре", "gamewin", 1, 2500, 100),
    "safe": ("💣 Открыть 5 безопасных клеток", "safe", 5, 2000, 80),
    "case": ("📦 Открыть кейс за DC", "case", 1, 3000, 150),
    "bets": ("💰 Сыграть на 10 000 DC", "bets", 10000, 4000, 180),
}
PATH_REWARDS = {
    1: [("coins", 500)],
    2: [("coins", 500)],
    3: [("school", 1)],
    4: [("coins", 750)],
    5: [("coins", 2000)],
    6: [("coins", 750)],
    7: [("school", 1)],
    8: [("coins", 1000)],
    9: [("coins", 1000)],
    10: [("coins", 3500)],
    11: [("coins", 1000)],
    12: [("school", 1)],
    13: [("coins", 1250)],
    14: [("coins", 1250)],
    15: [("coins", 3000)],
    16: [("student", 1)],
    17: [("coins", 1500)],
    18: [("coins", 1500)],
    19: [("school", 1)],
    20: [("coins", 5500)],
    21: [("coins", 1500)],
    22: [("coins", 1500)],
    23: [("school", 1)],
    24: [("coins", 2000)],
    25: [("coins", 5000)],
    26: [("coins", 2000)],
    27: [("student", 1)],
    28: [("coins", 2500)],
    29: [("coins", 2500)],
    30: [("coins", 5000)],
    31: [("coins", 2500)],
    32: [("school", 1)],
    33: [("coins", 3000)],
    34: [("coins", 3000)],
    35: [("coins", 7000)],
    36: [("student", 1)],
    37: [("coins", 3500)],
    38: [("coins", 3500)],
    39: [("school", 1)],
    40: [("gift", 15)],
    41: [("coins", 4000)],
    42: [("student", 1)],
    43: [("coins", 4000)],
    44: [("school", 1)],
    45: [("excellent", 1)],
    46: [("coins", 5000)],
    47: [("coins", 5000)],
    48: [("student", 1)],
    49: [("coins", 7000)],
    50: [("gift", 25), ("excellent", 1)],
}
SCHOOL_BOSSES = {
    1: {
        "name": "📚 Архимед Знаний",
        "max_hp": 5000000,
        "last_hit": 50000,
        "top_text": "🥇 NFT · 🥈 Premium · 🥉 подарок 100⭐",
    },
    2: {
        "name": "🌑 Магистр Забвений",
        "max_hp": 50000000,
        "last_hit": 250000,
        "top_text": "🥇 NFT · 🥈 NFT · 🥉 NFT",
    },
}
MAGISTER_SEAL_HP = 3000000
MAGISTER_SEAL_COUNT = 3
LOSS_SHARE_MIN_AMOUNT = 10000
GOLDEN_HOUR_SECONDS = 3600
GOLDEN_HOUR_DAMAGE_LIMIT = 100000
GOLDEN_HOUR_TOTAL_BONUS_DAMAGE_LIMIT = 100000
GOLDEN_HOUR_KNOWLEDGE_LIMIT = 500
GOLDEN_HOUR_GAMES_LIMIT = 20
MINES_GRID_SIZE = 25
MINES_COUNT = 8
MAX_BET = 1000000000


def validate_config():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан")
    if ADMIN_ID <= 0 or MAIN_CHAT_ID >= 0:
        raise ValueError("ADMIN_ID должен быть положительным, MAIN_CHAT_ID — ID группы")
    for value in (
        ADMIN_ID,
        MAIN_CHAT_ID,
        LOG_CHAT_ID,
        GAME_LOG_CHAT_ID,
        CHANNEL_CHAT_ID,
    ):
        if not -(2**63) < value < 2**63:
            raise ValueError("Telegram ID не помещается в SQLite INTEGER")


def economy_price(key: str) -> int:
    return int(ECONOMY[key])


# ==================== RANDOMNESS ====================


current_rng = ContextVar("rng", default=None)


class RandomSource:
    def __getattr__(self, name):
        return getattr(current_rng.get() or SystemRandom(), name)


random = RandomSource()


def begin_draw(seed):
    return current_rng.set(Random(seed))


# ==================== STORAGE ====================


current_connection = ContextVar("connection", default=None)
action_key = ContextVar("action_key", default="system")
write_lock = asyncio.Lock()
savepoints = counter_sequence()


def context(function):

    @wraps(function)
    def wrapped(*args, **kwargs):
        return CursorResult(function(*args, **kwargs))

    return wrapped


class CursorResult:
    def __init__(self, coroutine):
        self.coroutine = coroutine
        self.cursor = None

    def __await__(self):
        return self.coroutine.__await__()

    async def __aenter__(self):
        self.cursor = await self.coroutine
        return self.cursor

    async def __aexit__(self, *_):
        await self.cursor.close()


class Scope:
    def __init__(self, connection, name):
        self.connection = connection
        self.name = name
        self.row_factory = None

    @context
    async def execute(self, sql, parameters=()):
        if sql.strip().upper().startswith("BEGIN"):
            sql, parameters = ("SELECT 1", ())
        cursor = await self.connection.execute(sql, parameters)
        cursor.row_factory = self.row_factory
        return cursor

    @context
    async def executemany(self, sql, parameters):
        cursor = await self.connection.executemany(sql, parameters)
        cursor.row_factory = self.row_factory
        return cursor

    async def commit(self):
        pass

    async def rollback(self):
        await self.connection.execute(f"ROLLBACK TO {self.name}")


@asynccontextmanager
async def connect(path, timeout=30):
    active = current_connection.get()
    if active is None:
        async with aiosqlite.connect(path, timeout=timeout) as connection:
            await connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        return
    name = f"scope_{next(savepoints)}"
    await active.execute(f"SAVEPOINT {name}")
    try:
        yield Scope(active, name)
    except BaseException:
        await active.execute(f"ROLLBACK TO {name}")
        raise
    finally:
        await active.execute(f"RELEASE {name}")


def encode(value):
    if isinstance(value, dict):
        return {"dict": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, set):
        return {"set": [encode(v) for v in value]}
    if isinstance(value, tuple):
        return {"tuple": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if "dict" in value:
            return {decode(k): decode(v) for k, v in value["dict"]}
        if "set" in value:
            return set(map(decode, value["set"]))
        if "tuple" in value:
            return tuple(map(decode, value["tuple"]))
    if isinstance(value, list):
        return list(map(decode, value))
    return value


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS processed_updates (id INTEGER PRIMARY KEY, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS action_randomness (id INTEGER PRIMARY KEY, seed TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS notices (\n        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, text TEXT NOT NULL,\n        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,\n        next_attempt REAL NOT NULL DEFAULT 0, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS runtime_state (name TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS inbox (\n        id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',\n        attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS invoice_orders (\n        payload TEXT PRIMARY KEY, uid INTEGER NOT NULL, dc INTEGER NOT NULL,\n        stars INTEGER NOT NULL, created REAL NOT NULL, paid INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS payment_review (\n        charge TEXT PRIMARY KEY, uid INTEGER NOT NULL, data TEXT NOT NULL, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS gift_jobs (\n        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, gift_id TEXT NOT NULL,\n        status TEXT NOT NULL DEFAULT 'pending', error TEXT, created REAL NOT NULL,\n        action TEXT NOT NULL, sequence INTEGER NOT NULL, UNIQUE(action,sequence))",
    "CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status,id)",
    "CREATE INDEX IF NOT EXISTS gift_jobs_status ON gift_jobs(status,id)",
)


class Store:
    def __init__(self, path, state):
        self.path = path
        self.inbox_path = str(path) + ".inbox"
        self.state = state

    async def init(self):
        async with aiosqlite.connect(self.path, timeout=30) as c:
            await c.execute("PRAGMA journal_mode=WAL")
            for sql in SCHEMA:
                if "inbox" not in sql and "action_randomness" not in sql:
                    await c.execute(sql)
            async with c.execute(
                "SELECT COUNT(*) FROM coins WHERE balance IS NULL\n                OR typeof(balance)<>'integer' OR balance<0 OR balance>9000000000000000\n                OR visual_balance IS NULL OR typeof(visual_balance)<>'integer'\n                OR visual_balance<0 OR visual_balance>9000000000000000"
            ) as cursor:
                invalid = (await cursor.fetchone())[0]
            if invalid:
                raise ValueError(f"Found {invalid} invalid balances; reconcile a backup before startup")
            await c.execute("UPDATE gift_jobs SET status='unknown' WHERE status='sending'")
            for operation in ("INSERT", "UPDATE"):
                await c.execute(
                    f"CREATE TRIGGER IF NOT EXISTS coins_valid_{operation}\n                    BEFORE {operation} ON coins\n                    WHEN typeof(NEW.balance) != 'integer' OR NEW.balance < 0\n                      OR NEW.balance > 9000000000000000\n                      OR typeof(NEW.visual_balance) != 'integer' OR NEW.visual_balance < 0\n                      OR NEW.visual_balance > 9000000000000000\n                    BEGIN SELECT RAISE(ABORT, 'Balance outside safe integer range'); END"
                )
            async with c.execute("SELECT name,data FROM runtime_state") as cursor:
                for name, data in await cursor.fetchall():
                    if name in self.state:
                        self.state[name].clear()
                        self.state[name].update(decode(json.loads(data)))
            await c.commit()
        async with aiosqlite.connect(self.inbox_path, timeout=5) as c:
            await c.execute("PRAGMA journal_mode=WAL")
            for sql in SCHEMA:
                if "inbox" in sql or "action_randomness" in sql:
                    await c.execute(sql)
            await c.commit()

    @asynccontextmanager
    async def transaction(self, key="system"):
        if current_connection.get() is not None:
            raise RuntimeError("Nested runtime units are not supported")
        async with write_lock:
            snapshot = {name: copy.deepcopy(dict(value)) for name, value in self.state.items()}
            async with aiosqlite.connect(self.path, timeout=30) as connection:
                await connection.execute("PRAGMA foreign_keys=ON")
                await connection.execute("BEGIN IMMEDIATE")
                token = current_connection.set(connection)
                action = action_key.set(key)
                try:
                    yield connection
                    for name, value in self.state.items():
                        await connection.execute(
                            "INSERT OR REPLACE INTO runtime_state VALUES (?,?)",
                            (name, json.dumps(encode(dict(value)), ensure_ascii=False)),
                        )
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    for name, value in snapshot.items():
                        self.state[name].clear()
                        self.state[name].update(value)
                    raise
                finally:
                    current_connection.reset(token)
                    action_key.reset(action)

    async def enqueue(self, updates):
        async with aiosqlite.connect(self.inbox_path, timeout=5) as c:
            await c.executemany(
                "INSERT OR IGNORE INTO inbox(id,payload,created) VALUES (?,?,?)",
                [(u.update_id, u.model_dump_json(exclude_none=True), time.time()) for u in updates],
            )
            await c.executemany(
                "INSERT OR IGNORE INTO action_randomness VALUES (?,?)",
                [(u.update_id, secrets.token_hex(32)) for u in updates],
            )
            await c.commit()


# ==================== DATABASE ====================


class Database:
    def __init__(self, path: str = "activity.db"):
        self.path = path

    async def init(self) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS user_stats (\n                    user_id    INTEGER,\n                    chat_id    INTEGER,\n                    user_name  TEXT,\n                    chance     REAL    DEFAULT 0.1,\n                    msg_count  INTEGER DEFAULT 0,\n                    last_bonus REAL    DEFAULT 0,\n                    PRIMARY KEY (user_id, chat_id)\n                )\n            "
            )
            async with db.execute("PRAGMA table_info(user_stats)") as cur:
                columns = [row[1] for row in await cur.fetchall()]
            if "username" not in columns:
                await db.execute("ALTER TABLE user_stats ADD COLUMN username TEXT")
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS wins (\n                    id         INTEGER PRIMARY KEY AUTOINCREMENT,\n                    user_id    INTEGER NOT NULL,\n                    chat_id    INTEGER NOT NULL,\n                    user_name  TEXT,\n                    chance     REAL,\n                    won_at     REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS pending_gifts (\n                    id          INTEGER PRIMARY KEY AUTOINCREMENT,\n                    user_id     INTEGER NOT NULL,\n                    user_name   TEXT,\n                    gift_id     TEXT NOT NULL,\n                    reason      TEXT,\n                    created_at  REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS premium_orders (\n                    id          INTEGER PRIMARY KEY AUTOINCREMENT,\n                    user_id     INTEGER NOT NULL,\n                    user_name   TEXT,\n                    cost        INTEGER NOT NULL,\n                    created_at  REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS star_coin_purchases (\n                    payment_charge_id TEXT PRIMARY KEY,\n                    user_id           INTEGER NOT NULL,\n                    dc_amount         INTEGER NOT NULL,\n                    star_amount       INTEGER NOT NULL,\n                    created_at        REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS vip_users (\n                    user_id INTEGER PRIMARY KEY\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS banned_users (\n                    user_id   INTEGER PRIMARY KEY,\n                    reason    TEXT,\n                    banned_at REAL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS daily_stats (\n                    user_id   INTEGER,\n                    chat_id   INTEGER,\n                    user_name TEXT,\n                    date      TEXT NOT NULL,\n                    msg_count INTEGER DEFAULT 0,\n                    PRIMARY KEY (user_id, chat_id, date)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS coins (\n                    user_id         INTEGER PRIMARY KEY,\n                    balance         INTEGER DEFAULT 10,\n                    visual_balance  INTEGER DEFAULT 0,\n                    last_coin_bonus REAL    DEFAULT 0\n                )\n            "
            )
            async with db.execute("PRAGMA table_info(coins)") as cur:
                coin_columns = [row[1] for row in await cur.fetchall()]
            if "visual_balance" not in coin_columns:
                await db.execute("ALTER TABLE coins ADD COLUMN visual_balance INTEGER DEFAULT 0")
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS promo_codes (\n                    code       TEXT PRIMARY KEY COLLATE NOCASE,\n                    reward     INTEGER NOT NULL,\n                    reward_type TEXT NOT NULL DEFAULT 'coins',\n                    case_id    TEXT,\n                    case_count INTEGER,\n                    max_uses   INTEGER,\n                    uses       INTEGER NOT NULL DEFAULT 0,\n                    created_at REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS promo_activations (\n                    code       TEXT NOT NULL COLLATE NOCASE,\n                    user_id    INTEGER NOT NULL,\n                    activated_at REAL NOT NULL,\n                    PRIMARY KEY (code, user_id)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS case_keys (\n                    user_id  INTEGER NOT NULL,\n                    case_id  TEXT NOT NULL,\n                    amount   INTEGER NOT NULL DEFAULT 0,\n                    PRIMARY KEY (user_id, case_id)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS pandora_claims (\n                    user_id     INTEGER PRIMARY KEY,\n                    last_opened REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS jackpot_days (\n                    day         TEXT PRIMARY KEY,\n                    pool        INTEGER NOT NULL DEFAULT 0,\n                    drawn       INTEGER NOT NULL DEFAULT 0,\n                    winner_id   INTEGER,\n                    winner_name TEXT,\n                    drawn_at    REAL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS jackpot_players (\n                    day     TEXT NOT NULL,\n                    user_id INTEGER NOT NULL,\n                    name    TEXT NOT NULL,\n                    loss    INTEGER NOT NULL DEFAULT 0,\n                    tickets INTEGER NOT NULL DEFAULT 0,\n                    PRIMARY KEY(day, user_id)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS jackpot_actions (\n                    day     TEXT NOT NULL,\n                    user_id INTEGER NOT NULL,\n                    token   TEXT NOT NULL,\n                    PRIMARY KEY(day, user_id, token)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS mini_events (\n                    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n                    kind             TEXT NOT NULL,\n                    chat_id          TEXT NOT NULL,\n                    reward           INTEGER NOT NULL,\n                    max_participants INTEGER NOT NULL DEFAULT 0,\n                    ends             REAL NOT NULL,\n                    status           TEXT NOT NULL DEFAULT 'active',\n                    message_id       INTEGER,\n                    winner_id        INTEGER,\n                    winner_name      TEXT,\n                    announced_at     REAL,\n                    created_at       REAL NOT NULL\n                )\n            "
            )
            async with db.execute("PRAGMA table_info(mini_events)") as cur:
                mini_event_columns = {row[1] for row in await cur.fetchall()}
            if "announced_at" not in mini_event_columns:
                await db.execute("ALTER TABLE mini_events ADD COLUMN announced_at REAL")
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS mini_event_players (\n                    event_id INTEGER NOT NULL,\n                    user_id  INTEGER NOT NULL,\n                    name     TEXT NOT NULL,\n                    joined_at REAL NOT NULL,\n                    PRIMARY KEY(event_id, user_id)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS bot_users (\n                    user_id    INTEGER PRIMARY KEY,\n                    user_name  TEXT,\n                    started_at REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS bonus_broadcasts (\n                    id         INTEGER PRIMARY KEY AUTOINCREMENT,\n                    amount     INTEGER NOT NULL,\n                    reward_type TEXT NOT NULL DEFAULT 'coins',\n                    case_id     TEXT,\n                    created_at REAL NOT NULL\n                )\n            "
            )
            async with db.execute("PRAGMA table_info(bonus_broadcasts)") as cur:
                broadcast_columns = {row[1] for row in await cur.fetchall()}
            if "reward_type" not in broadcast_columns:
                await db.execute(
                    "ALTER TABLE bonus_broadcasts ADD COLUMN reward_type TEXT NOT NULL DEFAULT 'coins'"
                )
            if "case_id" not in broadcast_columns:
                await db.execute("ALTER TABLE bonus_broadcasts ADD COLUMN case_id TEXT")
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS bonus_deliveries (\n                    broadcast_id INTEGER NOT NULL,\n                    user_id      INTEGER NOT NULL,\n                    status       INTEGER NOT NULL DEFAULT 0,\n                    attempts     INTEGER NOT NULL DEFAULT 0,\n                    error        TEXT,\n                    PRIMARY KEY (broadcast_id, user_id)\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS support_access (\n                    user_id    INTEGER PRIMARY KEY,\n                    granted_at REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS support_sessions (\n                    user_id    INTEGER PRIMARY KEY,\n                    active     INTEGER NOT NULL DEFAULT 0,\n                    updated_at REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS support_links (\n                    admin_message_id INTEGER PRIMARY KEY,\n                    user_id          INTEGER NOT NULL,\n                    created_at       REAL NOT NULL\n                )\n            "
            )
            await db.execute(
                "\n                CREATE TABLE IF NOT EXISTS app_settings (\n                    key        TEXT PRIMARY KEY,\n                    value      REAL NOT NULL,\n                    updated_at REAL NOT NULL\n                )\n            "
            )
            async with db.execute("PRAGMA table_info(promo_codes)") as cur:
                promo_columns = [row[1] for row in await cur.fetchall()]
            if "reward_type" not in promo_columns:
                await db.execute(
                    "ALTER TABLE promo_codes ADD COLUMN reward_type TEXT NOT NULL DEFAULT 'coins'"
                )
            if "case_id" not in promo_columns:
                await db.execute("ALTER TABLE promo_codes ADD COLUMN case_id TEXT")
            if "case_count" not in promo_columns:
                await db.execute("ALTER TABLE promo_codes ADD COLUMN case_count INTEGER")
            await db.execute(
                "INSERT OR IGNORE INTO bot_users(user_id,user_name,started_at) SELECT c.user_id, COALESCE((SELECT u.user_name FROM user_stats u WHERE u.user_id=c.user_id ORDER BY u.chat_id LIMIT 1), CAST(c.user_id AS TEXT)), ? FROM coins c WHERE c.user_id>0",
                (time.time(),),
            )
            await db.commit()

    async def ban_user(self, user_id: int, reason: str = "") -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?)",
                (user_id, reason, time.time()),
            )
            await db.commit()

    async def unban_user(self, user_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
            await db.commit()

    async def is_banned(self, user_id: int) -> bool:
        async with connect(self.path) as db:
            async with db.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)) as cur:
                return await cur.fetchone() is not None

    async def get_ban_list(self) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT u.user_id, COALESCE(s.user_name, CAST(u.user_id AS TEXT)), u.reason FROM banned_users u LEFT JOIN user_stats s ON s.user_id = u.user_id AND s.chat_id = ?",
                (MAIN_CHAT_ID,),
            ) as cur:
                return await cur.fetchall()

    async def add_pending_gift(self, user_id: int, user_name: str, gift_id: str, reason: str) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO pending_gifts (user_id, user_name, gift_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_name, gift_id, reason, time.time()),
            )
            await db.commit()

    async def get_pending_gifts(self) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT id, user_id, user_name, gift_id, reason, created_at FROM pending_gifts ORDER BY created_at ASC"
            ) as cur:
                return await cur.fetchall()

    async def remove_pending_gift(self, gift_db_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute("DELETE FROM pending_gifts WHERE id=?", (gift_db_id,))
            await db.commit()

    async def add_premium_order(self, user_id: int, user_name: str, cost: int) -> int:
        async with connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO premium_orders (user_id, user_name, cost, created_at) VALUES (?, ?, ?, ?)",
                (user_id, user_name, cost, time.time()),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_premium_orders(self) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT id, user_id, user_name, cost, created_at FROM premium_orders ORDER BY created_at ASC"
            ) as cur:
                return await cur.fetchall()

    async def remove_premium_order(self, order_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute("DELETE FROM premium_orders WHERE id=?", (order_id,))
            await db.commit()

    async def credit_star_coin_purchase(
        self, payment_charge_id: str, user_id: int, dc_amount: int, star_amount: int
    ) -> tuple[bool, int]:
        """Зачисляет покупку один раз; повторный платёж Telegram не дублирует DC."""
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO star_coin_purchases (payment_charge_id, user_id, dc_amount, star_amount, created_at) VALUES (?, ?, ?, ?, ?)",
                    (payment_charge_id, user_id, dc_amount, star_amount, time.time()),
                )
                if cursor.rowcount != 1:
                    async with db.execute("SELECT balance FROM coins WHERE user_id=?", (user_id,)) as cur:
                        row = await cur.fetchone()
                    await db.rollback()
                    return (False, row[0] if row else COINS_START)
                await db.execute(
                    "INSERT INTO coins (user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
                    (user_id, COINS_START + dc_amount, dc_amount),
                )
                async with db.execute("SELECT balance FROM coins WHERE user_id=?", (user_id,)) as cur:
                    row = await cur.fetchone()
                await db.commit()
                return (True, row[0])
            except Exception:
                await db.rollback()
                raise

    async def get_settings(self) -> dict[str, float]:
        async with connect(self.path) as db:
            async with db.execute("SELECT key, value FROM app_settings") as cur:
                return {key: float(value) for key, value in await cur.fetchall()}

    async def set_setting(self, key: str, value: float) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, float(value), time.time()),
            )
            await db.commit()

    async def get_user(self, user_id: int, chat_id: int) -> tuple:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT chance, msg_count, last_bonus FROM user_stats WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            ) as cur:
                row = await cur.fetchone()
        return row if row else (START_CHANCE, 0, 0.0)

    async def update_user(
        self,
        user_id: int,
        chat_id: int,
        user_name: str,
        chance: float,
        msg_count: int,
        last_bonus: float,
    ) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "\n                INSERT INTO user_stats (user_id, chat_id, user_name, chance, msg_count, last_bonus)\n                VALUES (?, ?, ?, ?, ?, ?)\n                ON CONFLICT(user_id, chat_id) DO UPDATE SET\n                    user_name  = excluded.user_name,\n                    chance     = excluded.chance,\n                    msg_count  = excluded.msg_count,\n                    last_bonus = excluded.last_bonus\n            ",
                (user_id, chat_id, user_name, chance, msg_count, last_bonus),
            )
            await db.commit()

    async def get_user_name(self, user_id: int) -> str:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT user_name FROM user_stats WHERE user_id=? AND chat_id=?",
                (user_id, MAIN_CHAT_ID),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else str(user_id)

    async def set_username(
        self,
        user_id: int,
        username: str | None,
        chat_id: int = MAIN_CHAT_ID,
        user_name: str | None = None,
    ) -> None:
        normalized = username.lower().lstrip("@") if username else None
        async with connect(self.path) as db:
            if normalized:
                await db.execute(
                    "UPDATE user_stats SET username=NULL WHERE username=? AND user_id<>?",
                    (normalized, user_id),
                )
            await db.execute(
                "\n                INSERT INTO user_stats (user_id, chat_id, user_name, username)\n                VALUES (?, ?, ?, ?)\n                ON CONFLICT(user_id, chat_id) DO UPDATE SET\n                    username = excluded.username,\n                    user_name = COALESCE(excluded.user_name, user_stats.user_name)\n                ",
                (user_id, chat_id, user_name or str(user_id), normalized),
            )
            await db.commit()

    async def find_user_by_username(self, username: str):
        username = username.lower().lstrip("@")
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT user_id, user_name FROM user_stats WHERE username=? ORDER BY (chat_id=?) DESC LIMIT 1",
                (username, MAIN_CHAT_ID),
            ) as cur:
                return await cur.fetchone()

    async def transfer_coins(self, sender_id: int, recipient_id: int, amount: int):
        if amount <= 0 or sender_id == recipient_id:
            return (False, None, None)
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (sender_id, COINS_START),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (recipient_id, COINS_START),
                )
                cur = await db.execute(
                    "UPDATE coins SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (amount, sender_id, amount),
                )
                if cur.rowcount != 1:
                    await db.rollback()
                    return (False, None, None)
                await db.execute(
                    "UPDATE coins SET balance=balance+? WHERE user_id=?",
                    (amount, recipient_id),
                )
                await db.commit()
                async with db.execute("SELECT balance FROM coins WHERE user_id=?", (sender_id,)) as cur:
                    sender_row = await cur.fetchone()
                async with db.execute("SELECT balance FROM coins WHERE user_id=?", (recipient_id,)) as cur:
                    recipient_row = await cur.fetchone()
                return (True, sender_row[0], recipient_row[0])
            except Exception:
                await db.rollback()
                raise

    async def get_top(self, chat_id: int, limit: int = 5) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT user_name, chance, msg_count FROM user_stats WHERE chat_id=? ORDER BY chance DESC LIMIT ?",
                (chat_id, limit),
            ) as cur:
                return await cur.fetchall()

    async def add_win(self, user_id: int, chat_id: int, user_name: str, chance: float) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO wins (user_id, chat_id, user_name, chance, won_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, chat_id, user_name, chance, time.time()),
            )
            await db.commit()

    async def get_wins_count(self, user_id: int, chat_id: int) -> int:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM wins WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def get_wins_top(self, chat_id: int, limit: int = 10) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT user_name, COUNT(*) as cnt FROM wins WHERE chat_id=? GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
                (chat_id, limit),
            ) as cur:
                return await cur.fetchall()

    async def set_vip(self, user_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO vip_users (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def remove_vip(self, user_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute("DELETE FROM vip_users WHERE user_id=?", (user_id,))
            await db.commit()

    async def is_vip(self, user_id: int) -> bool:
        async with connect(self.path) as db:
            async with db.execute("SELECT 1 FROM vip_users WHERE user_id=?", (user_id,)) as cur:
                return await cur.fetchone() is not None

    async def increment_daily(self, user_id: int, chat_id: int, user_name: str) -> None:
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with connect(self.path) as db:
            await db.execute(
                "\n                INSERT INTO daily_stats (user_id, chat_id, user_name, date, msg_count)\n                VALUES (?, ?, ?, ?, 1)\n                ON CONFLICT(user_id, chat_id, date) DO UPDATE SET\n                    user_name = excluded.user_name,\n                    msg_count = msg_count + 1\n            ",
                (user_id, chat_id, user_name, today),
            )
            await db.commit()

    async def get_daily_top(self, chat_id: int, limit: int = 10) -> list:
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT user_name, msg_count FROM daily_stats WHERE chat_id=? AND date=? ORDER BY msg_count DESC LIMIT ?",
                (chat_id, today, limit),
            ) as cur:
                return await cur.fetchall()

    async def clear_old_daily(self) -> None:
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with connect(self.path) as db:
            await db.execute("DELETE FROM daily_stats WHERE date < ?", (today,))
            await db.commit()

    async def add_day_messages(self, user_id: int, chat_id: int, user_name: str, amount: int):
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with connect(self.path) as db:
            await db.execute(
                "\n                INSERT INTO daily_stats (user_id, chat_id, user_name, date, msg_count)\n                VALUES (?, ?, ?, ?, ?)\n                ON CONFLICT(user_id, chat_id, date)\n                DO UPDATE SET msg_count = msg_count + ?, user_name = excluded.user_name\n            ",
                (user_id, chat_id, user_name, today, amount, amount),
            )
            await db.commit()

    async def remove_day_messages(self, user_id: int, chat_id: int, amount: int):
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with connect(self.path) as db:
            await db.execute(
                "\n                UPDATE daily_stats SET msg_count = MAX(msg_count - ?, 0)\n                WHERE user_id=? AND chat_id=? AND date=?\n            ",
                (amount, user_id, chat_id, today),
            )
            await db.commit()

    async def get_coins(self, user_id: int) -> tuple:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT balance, last_coin_bonus FROM coins WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        return row if row else (COINS_START, 0.0)

    async def get_balance_details(self, user_id: int) -> tuple[int, int, int]:
        """Возвращает отображаемый, реальный и визуальный баланс."""
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT balance,COALESCE(visual_balance,0) FROM coins WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        real, visual = (int(row[0]), int(row[1])) if row else (COINS_START, 0)
        return (real + visual, real, visual)

    async def get_display_balance(self, user_id: int) -> int:
        displayed, _, _ = await self.get_balance_details(user_id)
        return displayed

    async def change_visual_balance(self, user_id: int, amount: int) -> tuple[bool, int, int, int]:
        """Меняет только визуальные DC; они никогда не участвуют в списаниях."""
        if amount == 0:
            displayed, real, visual = await self.get_balance_details(user_id)
            return (False, displayed, real, visual)
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO coins(user_id,balance,visual_balance) VALUES (?,?,0)",
                    (user_id, COINS_START),
                )
                cursor = await db.execute(
                    "UPDATE coins SET visual_balance=visual_balance+? WHERE user_id=? AND visual_balance+?>=0",
                    (amount, user_id, amount),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    displayed, real, visual = await self.get_balance_details(user_id)
                    return (False, displayed, real, visual)
                async with db.execute(
                    "SELECT balance,visual_balance FROM coins WHERE user_id=?",
                    (user_id,),
                ) as cur:
                    row = await cur.fetchone()
                await db.commit()
                real, visual = (int(row[0]), int(row[1]))
                return (True, real + visual, real, visual)
            except Exception:
                await db.rollback()
                raise

    async def add_coins(self, user_id: int, amount: int) -> int:
        async with connect(self.path) as db:
            await db.execute(
                "\n                INSERT INTO coins (user_id, balance) VALUES (?, ?)\n                ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?\n            ",
                (user_id, COINS_START + amount, amount),
            )
            await db.commit()
            async with db.execute("SELECT balance FROM coins WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else COINS_START

    async def remove_coins(self, user_id: int, amount: int) -> bool:
        if amount <= 0:
            return False
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (user_id, COINS_START),
                )
                cursor = await db.execute(
                    "UPDATE coins SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                    (amount, user_id, amount),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def settle_duel(
        self, challenger_id: int, opponent_id: int, bet: int, winner_id: int
    ) -> tuple[bool, int, int]:
        """Атомарно списывает две ставки и переводит весь банк победителю."""
        if (
            winner_id not in (challenger_id, opponent_id)
            or challenger_id == opponent_id
            or bet < DUEL_MIN_BET
            or (bet > DUEL_MAX_BET)
        ):
            return (False, 0, 0)
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.executemany(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    [(challenger_id, COINS_START), (opponent_id, COINS_START)],
                )
                async with db.execute(
                    "SELECT user_id, balance FROM coins WHERE user_id IN (?, ?)",
                    (challenger_id, opponent_id),
                ) as cur:
                    balances = {user_id: balance for user_id, balance in await cur.fetchall()}
                challenger_balance = balances.get(challenger_id, COINS_START)
                opponent_balance = balances.get(opponent_id, COINS_START)
                if challenger_balance < bet or opponent_balance < bet:
                    await db.rollback()
                    return (False, challenger_balance, opponent_balance)
                await db.execute(
                    "UPDATE coins SET balance = balance - ? WHERE user_id IN (?, ?)",
                    (bet, challenger_id, opponent_id),
                )
                await db.execute(
                    "UPDATE coins SET balance = balance + ? WHERE user_id=?",
                    (bet * 2, winner_id),
                )
                async with db.execute(
                    "SELECT user_id, balance FROM coins WHERE user_id IN (?, ?)",
                    (challenger_id, opponent_id),
                ) as cur:
                    final_balances = {user_id: balance for user_id, balance in await cur.fetchall()}
                await db.commit()
                return (
                    True,
                    final_balances[challenger_id],
                    final_balances[opponent_id],
                )
            except Exception:
                await db.rollback()
                raise

    async def set_coin_bonus_time(self, user_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "\n                INSERT INTO coins (user_id, last_coin_bonus) VALUES (?, ?)\n                ON CONFLICT(user_id) DO UPDATE SET last_coin_bonus = ?\n            ",
                (user_id, time.time(), time.time()),
            )
            await db.commit()

    async def get_coins_top(self, limit: int = 10) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "\n                SELECT c.user_id, COALESCE(u.user_name, CAST(c.user_id AS TEXT)),\n                       c.balance + COALESCE(c.visual_balance,0) AS shown_balance\n                FROM coins c\n                LEFT JOIN user_stats u ON u.user_id = c.user_id AND u.chat_id = ?\n                ORDER BY shown_balance DESC LIMIT ?\n            ",
                (MAIN_CHAT_ID, limit),
            ) as cur:
                return await cur.fetchall()

    async def create_promo(self, code: str, reward: int, max_uses: int | None) -> bool:
        if reward <= 0 or (max_uses is not None and max_uses <= 0):
            return False
        async with connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO promo_codes (code, reward, max_uses, created_at) VALUES (?, ?, ?, ?)",
                    (code, reward, max_uses, time.time()),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def create_case_promo(self, code: str, case_id: str, case_count: int, max_uses: int | None) -> bool:
        if case_id not in CASES or case_count <= 0 or (max_uses is not None and max_uses <= 0):
            return False
        async with connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO promo_codes (code, reward, reward_type, case_id, case_count, max_uses, created_at) VALUES (?, 0, 'case', ?, ?, ?, ?)",
                    (code, case_id, case_count, max_uses, time.time()),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def delete_promo(self, code: str) -> bool:
        async with connect(self.path) as db:
            cursor = await db.execute("DELETE FROM promo_codes WHERE code=?", (code,))
            await db.commit()
        return cursor.rowcount == 1

    async def get_promos(self) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT code, reward, reward_type, case_id, case_count, max_uses, uses FROM promo_codes ORDER BY created_at DESC"
            ) as cur:
                return await cur.fetchall()

    async def get_case_keys(self, user_id: int, case_id: str) -> int:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT amount FROM case_keys WHERE user_id=? AND case_id=?",
                (user_id, case_id),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def add_case_keys(self, user_id: int, case_id: str, amount: int = 1) -> int:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO case_keys(user_id,case_id,amount) VALUES (?,?,?) ON CONFLICT(user_id,case_id) DO UPDATE SET amount=amount+excluded.amount",
                (user_id, case_id, amount),
            )
            await db.commit()
        return await self.get_case_keys(user_id, case_id)

    async def pandora_remaining(self, user_id: int, now: float | None = None) -> int:
        now = time.time() if now is None else now
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT last_opened FROM pandora_claims WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return 0
        return max(0, int(PANDORA_COOLDOWN - (now - row[0]) + 0.999))

    async def claim_pandora(self, user_id: int, now: float | None = None) -> tuple[bool, int]:
        """Атомарно фиксирует бесплатное открытие, не позволяя двойной клик."""
        now = time.time() if now is None else now
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT last_opened FROM pandora_claims WHERE user_id=?", (user_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row and now - row[0] < PANDORA_COOLDOWN:
                    remaining = max(1, int(PANDORA_COOLDOWN - (now - row[0]) + 0.999))
                    await db.rollback()
                    return (False, remaining)
                await db.execute(
                    "INSERT INTO pandora_claims(user_id,last_opened) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_opened=excluded.last_opened",
                    (user_id, now),
                )
                await db.commit()
                return (True, 0)
            except Exception:
                await db.rollback()
                raise

    async def register_bot_user(self, user_id: int, user_name: str) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO bot_users(user_id,user_name,started_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET user_name=excluded.user_name",
                (user_id, user_name, time.time()),
            )
            await db.commit()

    async def grant_support_access(self, user_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO support_access(user_id,granted_at) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET granted_at=excluded.granted_at",
                (user_id, time.time()),
            )
            await db.commit()

    async def revoke_support_access(self, user_id: int) -> bool:
        async with connect(self.path) as db:
            cursor = await db.execute("DELETE FROM support_access WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM support_sessions WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM support_links WHERE user_id=?", (user_id,))
            await db.commit()
            return cursor.rowcount == 1

    async def has_support_access(self, user_id: int) -> bool:
        async with connect(self.path) as db:
            async with db.execute("SELECT 1 FROM support_access WHERE user_id=?", (user_id,)) as cur:
                return await cur.fetchone() is not None

    async def get_support_access_list(self) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT a.user_id,COALESCE(u.user_name,CAST(a.user_id AS TEXT)),a.granted_at FROM support_access a LEFT JOIN user_stats u ON u.user_id=a.user_id AND u.chat_id=? ORDER BY a.granted_at DESC",
                (MAIN_CHAT_ID,),
            ) as cur:
                return await cur.fetchall()

    async def set_support_session(self, user_id: int, active: bool) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT INTO support_sessions(user_id,active,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at",
                (user_id, int(active), time.time()),
            )
            await db.commit()

    async def is_support_session_active(self, user_id: int) -> bool:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT 1 FROM support_sessions s JOIN support_access a ON a.user_id=s.user_id WHERE s.user_id=? AND s.active=1",
                (user_id,),
            ) as cur:
                return await cur.fetchone() is not None

    async def link_support_message(self, admin_message_id: int, user_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO support_links(admin_message_id,user_id,created_at) VALUES (?,?,?)",
                (admin_message_id, user_id, time.time()),
            )
            await db.commit()

    async def get_support_user_by_admin_message(self, admin_message_id: int) -> int | None:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT user_id FROM support_links WHERE admin_message_id=?",
                (admin_message_id,),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else None

    async def create_bonus_broadcast(self, amount: int) -> tuple[int, int]:
        async with connect(self.path, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT user_id FROM bot_users ORDER BY user_id") as cur:
                    users = [row[0] for row in await cur.fetchall()]
                cur = await db.execute(
                    "INSERT INTO bonus_broadcasts(amount,created_at) VALUES (?,?)",
                    (amount, time.time()),
                )
                broadcast_id = cur.lastrowid
                for user_id in users:
                    await db.execute(
                        "INSERT INTO coins(user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
                        (user_id, COINS_START + amount, amount),
                    )
                    await db.execute(
                        "INSERT INTO bonus_deliveries(broadcast_id,user_id) VALUES (?,?)",
                        (broadcast_id, user_id),
                    )
                await db.commit()
                return (broadcast_id, len(users))
            except Exception:
                await db.rollback()
                raise

    async def create_case_broadcast(self, case_id: str, amount: int = 1) -> tuple[int, int]:
        if case_id not in CASES or amount <= 0:
            raise ValueError("Invalid case broadcast")
        async with connect(self.path, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT user_id FROM bot_users ORDER BY user_id") as cur:
                    users = [row[0] for row in await cur.fetchall()]
                cur = await db.execute(
                    "INSERT INTO bonus_broadcasts(amount,reward_type,case_id,created_at) VALUES (?,'case',?,?)",
                    (amount, case_id, time.time()),
                )
                broadcast_id = cur.lastrowid
                for user_id in users:
                    await db.execute(
                        "INSERT INTO case_keys(user_id,case_id,amount) VALUES (?,?,?) ON CONFLICT(user_id,case_id) DO UPDATE SET amount=amount+excluded.amount",
                        (user_id, case_id, amount),
                    )
                    await db.execute(
                        "INSERT INTO bonus_deliveries(broadcast_id,user_id) VALUES (?,?)",
                        (broadcast_id, user_id),
                    )
                await db.commit()
                return (broadcast_id, len(users))
            except Exception:
                await db.rollback()
                raise

    async def record_jackpot_loss(
        self,
        user_id: int,
        user_name: str,
        loss: int,
        token: str,
        now: float | None = None,
    ) -> tuple[int, int, int]:
        """Adds 2% of a real loss to today's jackpot and awards capped tickets."""
        if loss <= 0:
            return (0, 0, 0)
        now = time.time() if now is None else now
        day = jackpot_day_key(now)
        contribution = int(loss) * JACKPOT_PERCENT // 100
        async with connect(self.path, timeout=30) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("INSERT OR IGNORE INTO jackpot_days(day) VALUES (?)", (day,))
                action = await db.execute(
                    "INSERT OR IGNORE INTO jackpot_actions(day,user_id,token) VALUES (?,?,?)",
                    (day, user_id, token),
                )
                if action.rowcount != 1:
                    await db.rollback()
                    return (0, 0, 0)
                async with db.execute(
                    "SELECT loss,tickets FROM jackpot_players WHERE day=? AND user_id=?",
                    (day, user_id),
                ) as cur:
                    previous = await cur.fetchone()
                old_loss, old_tickets = previous if previous else (0, 0)
                total_loss = int(old_loss) + int(loss)
                tickets = min(JACKPOT_TICKET_LIMIT, total_loss // JACKPOT_TICKET_STEP)
                earned = tickets - int(old_tickets)
                await db.execute(
                    "INSERT INTO jackpot_players(day,user_id,name,loss,tickets) VALUES (?,?,?,?,?) ON CONFLICT(day,user_id) DO UPDATE SET name=excluded.name,loss=excluded.loss,tickets=excluded.tickets",
                    (day, user_id, user_name, total_loss, tickets),
                )
                if contribution:
                    await db.execute(
                        "UPDATE jackpot_days SET pool=pool+? WHERE day=?",
                        (contribution, day),
                    )
                await db.commit()
                return (contribution, earned, tickets)
            except Exception:
                await db.rollback()
                raise

    async def jackpot_summary(self, user_id: int, now: float | None = None) -> dict:
        day = jackpot_day_key(now)
        async with connect(self.path) as db:
            async with db.execute("SELECT pool FROM jackpot_days WHERE day=?", (day,)) as cur:
                pool_row = await cur.fetchone()
            async with db.execute(
                "SELECT tickets,loss FROM jackpot_players WHERE day=? AND user_id=?",
                (day, user_id),
            ) as cur:
                player_row = await cur.fetchone()
            async with db.execute(
                "SELECT COUNT(*) FROM jackpot_players WHERE day=? AND tickets>0", (day,)
            ) as cur:
                players_row = await cur.fetchone()
        return {
            "day": day,
            "pool": int(pool_row[0]) if pool_row else 0,
            "tickets": int(player_row[0]) if player_row else 0,
            "loss": int(player_row[1]) if player_row else 0,
            "players": int(players_row[0]) if players_row else 0,
        }

    async def jackpot_top(self, now: float | None = None, limit: int = 10) -> list:
        day = jackpot_day_key(now)
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT name,tickets FROM jackpot_players WHERE day=? AND tickets>0 ORDER BY tickets DESC,loss DESC,user_id LIMIT ?",
                (day, limit),
            ) as cur:
                return await cur.fetchall()

    async def jackpot_winners(self, limit: int = 5) -> list:
        async with connect(self.path) as db:
            async with db.execute(
                "SELECT winner_name,pool,day FROM jackpot_days WHERE drawn=1 AND winner_id IS NOT NULL ORDER BY day DESC LIMIT ?",
                (limit,),
            ) as cur:
                return await cur.fetchall()

    async def draw_pending_jackpots(self, now: float | None = None) -> list[dict]:
        """Draws all undrawn Moscow days before today; idle pools roll over."""
        now = time.time() if now is None else now
        today = jackpot_day_key(now)
        outcomes = []
        async with connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT day,pool FROM jackpot_days WHERE day<? AND drawn=0 ORDER BY day",
                    (today,),
                ) as cur:
                    days = await cur.fetchall()
                for row in days:
                    day, pool = (row["day"], int(row["pool"]))
                    async with db.execute(
                        "SELECT user_id,name,tickets FROM jackpot_players WHERE day=? AND tickets>0 ORDER BY user_id",
                        (day,),
                    ) as cur:
                        players = await cur.fetchall()
                    ticket_total = sum((int(player["tickets"]) for player in players))
                    if ticket_total <= 0:
                        await db.execute(
                            "UPDATE jackpot_days SET drawn=1,drawn_at=? WHERE day=?",
                            (now, day),
                        )
                        if pool:
                            await db.execute(
                                "INSERT OR IGNORE INTO jackpot_days(day) VALUES (?)",
                                (today,),
                            )
                            await db.execute(
                                "UPDATE jackpot_days SET pool=pool+? WHERE day=?",
                                (pool, today),
                            )
                        outcomes.append({"kind": "carry", "day": day, "pool": pool})
                        continue
                    winning_ticket = secrets.SystemRandom().randint(1, ticket_total)
                    cursor = 0
                    winner = players[-1]
                    for player in players:
                        cursor += int(player["tickets"])
                        if winning_ticket <= cursor:
                            winner = player
                            break
                    await db.execute(
                        "UPDATE jackpot_days SET drawn=1,winner_id=?,winner_name=?,drawn_at=? WHERE day=?",
                        (winner["user_id"], winner["name"], now, day),
                    )
                    if pool:
                        await db.execute(
                            "INSERT INTO coins(user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
                            (winner["user_id"], COINS_START + pool, pool),
                        )
                    outcomes.append(
                        {
                            "kind": "winner",
                            "day": day,
                            "pool": pool,
                            "user_id": int(winner["user_id"]),
                            "name": winner["name"],
                            "tickets": int(winner["tickets"]),
                            "total_tickets": ticket_total,
                        }
                    )
                await db.commit()
                return outcomes
            except Exception:
                await db.rollback()
                raise

    async def create_mini_event(
        self, kind: str, chat_id: str, reward: int, max_participants: int, duration: int
    ) -> tuple[str, dict | None]:
        if kind not in {"first", "first_n", "ticket"} or reward <= 0 or duration <= 0:
            raise ValueError("Invalid mini event")
        now = time.time()
        async with connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id FROM mini_events WHERE chat_id=? AND status='active' LIMIT 1",
                    (chat_id,),
                ) as cur:
                    active = await cur.fetchone()
                if active:
                    await db.rollback()
                    return ("active_exists", None)
                cursor = await db.execute(
                    "INSERT INTO mini_events(kind,chat_id,reward,max_participants,ends,created_at) VALUES (?,?,?,?,?,?)",
                    (kind, chat_id, reward, max_participants, now + duration, now),
                )
                event_id = cursor.lastrowid
                async with db.execute("SELECT * FROM mini_events WHERE id=?", (event_id,)) as cur:
                    event = dict(await cur.fetchone())
                await db.commit()
                return ("created", event)
            except Exception:
                await db.rollback()
                raise

    async def set_mini_event_message(self, event_id: int, message_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute("UPDATE mini_events SET message_id=? WHERE id=?", (message_id, event_id))
            await db.commit()

    async def join_mini_event(self, event_id: int, user_id: int, name: str) -> dict:
        now = time.time()
        async with connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT * FROM mini_events WHERE id=?", (event_id,)) as cur:
                    event = await cur.fetchone()
                if not event or event["status"] != "active":
                    await db.rollback()
                    return {"status": "finished"}
                if event["ends"] <= now:
                    await db.execute(
                        "UPDATE mini_events SET status='expired' WHERE id=?",
                        (event_id,),
                    )
                    await db.commit()
                    return {"status": "expired"}
                joined = await db.execute(
                    "INSERT OR IGNORE INTO mini_event_players(event_id,user_id,name,joined_at) VALUES (?,?,?,?)",
                    (event_id, user_id, name, now),
                )
                if joined.rowcount != 1:
                    await db.rollback()
                    return {"status": "already"}
                async with db.execute(
                    "SELECT COUNT(*) FROM mini_event_players WHERE event_id=?",
                    (event_id,),
                ) as cur:
                    participant_count = int((await cur.fetchone())[0])
                paid = event["kind"] in {"first", "first_n"}
                complete = paid and participant_count >= event["max_participants"]
                if paid:
                    await db.execute(
                        "INSERT INTO coins(user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
                        (user_id, COINS_START + event["reward"], event["reward"]),
                    )
                if complete:
                    await db.execute(
                        "UPDATE mini_events SET status='completed' WHERE id=?",
                        (event_id,),
                    )
                await db.commit()
                return {
                    "status": "joined",
                    "kind": event["kind"],
                    "reward": int(event["reward"]),
                    "count": participant_count,
                    "max": int(event["max_participants"]),
                    "complete": complete,
                }
            except Exception:
                await db.rollback()
                raise

    async def cancel_mini_event(self, chat_id: str) -> dict | None:
        async with connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM mini_events WHERE chat_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                    (chat_id,),
                ) as cur:
                    event = await cur.fetchone()
                if not event:
                    await db.rollback()
                    return None
                await db.execute(
                    "UPDATE mini_events SET status='cancelled' WHERE id=?",
                    (event["id"],),
                )
                await db.commit()
                return dict(event)
            except Exception:
                await db.rollback()
                raise

    async def finish_due_mini_events(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        outcomes = []
        async with connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM mini_events WHERE status='active' AND ends<=? ORDER BY id",
                    (now,),
                ) as cur:
                    events = await cur.fetchall()
                for event in events:
                    async with db.execute(
                        "SELECT user_id,name FROM mini_event_players WHERE event_id=? ORDER BY joined_at,user_id",
                        (event["id"],),
                    ) as cur:
                        players = await cur.fetchall()
                    if event["kind"] == "ticket" and players:
                        winner = secrets.SystemRandom().choice(players)
                        await db.execute(
                            "INSERT INTO coins(user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
                            (
                                winner["user_id"],
                                COINS_START + event["reward"],
                                event["reward"],
                            ),
                        )
                        await db.execute(
                            "UPDATE mini_events SET status='completed',winner_id=?,winner_name=? WHERE id=?",
                            (winner["user_id"], winner["name"], event["id"]),
                        )
                        outcomes.append(
                            {
                                "kind": "ticket_winner",
                                "event": dict(event),
                                "count": len(players),
                                "winner_id": int(winner["user_id"]),
                                "winner_name": winner["name"],
                            }
                        )
                    else:
                        await db.execute(
                            "UPDATE mini_events SET status='expired' WHERE id=?",
                            (event["id"],),
                        )
                        outcomes.append(
                            {
                                "kind": "expired",
                                "event": dict(event),
                                "count": len(players),
                            }
                        )
                await db.commit()
                return outcomes
            except Exception:
                await db.rollback()
                raise

    async def pending_mini_event_announcements(self) -> list[dict]:
        """Completed ticket draws that still need a public winner announcement."""
        async with connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT event.*, COUNT(player.user_id) AS participant_count FROM mini_events AS event LEFT JOIN mini_event_players AS player ON player.event_id=event.id WHERE event.kind='ticket' AND event.status='completed' AND event.winner_id IS NOT NULL AND event.announced_at IS NULL GROUP BY event.id ORDER BY event.id"
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def mark_mini_event_announced(self, event_id: int) -> None:
        async with connect(self.path) as db:
            await db.execute(
                "UPDATE mini_events SET announced_at=? WHERE id=? AND announced_at IS NULL",
                (time.time(), event_id),
            )
            await db.commit()

    async def open_case(self, user_id: int, case_id: str, price: int | None, key_only: bool = False) -> str:
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE case_keys SET amount = amount - 1 WHERE user_id=? AND case_id=? AND amount > 0",
                    (user_id, case_id),
                )
                if cursor.rowcount == 1:
                    await db.commit()
                    return "key"
                if key_only:
                    await db.rollback()
                    return "key_required"
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (user_id, COINS_START),
                )
                cursor = await db.execute(
                    "UPDATE coins SET balance = balance - ? WHERE user_id=? AND balance >= ?",
                    (price, user_id, price),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return "insufficient"
                await db.commit()
                return "coins"
            except Exception:
                await db.rollback()
                raise

    async def redeem_promo(self, code: str, user_id: int):
        async with connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT reward, reward_type, case_id, case_count, max_uses, uses FROM promo_codes WHERE code=?",
                    (code,),
                ) as cur:
                    promo = await cur.fetchone()
                if not promo:
                    await db.rollback()
                    return ("not_found", None, None, None, None, None, None)
                reward, reward_type, case_id, case_count, max_uses, uses = promo
                async with db.execute(
                    "SELECT 1 FROM promo_activations WHERE code=? AND user_id=?",
                    (code, user_id),
                ) as cur:
                    already_used = await cur.fetchone()
                if already_used:
                    await db.rollback()
                    return ("already_used", None, None, None, None, None, None)
                if max_uses is not None and uses >= max_uses:
                    await db.rollback()
                    return ("limit_reached", None, None, None, None, None, None)
                await db.execute(
                    "INSERT INTO promo_activations (code, user_id, activated_at) VALUES (?, ?, ?)",
                    (code, user_id, time.time()),
                )
                if reward_type == "case":
                    await db.execute(
                        "INSERT INTO case_keys (user_id, case_id, amount) VALUES (?, ?, ?) ON CONFLICT(user_id, case_id) DO UPDATE SET amount = amount + ?",
                        (user_id, case_id, case_count, case_count),
                    )
                else:
                    await db.execute(
                        "INSERT INTO coins (user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
                        (user_id, COINS_START + reward, reward),
                    )
                await db.execute("UPDATE promo_codes SET uses = uses + 1 WHERE code=?", (code,))
                await db.commit()
                return (
                    "success",
                    reward,
                    reward_type,
                    case_id,
                    case_count,
                    uses + 1,
                    max_uses,
                )
            except Exception:
                await db.rollback()
                raise


# ==================== EVENT ====================


def loss_share_chance(lost_amount: int) -> float:
    """10k → 10%, 25k → 4%, 50k → 2%, 100k → 1%."""
    if lost_amount < LOSS_SHARE_MIN_AMOUNT:
        return 0.0
    return min(0.1, 1000 / lost_amount)


class SchoolEvent:
    """All progression, claims, boss damage and refunds use SQLite transactions."""

    def __init__(self, database):
        self.database = database

    @asynccontextmanager
    async def transaction(self):
        async with connect(self.database.path, timeout=30) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def one(self, conn, sql, args=()):
        async with conn.execute(sql, args) as cursor:
            return await cursor.fetchone()

    async def init(self):
        async with self.transaction() as c:
            for sql in (
                "CREATE TABLE IF NOT EXISTS school_seasons (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, ends REAL, stopped INTEGER DEFAULT 0, hp INTEGER, max_hp INTEGER, killed REAL, path_winner INTEGER, boss_stage INTEGER DEFAULT 1, seal_stage INTEGER DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS school_players (season INTEGER, uid INTEGER, name TEXT, knowledge INTEGER DEFAULT 0, damage INTEGER DEFAULT 0, reached REAL DEFAULT 0, last_message REAL DEFAULT 0, PRIMARY KEY(season,uid))",
                "CREATE TABLE IF NOT EXISTS school_quests (season INTEGER, uid INTEGER, day TEXT, quest TEXT, progress INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0, PRIMARY KEY(season,uid,day,quest))",
                "CREATE TABLE IF NOT EXISTS school_days (season INTEGER, uid INTEGER, day TEXT, streak INTEGER, claimed INTEGER DEFAULT 0, PRIMARY KEY(season,uid,day))",
                "CREATE TABLE IF NOT EXISTS school_claims (season INTEGER, uid INTEGER, prize TEXT, PRIMARY KEY(season,uid,prize))",
                "CREATE TABLE IF NOT EXISTS school_actions (season INTEGER, uid INTEGER, token TEXT, PRIMARY KEY(season,uid,token))",
                "CREATE TABLE IF NOT EXISTS school_prizes (id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER, uid INTEGER, reason TEXT, kind TEXT, amount INTEGER, done INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, UNIQUE(season,uid,reason,kind))",
                "CREATE TABLE IF NOT EXISTS school_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER, tag TEXT, text TEXT, sent INTEGER DEFAULT 0, UNIQUE(season,tag))",
                "CREATE TABLE IF NOT EXISTS golden_hours (id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER NOT NULL, started REAL NOT NULL, ends REAL NOT NULL, finished REAL, bonus_damage INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS golden_hour_players (hour INTEGER NOT NULL, uid INTEGER NOT NULL, name TEXT NOT NULL, bonus_damage INTEGER NOT NULL DEFAULT 0, bonus_knowledge INTEGER NOT NULL DEFAULT 0, games INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(hour,uid))",
            ):
                await c.execute(sql)
            async with c.execute("PRAGMA table_info(school_prizes)") as cur:
                prize_columns = {row[1] for row in await cur.fetchall()}
            if "attempts" not in prize_columns:
                await c.execute("ALTER TABLE school_prizes ADD COLUMN attempts INTEGER DEFAULT 0")
            async with c.execute("PRAGMA table_info(school_seasons)") as cur:
                season_columns = {row[1] for row in await cur.fetchall()}
            if "boss_stage" not in season_columns:
                await c.execute("ALTER TABLE school_seasons ADD COLUMN boss_stage INTEGER DEFAULT 1")
            if "seal_stage" not in season_columns:
                await c.execute("ALTER TABLE school_seasons ADD COLUMN seal_stage INTEGER DEFAULT 0")
            async with c.execute("PRAGMA table_info(golden_hours)") as cur:
                golden_hour_columns = {row[1] for row in await cur.fetchall()}
            if "bonus_damage" not in golden_hour_columns:
                await c.execute("ALTER TABLE golden_hours ADD COLUMN bonus_damage INTEGER NOT NULL DEFAULT 0")
            await c.execute(
                "UPDATE golden_hours SET bonus_damage=COALESCE((SELECT SUM(p.bonus_damage) FROM golden_hour_players p WHERE p.hour=golden_hours.id),0) WHERE bonus_damage=0"
            )
            async with c.execute(
                "SELECT id FROM school_seasons WHERE boss_stage=1 AND hp<=0 AND killed IS NOT NULL AND stopped=0 AND ends>?",
                (time.time(),),
            ) as cur:
                defeated_legacy_seasons = [row[0] for row in await cur.fetchall()]
            for season_id in defeated_legacy_seasons:
                await c.execute(
                    "UPDATE school_seasons SET boss_stage=2,hp=?,max_hp=?,seal_stage=0,killed=NULL WHERE id=?",
                    (SCHOOL_BOSSES[2]["max_hp"], SCHOOL_BOSSES[2]["max_hp"], season_id),
                )
                await c.execute(
                    "UPDATE school_players SET damage=0,reached=0 WHERE season=?",
                    (season_id,),
                )

    async def current(self, c, now=None, active=True):
        row = await self.one(c, "SELECT * FROM school_seasons ORDER BY id DESC LIMIT 1")
        if active and row and (row["stopped"] or (now or time.time()) >= row["ends"]):
            return None
        return row

    async def active_golden_hour(self, c, season, now):
        return await self.one(
            c,
            "SELECT * FROM golden_hours WHERE season=? AND finished IS NULL AND ends>? ORDER BY id DESC LIMIT 1",
            (season, now),
        )

    async def golden_hour_status(self, now=None):
        now = time.time() if now is None else now
        async with self.transaction() as c:
            season = await self.current(c, now)
            if not season:
                return None
            return await self.active_golden_hour(c, season["id"], now)

    async def start_golden_hour(self, now=None):
        now = time.time() if now is None else now
        async with self.transaction() as c:
            season = await self.current(c, now)
            if not season:
                return ("no_event", None)
            existing = await self.active_golden_hour(c, season["id"], now)
            if existing:
                return ("active", existing)
            await c.execute(
                "INSERT INTO golden_hours(season,started,ends) VALUES (?,?,?)",
                (season["id"], now, now + GOLDEN_HOUR_SECONDS),
            )
            return ("started", await self.one(c, "SELECT * FROM golden_hours WHERE id=last_insert_rowid()"))

    async def stop_golden_hour(self, now=None):
        now = time.time() if now is None else now
        async with self.transaction() as c:
            season = await self.current(c, now)
            if not season:
                return False
            hour = await self.active_golden_hour(c, season["id"], now)
            if not hour:
                return False
            await c.execute("UPDATE golden_hours SET ends=? WHERE id=?", (now, hour["id"]))
            return True

    async def finish_golden_hours(self, now=None):
        now = time.time() if now is None else now
        outcomes = []
        async with self.transaction() as c:
            async with c.execute(
                "SELECT * FROM golden_hours WHERE finished IS NULL AND ends<=? ORDER BY id",
                (now,),
            ) as cur:
                hours = await cur.fetchall()
            for hour in hours:
                async with c.execute(
                    "SELECT name,bonus_damage,bonus_knowledge,games FROM golden_hour_players WHERE hour=? ORDER BY bonus_damage DESC,games DESC,uid LIMIT 3",
                    (hour["id"],),
                ) as cur:
                    top = await cur.fetchall()
                await c.execute("UPDATE golden_hours SET finished=? WHERE id=?", (now, hour["id"]))
                outcomes.append((hour, top))
        return outcomes

    async def start(self):
        async with self.transaction() as c:
            if await self.current(c):
                return False
            now = time.time()
            await c.execute(
                "INSERT INTO school_seasons(started,ends,hp,max_hp,boss_stage) VALUES (?,?,?,?,1)",
                (
                    now,
                    now + 20 * 86400,
                    SCHOOL_BOSSES[1]["max_hp"],
                    SCHOOL_BOSSES[1]["max_hp"],
                ),
            )
            return True

    async def stop(self, season):
        async with self.transaction() as c:
            await c.execute("UPDATE school_seasons SET stopped=1 WHERE id=?", (season,))

    async def change_event_days(self, days: int, admin_id: int, token: str):
        if not isinstance(days, int) or days == 0 or abs(days) > 365:
            raise ValueError("Days must be a nonzero integer within -365..365")
        async with self.transaction() as c:
            season = await self.current(c, active=False)
            if not season:
                return ("missing", None)
            if season["stopped"]:
                return ("stopped", None)
            action = await c.execute(
                "INSERT OR IGNORE INTO school_actions(season,uid,token) VALUES (?,?,?)",
                (season["id"], admin_id, token),
            )
            if action.rowcount != 1:
                return ("duplicate", season["ends"])
            new_end = season["ends"] + days * 86400
            await c.execute("UPDATE school_seasons SET ends=? WHERE id=?", (new_end, season["id"]))
            return ("updated", new_end)

    async def coins(self, c, uid, amount):
        await c.execute(
            "INSERT INTO coins(user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
            (uid, COINS_START + amount, amount),
        )

    async def reward(self, c, season, uid, reason, items):
        for kind, amount in items:
            if kind == "coins":
                await self.coins(c, uid, amount)
            elif kind in CASES:
                await c.execute(
                    "INSERT INTO case_keys(user_id,case_id,amount) VALUES (?,?,?) ON CONFLICT(user_id,case_id) DO UPDATE SET amount=amount+excluded.amount",
                    (uid, kind, amount),
                )
            else:
                await c.execute(
                    "INSERT OR IGNORE INTO school_prizes(season,uid,reason,kind,amount) VALUES (?,?,?,?,?)",
                    (season, uid, reason, kind, amount),
                )

    async def prepare(self, c, season, uid, name, now):
        day = datetime.fromtimestamp(now, pytz.timezone("Europe/Moscow")).date().isoformat()
        await c.execute(
            "INSERT INTO school_players(season,uid,name) VALUES (?,?,?) ON CONFLICT(season,uid) DO UPDATE SET name=excluded.name",
            (season, uid, name),
        )
        exists = await self.one(
            c,
            "SELECT 1 FROM school_quests WHERE season=? AND uid=? AND day=?",
            (season, uid, day),
        )
        if not exists:
            chosen = random.sample(list(QUESTS), 3)
            await c.executemany(
                "INSERT INTO school_quests(season,uid,day,quest) VALUES (?,?,?,?)",
                [(season, uid, day, q) for q in chosen],
            )
        return day

    async def progress(self, c, season, uid, name, day, now, metrics):
        player = await self.one(c, "SELECT * FROM school_players WHERE season=? AND uid=?", (season, uid))
        metrics = dict(metrics)
        if metrics.get("messages"):
            if now - player["last_message"] < 10:
                metrics.pop("messages")
            else:
                await c.execute(
                    "UPDATE school_players SET last_message=? WHERE season=? AND uid=?",
                    (now, season, uid),
                )
        async with c.execute(
            "SELECT * FROM school_quests WHERE season=? AND uid=? AND day=?",
            (season, uid, day),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            _, metric, target, _, _ = QUESTS[row["quest"]]
            value = max(0, int(metrics.get(metric, 0)))
            if value:
                await c.execute(
                    "UPDATE school_quests SET progress=MIN(?,progress+?) WHERE season=? AND uid=? AND day=? AND quest=?",
                    (target, value, season, uid, day, row["quest"]),
                )
        async with c.execute(
            "SELECT quest,progress FROM school_quests WHERE season=? AND uid=? AND day=?",
            (season, uid, day),
        ) as cur:
            complete = all((row["progress"] >= QUESTS[row["quest"]][2] for row in await cur.fetchall()))
        if complete:
            yesterday = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
            previous = await self.one(
                c,
                "SELECT streak FROM school_days WHERE season=? AND uid=? AND day=?",
                (season, uid, yesterday),
            )
            streak = previous[0] % 5 + 1 if previous else 1
            await c.execute(
                "INSERT OR IGNORE INTO school_days(season,uid,day,streak) VALUES (?,?,?,?)",
                (season, uid, day, streak),
            )

    async def record(self, uid, name, token, metrics=None, loss=0, now=None, manual=False):
        now = time.time() if now is None else now
        async with self.transaction() as c:
            season = await self.current(c, now)
            if not season or uid <= 0:
                return (0, 0)
            sid = season["id"]
            cur = await c.execute(
                "INSERT OR IGNORE INTO school_actions(season,uid,token) VALUES (?,?,?)",
                (sid, uid, token),
            )
            if cur.rowcount != 1:
                return (0, 0)
            if not manual:
                day = await self.prepare(c, sid, uid, name, now)
                await self.progress(c, sid, uid, name, day, now, metrics or {})
            golden = None if manual else await self.active_golden_hour(c, sid, now)
            if golden and int((metrics or {}).get("games", 0)):
                await c.execute(
                    "INSERT INTO golden_hour_players(hour,uid,name,games) VALUES (?,?,?,?) "
                    "ON CONFLICT(hour,uid) DO UPDATE SET name=excluded.name,games=MIN(?,golden_hour_players.games+excluded.games)",
                    (golden["id"], uid, name, 1, GOLDEN_HOUR_GAMES_LIMIT),
                )
            damage = refund = 0
            phase_reborn = False
            if loss > 0 and season["hp"] > 0:
                stage = int(season["boss_stage"] or 1)
                seals = int(season["seal_stage"] or 0)
                damage_limit = int(season["hp"])
                if stage == 2 and seals == 0 and (season["hp"] > MAGISTER_SEAL_HP):
                    damage_limit = int(season["hp"]) - MAGISTER_SEAL_HP
                base_damage = min(int(loss), damage_limit)
                damage = base_damage
                refund = int(loss) - base_damage
                if golden and base_damage:
                    player = await self.one(
                        c,
                        "SELECT bonus_damage FROM golden_hour_players WHERE hour=? AND uid=?",
                        (golden["id"], uid),
                    )
                    used_bonus = int(player[0]) if player else 0
                    extra_damage = min(
                        base_damage,
                        max(0, GOLDEN_HOUR_DAMAGE_LIMIT - used_bonus),
                        max(0, GOLDEN_HOUR_TOTAL_BONUS_DAMAGE_LIMIT - int(golden["bonus_damage"] or 0)),
                        max(0, damage_limit - base_damage),
                    )
                    if extra_damage:
                        damage += extra_damage
                        await c.execute(
                            "UPDATE golden_hours SET bonus_damage=bonus_damage+? WHERE id=?",
                            (extra_damage, golden["id"]),
                        )
                        await c.execute(
                            "INSERT INTO golden_hour_players(hour,uid,name,bonus_damage) VALUES (?,?,?,?) "
                            "ON CONFLICT(hour,uid) DO UPDATE SET name=excluded.name,bonus_damage=golden_hour_players.bonus_damage+excluded.bonus_damage",
                            (
                                golden["id"],
                                uid,
                                name,
                                extra_damage,
                            ),
                        )
                if not manual:
                    await c.execute(
                        "UPDATE school_players SET damage=damage+?, reached=? WHERE season=? AND uid=?",
                        (damage, now, sid, uid),
                    )
                await c.execute("UPDATE school_seasons SET hp=hp-? WHERE id=?", (damage, sid))
                if refund and (not manual):
                    await self.coins(c, uid, refund)
                remaining_hp = int(season["hp"]) - damage
                if stage == 2 and seals == 0 and (remaining_hp == MAGISTER_SEAL_HP):
                    await c.execute(
                        "UPDATE school_seasons SET seal_stage=1,max_hp=? WHERE id=?",
                        (MAGISTER_SEAL_HP, sid),
                    )
                    await c.execute(
                        "INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)",
                        (
                            sid,
                            "magister-seal:1",
                            "🔮 Первая печать снята!\n\nИгроки пробили первую защиту Магистра Забвений.\n❤️ Осталось: 3 000 000 HP\n\nВпереди ещё две печати.",
                        ),
                    )
                elif stage == 2 and remaining_hp == 0 and (1 <= seals < MAGISTER_SEAL_COUNT):
                    next_seal = seals + 1
                    seal_text = (
                        "🔮 Вторая печать снята!\n\nМагистр Забвений высвободил новую силу.\n❤️ Новая линия здоровья: 3 000 000 HP\n\nОсталась последняя печать."
                        if next_seal == 2
                        else "🔮 Третья печать снята!\n\nПоследняя защита Магистра разрушена.\n❤️ Финальная линия здоровья: 3 000 000 HP\n\nТеперь его можно победить окончательно."
                    )
                    await c.execute(
                        "UPDATE school_seasons SET hp=?,max_hp=?,seal_stage=? WHERE id=?",
                        (MAGISTER_SEAL_HP, MAGISTER_SEAL_HP, next_seal, sid),
                    )
                    await c.execute(
                        "INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)",
                        (sid, f"magister-seal:{next_seal}", seal_text),
                    )
                    phase_reborn = True
                share_chance = 0 if manual else loss_share_chance(base_damage)
                if share_chance and random.random() < share_chance:
                    async with c.execute(
                        "SELECT uid,name FROM school_players WHERE season=? AND damage>0 ORDER BY damage DESC,reached,uid LIMIT 10",
                        (sid,),
                    ) as cur:
                        top_ten = await cur.fetchall()
                    candidates = [player for player in top_ten if player["uid"] != uid]
                    if candidates:
                        recipient = random.choice(candidates)
                        share_amount = base_damage // 2
                        await self.coins(c, recipient["uid"], share_amount)
                        await c.execute(
                            "INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)",
                            (
                                sid,
                                f"loss-share:{uid}:{token}",
                                f"🎁 Босс перераспределил потерянные знания!\n\n{recipient['name']} получает {share_amount:,} DC из проигранной ставки игрока {name}.".replace(
                                    ",", " "
                                ),
                            ),
                        )
                if damage == season["hp"] and (not phase_reborn):
                    stage = int(season["boss_stage"] or 1)
                    boss = SCHOOL_BOSSES.get(stage, SCHOOL_BOSSES[2])
                    await c.execute("UPDATE school_seasons SET killed=? WHERE id=?", (now, sid))
                    async with c.execute(
                        "SELECT * FROM school_players WHERE season=? AND damage>0 ORDER BY damage DESC,reached,uid",
                        (sid,),
                    ) as cur:
                        players = await cur.fetchall()
                    top = "\n".join(
                        (f"{i}. {p['name']} — {p['damage']:,}" for i, p in enumerate(players[:3], 1))
                    )
                    if stage == 1:
                        for p in players:
                            rewards = []
                            if p["damage"] >= 10000:
                                rewards.append(("coins", 10000))
                            if p["damage"] >= 100000:
                                rewards.append(("excellent", 1))
                            await self.reward(c, sid, p["uid"], "Архимед: победа над боссом", rewards)
                        if not manual:
                            await self.reward(
                                c,
                                sid,
                                uid,
                                "Архимед: последний удар",
                                [("coins", boss["last_hit"])],
                            )
                        stage_one_prizes = [
                            [("nft", 1)],
                            [("premium", 1)],
                            [("gift", 100)],
                        ]
                        for place, p in enumerate(players[:3], 1):
                            await self.reward(
                                c,
                                sid,
                                p["uid"],
                                f"Архимед: топ-{place} по урону",
                                stage_one_prizes[place - 1],
                            )
                        await c.execute(
                            "INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)",
                            (
                                sid,
                                "boss:1",
                                f"🏆 Архимед Знаний побеждён!\n\n{top}\n\n"
                                + (
                                    f"Последний удар: {name} — {boss['last_hit']:,} DC.\n"
                                    if not manual
                                    else "Босс добит вручную администратором.\n"
                                )
                                + "Рейтинг урона обнулён. Появился новый босс — 🌑 Магистр Забвений с 50 000 000 HP!",
                            ),
                        )
                        await c.execute(
                            "UPDATE school_seasons SET boss_stage=2,hp=?,max_hp=?,seal_stage=0,killed=NULL WHERE id=?",
                            (
                                SCHOOL_BOSSES[2]["max_hp"],
                                SCHOOL_BOSSES[2]["max_hp"],
                                sid,
                            ),
                        )
                        await c.execute(
                            "UPDATE school_players SET damage=0,reached=0 WHERE season=?",
                            (sid,),
                        )
                    else:
                        if not manual:
                            await self.reward(
                                c,
                                sid,
                                uid,
                                "Магистр Забвений: последний удар",
                                [("coins", boss["last_hit"])],
                            )
                        for place, p in enumerate(players[:3], 1):
                            await self.reward(
                                c,
                                sid,
                                p["uid"],
                                f"Магистр Забвений: топ-{place} по урону",
                                [("nft", 1)],
                            )
                        await c.execute(
                            "INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)",
                            (
                                sid,
                                "boss:2",
                                f"🏆 Магистр Забвений побеждён!\n\n{top}\n\n"
                                + (
                                    f"Последний удар: {name} — {boss['last_hit']:,} DC.\n"
                                    if not manual
                                    else "Босс добит вручную администратором.\n"
                                )
                                + "Все игроки из топ-3 получают NFT. Рейтинг зафиксирован!",
                            ),
                        )
            return (damage, 0 if manual else refund)

    async def claim(self, sid, uid, name, category, value):
        async with self.transaction() as c:
            season = await self.current(c)
            if not season or season["id"] != sid:
                return "Ивент завершён или эта кнопка устарела."
            now = time.time()
            day = await self.prepare(c, sid, uid, name, now)
            if category == "quest":
                qday, qid = value.split("/", 1)
                if qday != day or qid not in QUESTS:
                    return "Задание устарело."
                row = await self.one(
                    c,
                    "SELECT * FROM school_quests WHERE season=? AND uid=? AND day=? AND quest=?",
                    (sid, uid, day, qid),
                )
                if not row or row["progress"] < QUESTS[qid][2]:
                    return "Сначала выполни задание."
                if row["claimed"]:
                    return "Награда уже получена."
                await c.execute(
                    "UPDATE school_quests SET claimed=1 WHERE season=? AND uid=? AND day=? AND quest=?",
                    (sid, uid, day, qid),
                )
                _, _, _, dc, points = QUESTS[qid]
                await self.coins(c, uid, dc)
                golden = await self.active_golden_hour(c, sid, now)
                bonus_points = 0
                if golden:
                    golden_player = await self.one(
                        c,
                        "SELECT bonus_knowledge FROM golden_hour_players WHERE hour=? AND uid=?",
                        (golden["id"], uid),
                    )
                    used_bonus = int(golden_player[0]) if golden_player else 0
                    bonus_points = min(points, max(0, GOLDEN_HOUR_KNOWLEDGE_LIMIT - used_bonus))
                    if bonus_points:
                        await c.execute(
                            "INSERT INTO golden_hour_players(hour,uid,name,bonus_knowledge) VALUES (?,?,?,?) "
                            "ON CONFLICT(hour,uid) DO UPDATE SET name=excluded.name,bonus_knowledge=golden_hour_players.bonus_knowledge+excluded.bonus_knowledge",
                            (golden["id"], uid, name, bonus_points),
                        )
                total_points = points + bonus_points
                await c.execute(
                    "UPDATE school_players SET knowledge=knowledge+? WHERE season=? AND uid=?",
                    (total_points, sid, uid),
                )
                p = await self.one(
                    c,
                    "SELECT knowledge FROM school_players WHERE season=? AND uid=?",
                    (sid, uid),
                )
                if p[0] >= 5000:
                    cur = await c.execute(
                        "UPDATE school_seasons SET path_winner=? WHERE id=? AND path_winner IS NULL",
                        (uid, sid),
                    )
                    if cur.rowcount == 1:
                        await self.reward(c, sid, uid, "Первый на уровне 50", [("nft", 1)])
                        await c.execute(
                            "INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)",
                            (
                                sid,
                                "race",
                                f"🏁 {name} первым достиг 50-го уровня!\n🏆 Отдельный NFT ждёт выдачи администратором. Остальные участники продолжают призовой путь.",
                            ),
                        )
                golden_text = f" (⚡ Золотой час: +{bonus_points} 📖)" if bonus_points else ""
                return f"✅ +{dc:,} DC и +{total_points} 📖" + golden_text
            if category == "day":
                if value != day:
                    return "Бонус относится к другому дню."
                row = await self.one(
                    c,
                    "SELECT * FROM school_days WHERE season=? AND uid=? AND day=?",
                    (sid, uid, day),
                )
                if not row:
                    return "Выполни все три задания."
                if row["claimed"]:
                    return "Бонус уже получен."
                await c.execute(
                    "UPDATE school_days SET claimed=1 WHERE season=? AND uid=? AND day=?",
                    (sid, uid, day),
                )
                rewards = [("coins", 3000)]
                if row["streak"] == 5:
                    rewards.append(("excellent", 1))
                await self.reward(c, sid, uid, "Серия квестов", rewards)
                return "✅ +3 000 DC" + (
                    " и 🔑 Кейс отличника за 5 дней подряд!" if row["streak"] == 5 else ""
                )
            if category in {"level", "levels"}:
                player = await self.one(
                    c,
                    "SELECT knowledge FROM school_players WHERE season=? AND uid=?",
                    (sid, uid),
                )
                if category == "levels":
                    max_level = min(50, player[0] // 100)
                    received = []
                    received_levels = []
                    for level in range(1, max_level + 1):
                        cur = await c.execute(
                            "INSERT OR IGNORE INTO school_claims(season,uid,prize) VALUES (?,?,?)",
                            (sid, uid, f"level:{level}"),
                        )
                        if cur.rowcount == 1:
                            await self.reward(c, sid, uid, f"Уровень {level}", PATH_REWARDS[level])
                            received.extend(PATH_REWARDS[level])
                            received_levels.append(level)
                    if not received:
                        return "Все доступные награды уже получены."
                    totals = {}
                    real_prizes = []
                    for kind, amount in received:
                        if kind in {"gift", "nft", "premium"}:
                            real_prizes.append((kind, amount))
                        else:
                            totals[kind] = totals.get(kind, 0) + amount
                    summary = event_reward_text(list(totals.items()) + real_prizes)
                    return f"✅ Получено уровней: {len(received_levels)}. Начислено: {summary}"
                level = int(value)
                if level not in PATH_REWARDS or player[0] < level * 100:
                    return "Этот уровень ещё не достигнут."
                cur = await c.execute(
                    "INSERT OR IGNORE INTO school_claims(season,uid,prize) VALUES (?,?,?)",
                    (sid, uid, f"level:{level}"),
                )
                if cur.rowcount != 1:
                    return "Награда уже получена."
                await self.reward(c, sid, uid, f"Уровень {level}", PATH_REWARDS[level])
                return f"✅ Уровень {level}: {event_reward_text(PATH_REWARDS[level])}"
            return "Неизвестная награда."


def event_reward_text(items):
    labels = []
    for kind, amount in items:
        if kind == "coins":
            labels.append(f"{amount:,} DC")
        elif kind in CASES:
            labels.append(f"🔑 {CASES[kind]['title']} ×{amount}")
        else:
            labels.append({"gift": f"🎁 {amount}⭐", "nft": "NFT", "premium": "Premium на месяц"}[kind])
    return " + ".join(labels).replace(",", " ")


# ==================== GAMES ====================


def mines_multiplier(safe_opened: int) -> float:
    """Коэффициент для поля 5×5 с восемью минами."""
    if safe_opened <= 0:
        return 0.0
    if safe_opened == 1:
        return 1.18
    if safe_opened == 2:
        return 1.42
    fair_multiplier = comb(MINES_GRID_SIZE, safe_opened) / comb(MINES_GRID_SIZE - MINES_COUNT, safe_opened)
    return round(fair_multiplier * 0.9, 2)


def mines_prize(bet: int, safe_opened: int) -> int:
    return int(bet * mines_multiplier(safe_opened))


def mines_keyboard(game: dict, reveal: bool = False) -> InlineKeyboardMarkup:
    opened = game["opened"]
    mines = game["mines"]
    buttons = []
    for row in range(5):
        line = []
        for column in range(5):
            cell = row * 5 + column
            if reveal and cell in mines:
                text = "💣"
                callback_data = "mines_done"
            elif cell in opened:
                text = " "
                callback_data = "mines_done"
            elif reveal:
                text = " "
                callback_data = "mines_done"
            else:
                text = "❓"
                callback_data = f"mines_cell_{cell}"
            line.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        buttons.append(line)
    if not reveal:
        if opened:
            prize = mines_prize(game["bet"], len(opened))
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"💸 Забрать {prize:,} DC".replace(",", " "),
                        callback_data="mines_cashout",
                    )
                ]
            )
        else:
            buttons.append([InlineKeyboardButton(text="❌", callback_data="mines_cashout")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def mines_text(game: dict) -> str:
    safe_opened = len(game["opened"])
    text = f"💣 Ты начал игру «Минное поле»!\n💰 Ставка: {game['bet']:,} DC\n💣 Мин на поле: {MINES_COUNT}".replace(
        ",", " "
    )
    if safe_opened:
        multiplier = mines_multiplier(safe_opened)
        prize = mines_prize(game["bet"], safe_opened)
        text += f"\n💵 Выигрыш: x{multiplier:.2f} | {prize:,} DC".replace(",", " ")
    return text


def create_scratch_board(win: bool) -> list[str]:
    """Создаёт поле с одной тройкой при победе и без троек при проигрыше."""
    if win:
        target = random.choice(SCRATCH_SYMBOLS)
        fillers = random.sample([symbol for symbol in SCRATCH_SYMBOLS if symbol != target], 3)
        board = [target] * 3 + [symbol for symbol in fillers for _ in range(2)]
    else:
        fillers = random.sample(list(SCRATCH_SYMBOLS), 5)
        board = [symbol for symbol in fillers[:4] for _ in range(2)] + [fillers[4]]
    random.shuffle(board)
    return board


def scratch_keyboard(game: dict, reveal: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for row in range(3):
        line = []
        for column in range(3):
            cell = row * 3 + column
            opened = reveal or cell in game["opened"]
            line.append(
                InlineKeyboardButton(
                    text=game["board"][cell] if opened else "❓",
                    callback_data="scratch_done" if reveal else f"scratch_cell_{cell}",
                )
            )
        buttons.append(line)
    if reveal:
        buttons.append([InlineKeyboardButton(text="🔄 Играть ещё", callback_data="game:scratch")])
        buttons.append([InlineKeyboardButton(text="🎮 Все игры", callback_data="games")])
    else:
        buttons.append([InlineKeyboardButton(text="✨ Открыть всё", callback_data="scratch_reveal")])
        buttons.append([InlineKeyboardButton(text="🎮 Все игры", callback_data="games")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def scratch_text(game: dict) -> str:
    return f"🎟 Скретч-карты\n\n🎯 Задача: найти 3 одинаковых символа.\n❌ Если тройки нет — проигрыш.\n💸 Выигрыш: ×{SCRATCH_MULTIPLIER}\n\n💰 Ставка: {game['bet']:,} DC\n🪙 Баланс: {game['balance']:,} DC\n\n👇 Открывай клетки или нажми «Открыть всё».".replace(
        ",", " "
    )


def lottery_multiplier_text(multiplier: float) -> str:
    return f"×{multiplier:g}".replace(".", ",")


def lottery_keyboard(game: dict, reveal: bool = False, selected: int | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for row in range(5):
        line = []
        for column in range(5):
            cell = row * 5 + column
            if not reveal:
                text, data = ("🎟", f"lottery_cell_{cell}")
            else:
                multiplier = game["board"][cell]
                if cell == selected:
                    text = "❌" if multiplier == 0 else f"🎯{lottery_multiplier_text(multiplier)}"
                else:
                    text = lottery_multiplier_text(multiplier)
                data = "lottery_done"
            line.append(InlineKeyboardButton(text=text, callback_data=data))
        buttons.append(line)
    if reveal:
        buttons.append([InlineKeyboardButton(text="🔄 Играть ещё", callback_data="game:lottery")])
        buttons.append([InlineKeyboardButton(text="🎮 Все игры", callback_data="games")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def lottery_text(game: dict) -> str:
    bet_text = f"{game['bet']:,}".replace(",", " ")
    return f"🎫 Лотерея 5×5\n\n💸 Ставка: {bet_text} DC\nПопробуй угадать, в каком билете находится самый большой коэффициент!\n\nНа поле: ×3 — 1 · ×2,5 — 1 · ×2 — 3 · ×1,5 — 5\n×1 — 3 · ×0,5 — 2 · ×0 — 10\n\n👇 Выбери один билет:"


# ==================== VIEWS ====================


def start_keyboard(is_admin: bool = False, support_access: bool = False):
    buttons = [
        [InlineKeyboardButton(text="🏫 Школьный ивент", callback_data="school:boss")],
        [
            InlineKeyboardButton(text="⭐ Квесты", callback_data="school:quests"),
            InlineKeyboardButton(text="📖 Призовой путь", callback_data="school:path:0"),
        ],
        [InlineKeyboardButton(text="🎮 Игры", callback_data="games")],
        [InlineKeyboardButton(text="🏆 Джекпот дня", callback_data="jackpot:view")],
        [InlineKeyboardButton(text="📦 Кейсы", callback_data="cases")],
        [InlineKeyboardButton(text="⭐ Купить D-COINS", callback_data="buy_dc_menu")],
        [InlineKeyboardButton(text="❓ Как играть", callback_data="help")],
    ]
    if support_access:
        buttons.append([InlineKeyboardButton(text="💬 Чат с администратором", callback_data="support_open")])
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")],
        ]
    )


def exchange_keyboard(balance: int):
    buttons = [
        [
            InlineKeyboardButton(
                text=f"📈 {economy_price('chance_price'):,} DC → +1 балл шанса".replace(",", " "),
                callback_data="exch_chance",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🎁 {economy_price('gift_15'):,} DC → подарок 15⭐".replace(",", " "),
                callback_data="exch_gift_15",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🎁 {economy_price('gift_25'):,} DC → подарок 25⭐".replace(",", " "),
                callback_data="exch_gift_25",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🎁 {economy_price('gift_50'):,} DC → подарок 50⭐".replace(",", " "),
                callback_data="exch_gift_50",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🎁 {economy_price('gift_100'):,} DC → подарок 100⭐".replace(",", " "),
                callback_data="exch_gift_100",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"💎 {economy_price('premium_1m'):,} DC → Premium на месяц".replace(",", " "),
                callback_data="exch_premium_1m",
            )
        ],
        [InlineKeyboardButton(text="⭐ Купить DC за звёзды", callback_data="buy_dc_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def buy_dc_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for dc_amount, star_amount in STAR_DC_PACKAGES.items():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🪙 {dc_amount:,} DC — {star_amount}⭐".replace(",", " "),
                    callback_data=f"buy_dc_{dc_amount}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="◀️ К обмену", callback_data="buy_dc_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cases_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 ЯЩИК ПАНДОРЫ — бесплатно раз в 5 дней",
                    callback_data="pandora_view",
                )
            ],
            [InlineKeyboardButton(text="🎒 ШКОЛЬНЫЙ — 3 000 DC", callback_data="case_view_school")],
            [
                InlineKeyboardButton(
                    text="🎓 СТУДЕНЧЕСКИЙ — 12 000 DC",
                    callback_data="case_view_student",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏆 КЕЙС ОТЛИЧНИКА — только 🔑",
                    callback_data="case_view_excellent",
                )
            ],
            [InlineKeyboardButton(text="🩸 BLOOD — 5 000 DC", callback_data="case_view_blood")],
            [InlineKeyboardButton(text="🐆 PANTERA — 10 000 DC", callback_data="case_view_pantera")],
            [InlineKeyboardButton(text="🕷 SPIDER MAN — 7 500 DC", callback_data="case_view_spider_man")],
        ]
    )


def case_detail_keyboard(case_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Открыть кейс", callback_data=f"case_open_{case_id}")],
            [InlineKeyboardButton(text="⬅️ Все кейсы", callback_data="cases")],
        ]
    )


def format_pandora_wait(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = (remainder + 59) // 60
    if minutes == 60:
        hours += 1
        minutes = 0
    if hours == 24:
        days += 1
        hours = 0
    parts = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes or not parts:
        parts.append(f"{minutes} мин.")
    return " ".join(parts)


def pandora_keyboard(remaining: int) -> InlineKeyboardMarkup:
    if remaining <= 0:
        action = InlineKeyboardButton(text="🎁 Открыть бесплатно", callback_data="pandora_open")
    else:
        action = InlineKeyboardButton(
            text=f"⏳ Через {format_pandora_wait(remaining)}",
            callback_data="pandora_wait",
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [action],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="pandora_view")],
            [InlineKeyboardButton(text="⬅️ Все кейсы", callback_data="cases")],
        ]
    )


def support_chat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Закрыть диалог", callback_data="support_close")]]
    )


def games_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎰 Слоты", callback_data="game:slots"),
                InlineKeyboardButton(text="🎡 Рулетка", callback_data="game:roulette"),
            ],
            [
                InlineKeyboardButton(text="🎲 Кубик", callback_data="game:dice"),
                InlineKeyboardButton(text="💣 Мины", callback_data="game:mines"),
            ],
            [
                InlineKeyboardButton(text="🪙 Орёл и решка", callback_data="game:coinflip"),
                InlineKeyboardButton(text="🎟 Скретч-карты", callback_data="game:scratch"),
            ],
            [InlineKeyboardButton(text="🎫 Лотерея 5×5", callback_data="game:lottery")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]
    )


def game_bet_keyboard(game: str, option: str = "-") -> InlineKeyboardMarkup:
    buttons = []
    for index in range(0, len(GAME_BET_PRESETS), 2):
        row = []
        for bet in GAME_BET_PRESETS[index : index + 2]:
            row.append(
                InlineKeyboardButton(
                    text=f"{bet:,} DC".replace(",", " "),
                    callback_data=f"gb:{game}:{option}:{bet}",
                )
            )
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Все игры", callback_data="games")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def jackpot_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Топ билетов", callback_data="jackpot:top"),
                InlineKeyboardButton(text="🏅 Победители", callback_data="jackpot:winners"),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="jackpot:view")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
        ]
    )


def duel_keyboard(duel_id: str, bet: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⚔️ Принять за {bet:,} DC".replace(",", " "),
                    callback_data=f"duel_accept:{duel_id}",
                )
            ],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"duel_cancel:{duel_id}")],
        ]
    )


# ==================== PAYMENTS ====================


async def create_invoice_order(path, uid, dc, stars):
    if stars <= 0 or dc < 0:
        raise ValueError("Invalid invoice")
    payload = "order:" + secrets.token_urlsafe(24)
    async with connect(path) as c:
        await c.execute(
            "INSERT INTO invoice_orders(payload,uid,dc,stars,created) VALUES (?,?,?,?,?)",
            (payload, uid, dc, stars, time.time()),
        )
        await c.commit()
    return payload


async def validate_checkout(query, path):
    async with aiosqlite.connect(path, timeout=2) as c:
        async with c.execute(
            "SELECT uid,stars,created,paid FROM invoice_orders WHERE payload=?",
            (query.invoice_payload,),
        ) as cursor:
            row = await cursor.fetchone()
        async with c.execute("SELECT 1 FROM banned_users WHERE user_id=?", (query.from_user.id,)) as cursor:
            banned = await cursor.fetchone()
    valid = bool(
        row
        and row[0] == query.from_user.id
        and (row[1] == query.total_amount)
        and (query.currency == "XTR")
        and (not row[3])
        and (not banned)
        and (time.time() - row[2] < 3600)
    )
    await query.answer(
        ok=valid,
        error_message=None if valid else "Счёт недействителен. Создайте новый счёт в меню.",
    )


async def accept_payment(db, message):
    p = message.successful_payment
    uid = message.from_user.id
    async with connect(db.path) as c:
        async with c.execute(
            "SELECT uid,dc,stars,paid FROM invoice_orders WHERE payload=?",
            (p.invoice_payload,),
        ) as cursor:
            order = await cursor.fetchone()
        async with c.execute(
            "SELECT 1 FROM star_coin_purchases WHERE payment_charge_id=?",
            (p.telegram_payment_charge_id,),
        ) as cursor:
            duplicate = await cursor.fetchone()
        if duplicate:
            return "ℹ️ Этот платёж уже зачислен."
        if not order or order[0] != uid or order[2] != p.total_amount or (p.currency != "XTR") or order[3]:
            await c.execute(
                "INSERT OR IGNORE INTO payment_review VALUES (?,?,?,?)",
                (p.telegram_payment_charge_id, uid, p.model_dump_json(), time.time()),
            )
            await c.commit()
            return "Оплата сохранена для проверки администратором. Обратитесь через /paysupport."
        await db.credit_star_coin_purchase(p.telegram_payment_charge_id, uid, order[1], order[2])
        await c.execute("UPDATE invoice_orders SET paid=1 WHERE payload=?", (p.invoice_payload,))
        await c.commit()
    return (
        f"✅ Оплачено {order[2]} Stars. Начислено {order[1]:,} DC."
        if order[1]
        else "✅ Баланс бота пополнен."
    )


# ==================== DELIVERY ====================


async def queue_gift(path, uid, gift_id):
    async with connect(path) as c:
        async with c.execute("SELECT COUNT(*) FROM gift_jobs WHERE action=?", (action_key.get(),)) as cursor:
            sequence = (await cursor.fetchone())[0]
        await c.execute(
            "INSERT INTO gift_jobs(uid,gift_id,created,action,sequence) VALUES (?,?,?,?,?)",
            (uid, gift_id, time.time(), action_key.get(), sequence),
        )
        await c.commit()


async def deliver_one(bot, path):
    async with write_lock:
        async with aiosqlite.connect(path, timeout=30) as c:
            await c.execute("BEGIN IMMEDIATE")
            async with c.execute(
                "SELECT id,uid,gift_id FROM gift_jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ) as cursor:
                job = await cursor.fetchone()
            if not job:
                await c.rollback()
                return
            await c.execute("UPDATE gift_jobs SET status='sending' WHERE id=?", (job[0],))
            await c.commit()
    status, error = ("sent", None)
    try:
        sent = await bot.send_gift(user_id=job[1], gift_id=job[2])
        if not sent:
            status, error = ("failed", "Telegram returned false")
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 0.2)
        status, error = ("pending", "rate limited")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        status, error = ("failed", type(exc).__name__ + ": " + exc.message[:200])
    except Exception as exc:
        status, error = ("unknown", type(exc).__name__)
        logger.exception("Gift %s needs reconciliation", job[0])
    async with write_lock:
        async with aiosqlite.connect(path, timeout=30) as c:
            await c.execute(
                "UPDATE gift_jobs SET status=?,error=? WHERE id=?",
                (status, error, job[0]),
            )
            await c.commit()


async def gift_worker(bot, path):
    while True:
        await deliver_one(bot, path)
        await asyncio.sleep(1)


# ==================== NOTICES ====================


async def queue_notice(path, chat_id, text):
    if not chat_id:
        return
    async with connect(path) as c:
        await c.execute(
            "INSERT INTO notices(chat_id,text,created) VALUES (?,?,?)",
            (str(chat_id), text, time.time()),
        )
        await c.commit()


async def notice_worker(bot, path):
    while True:
        async with aiosqlite.connect(path) as c:
            async with c.execute(
                "SELECT id,chat_id,text,attempts FROM notices WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            await asyncio.sleep(0.5)
            continue
        status = "sent"
        try:
            chat_id = int(row[1]) if row[1].lstrip("-").isdigit() else row[1]
            await bot.send_message(chat_id, row[2])
        except (TelegramForbiddenError, TelegramBadRequest):
            status = "failed"
            logger.exception("Permanent notification failure %s", row[0])
        except Exception:
            status = "pending" if row[3] < 7 else "failed"
            logger.exception("Notification failure %s", row[0])
        async with write_lock:
            async with aiosqlite.connect(path) as c:
                await c.execute(
                    "UPDATE notices SET status=?,attempts=attempts+1,next_attempt=? WHERE id=?",
                    (status, time.time() + min(3600, 2 ** (row[3] + 1)), row[0]),
                )
                await c.commit()


async def housekeeping(path):
    threshold = time.time() - 30 * 86400
    async with aiosqlite.connect(str(path) + ".inbox") as c:
        await c.execute("DELETE FROM inbox WHERE status='done' AND created<?", (threshold,))
        await c.execute("DELETE FROM action_randomness WHERE id NOT IN (SELECT id FROM inbox)")
        await c.commit()
    async with connect(path) as c:
        await c.execute("DELETE FROM notices WHERE status='sent' AND created<?", (threshold,))
        await c.execute("DELETE FROM support_links WHERE created_at<?", (threshold,))
        await c.commit()


# ==================== TRANSPORT ====================


def split_text(text, limit=3500):
    chunks, current, size = ([], [], 0)
    for char in text:
        units = 2 if ord(char) > 65535 else 1
        if size + units > limit:
            chunks.append("".join(current))
            current, size = ([], 0)
        current.append(char)
        size += units
    if current:
        chunks.append("".join(current))
    return chunks or [" "]


class TelegramTransport(BaseRequestMiddleware):
    def __init__(self):
        self.next_request = 0.0
        self.rate_lock = asyncio.Lock()

    async def call(self, make_request, bot, method):
        for attempt in range(4):
            try:
                if not isinstance(method, AnswerCallbackQuery):
                    async with self.rate_lock:
                        await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                        self.next_request = time.monotonic() + 0.05
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt == 3:
                    raise
                await asyncio.sleep(exc.retry_after + 0.2)
            except TelegramNetworkError:
                if isinstance(method, AnswerCallbackQuery):
                    logger.info("Callback acknowledgement unavailable")
                    return True
                raise
            except TelegramBadRequest as exc:
                if isinstance(method, EditMessageText) and "message is not modified" in exc.message.lower():
                    return True
                if isinstance(method, AnswerCallbackQuery) and any(
                    (word in exc.message.lower() for word in ("query is too old", "query id is invalid"))
                ):
                    return True
                raise

    async def __call__(self, make_request, bot, method):
        if isinstance(method, AnswerCallbackQuery) and method.text:
            method = method.model_copy(update={"text": split_text(method.text, 190)[0]})
        if not isinstance(method, (SendMessage, EditMessageText)):
            return await self.call(make_request, bot, method)
        chunks = split_text(method.text)
        if len(chunks) == 1:
            return await self.call(make_request, bot, method)
        if method.parse_mode in ("HTML", "Markdown", "MarkdownV2") or method.entities:
            raise ValueError("Paginate formatted text before sending")
        result = await self.call(make_request, bot, method.model_copy(update={"text": chunks[0]}))
        for chunk in chunks[1:]:
            await self.call(make_request, bot, SendMessage(chat_id=method.chat_id, text=chunk))
        return result


# ==================== HANDLERS ====================


active_games: dict = {}
pending_game_bets: dict[int, dict] = {}
active_duels: dict[str, dict] = {}
duel_by_user: dict[int, str] = {}
db = Database(os.getenv("DB_PATH", "activity.db"))


def case_reward_parts(reward: tuple) -> tuple[str, int, float]:
    if isinstance(reward[0], str):
        kind, value, weight = reward
    else:
        value, weight = reward
        kind = "coins"
    return (kind, int(value), float(weight))


def case_reward_tuple(original: tuple, weight: float) -> tuple:
    if isinstance(original[0], str):
        return (original[0], original[1], float(weight))
    return (original[0], float(weight))


async def load_runtime_settings() -> None:
    settings = await db.get_settings()
    for key in ECONOMY:
        stored = settings.get(f"economy:{key}")
        if stored is not None and math.isfinite(stored) and (1 <= stored <= 1000000000):
            ECONOMY[key] = int(stored)
    for dc_amount in STAR_DC_PACKAGES:
        stored = settings.get(f"stars:{dc_amount}")
        if stored is not None and math.isfinite(stored) and (1 <= stored <= 1000000000):
            STAR_DC_PACKAGES[dc_amount] = int(stored)
    for case_id, case in CASES.items():
        updated_rewards = []
        for index, reward in enumerate(case["rewards"]):
            stored = settings.get(f"case_chance:{case_id}:{index}")
            updated_rewards.append(
                case_reward_tuple(
                    reward,
                    stored
                    if stored is not None and math.isfinite(stored) and (stored >= 0)
                    else case_reward_parts(reward)[2],
                )
            )
        total_weight = sum((case_reward_parts(reward)[2] for reward in updated_rewards))
        if not math.isfinite(total_weight) or total_weight <= 0:
            logger.warning("Invalid case weights for %s; keeping defaults", case_id)
            continue
        if total_weight > 0:
            updated_rewards = [
                case_reward_tuple(reward, case_reward_parts(reward)[2] * 100.0 / total_weight)
                for reward in updated_rewards
            ]
        case["rewards"] = updated_rewards


async def save_case_chances(case_id: str) -> None:
    for index, reward in enumerate(CASES[case_id]["rewards"]):
        await db.set_setting(f"case_chance:{case_id}:{index}", case_reward_parts(reward)[2])


def display_name(user) -> str:
    return user.full_name or str(user.id)


def gift_roll_wins(chance: float) -> bool:
    return chance >= MAX_CHANCE or random.uniform(0, GIFT_WIN_ROLL_MAX) <= chance


async def send_log(bot: Bot, text: str) -> None:
    await queue_notice(db.path, LOG_CHAT_ID, text)


async def send_game_log(bot: Bot, text: str) -> None:
    await queue_notice(db.path, GAME_LOG_CHAT_ID, text)


async def publish_promo(bot: Bot, text: str) -> bool:
    try:
        await bot.send_message(PROMO_CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning("publish_promo failed: %s", e)
        return False


HIDDEN_PROMO_FLAGS = {"скрытый", "скрыто", "hidden", "private"}


def parse_hidden_promo_args(text: str) -> tuple[list[str], bool]:
    """Убирает необязательный флаг скрытого промокода из конца команды."""
    args = text.split()
    hidden = bool(args and args[-1].lower() in HIDDEN_PROMO_FLAGS)
    return (args[:-1] if hidden else args, hidden)


async def send_bot_gift(bot: Bot, user_id: int, gift_id: str) -> str:
    await queue_gift(db.path, user_id, gift_id)
    return "очередь доставки"


async def send_gift_safe(bot: Bot, user_id: int, user_name: str, gift_id: str, reason: str) -> None:
    await send_bot_gift(bot, user_id, gift_id)
    await send_log(bot, f"🎁 Подарок в очереди: {user_name} ({user_id}); {reason}")


async def casino_timeout_checker(bot: Bot) -> None:
    for uid, game in list(active_games.items()):
        if game["expires"] > time.time():
            continue
        active_games.pop(uid)
        if game["game"] == "mines" and game.get("opened"):
            refund = mines_prize(game["bet"], len(game["opened"]))
        else:
            refund = game["bet"]
        await db.add_coins(uid, refund)
        if game["game"] == "mines" and game.get("opened"):
            from aiogram.types import User

            user = User(id=uid, is_bot=False, first_name=await db.get_user_name(uid))
            await school_game(user, f"mines:{game['token']}", game["bet"], True)
        await send_log(bot, f"Тайм-аут игры {uid}: начислено {refund} DC")
        try:
            await bot.send_message(
                game["chat_id"],
                f"⏰ Игра завершена по времени. Начислено {refund:,} DC.",
            )
        except Exception:
            logger.exception("Cannot notify about game timeout")
    for uid, state in list(pending_game_bets.items()):
        if state["expires"] <= time.time():
            pending_game_bets.pop(uid, None)


def release_duel(duel_id: str) -> dict | None:
    duel = active_duels.pop(duel_id, None)
    if not duel:
        return None
    for user_id in (duel["challenger_id"], duel.get("opponent_id")):
        if user_id is not None and duel_by_user.get(user_id) == duel_id:
            duel_by_user.pop(user_id, None)
    return duel


async def duel_timeout_checker(bot: Bot) -> None:
    now = time.time()
    expired_ids = [
        duel_id
        for duel_id, duel in active_duels.items()
        if duel.get("status") == "open" and duel["expires"] <= now
    ]
    for duel_id in expired_ids:
        duel = release_duel(duel_id)
        if not duel:
            continue
        try:
            await bot.edit_message_text(
                chat_id=duel["chat_id"],
                message_id=duel["message_id"],
                text=f"⌛ Дуэль отменена\n\nИгрок: {duel['challenger_name']}\nСтавка: {duel['bet']:,} DC\nНикто не принял вызов за 5 минут.".replace(
                    ",", " "
                ),
            )
        except Exception as e:
            logger.warning("Could not expire duel %s: %s", duel_id, e)


router = Router()
cooldowns: TTLCache = TTLCache(maxsize=50000, ttl=COOLDOWN_SECONDS)
casino_bet_cooldowns: TTLCache = TTLCache(maxsize=50000, ttl=CASINO_BET_COOLDOWN)
case_open_cooldowns: TTLCache = TTLCache(maxsize=50000, ttl=CASE_OPEN_COOLDOWN)
duel_cooldowns: TTLCache = TTLCache(maxsize=50000, ttl=DUEL_COOLDOWN)
game_action_lock = asyncio.Lock()
school_prize_delivery_lock = asyncio.Lock()
school_event = SchoolEvent(db)


def event_navigation():
    return [
        [
            InlineKeyboardButton(text="📚 Босс", callback_data="school:boss"),
            InlineKeyboardButton(text="🏆 Топ по урону", callback_data="school:top"),
        ],
        [
            InlineKeyboardButton(text="⭐ Квесты", callback_data="school:quests"),
            InlineKeyboardButton(text="📖 Призовой путь", callback_data="school:path:0"),
        ],
        [InlineKeyboardButton(text="🏅 Топ-10 призового пути", callback_data="school:path_top")],
    ]


async def event_page(uid, name, page):
    buttons = []
    async with school_event.transaction() as c:
        season = await school_event.current(c, active=False)
        if not season:
            return (
                "🏫 Школьный ивент ещё не запущен.",
                InlineKeyboardMarkup(inline_keyboard=event_navigation()),
            )
        sid = season["id"]
        now = time.time()
        active = not season["stopped"] and now < season["ends"]
        day = (
            await school_event.prepare(c, sid, uid, name, now)
            if active
            else datetime.fromtimestamp(now, pytz.timezone("Europe/Moscow")).date().isoformat()
        )
        p = await school_event.one(c, "SELECT * FROM school_players WHERE season=? AND uid=?", (sid, uid))
        knowledge = p["knowledge"] if p else 0
        footer = "" if active else "\n\n⏳ Ивент завершён."
        if page == "quests":
            async with c.execute(
                "SELECT * FROM school_quests WHERE season=? AND uid=? AND day=? ORDER BY quest",
                (sid, uid, day),
            ) as cur:
                quests = await cur.fetchall()
            lines = [
                f"⭐ Квесты · {day} (МСК)",
                f"📖 Очки знаний: {knowledge}",
                "Смена заданий в 00:00 МСК. Награды забирай сегодня.",
                "",
            ]
            for row in quests:
                label, _, target, dc, points = QUESTS[row["quest"]]
                lines.append(f"{label}\n{row['progress']}/{target} · {dc:,} DC + {points} 📖")
                if active and row["progress"] >= target and (not row["claimed"]):
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                text=f"🎁 Забрать: {label}",
                                callback_data=f"scq:{sid}:{uid}:{day}/{row['quest']}",
                            )
                        ]
                    )
                elif row["claimed"]:
                    lines.append("✅ Получено")
            today = await school_event.one(
                c,
                "SELECT * FROM school_days WHERE season=? AND uid=? AND day=?",
                (sid, uid, day),
            )
            yesterday = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
            prev = await school_event.one(
                c,
                "SELECT streak FROM school_days WHERE season=? AND uid=? AND day=?",
                (sid, uid, yesterday),
            )
            streak = today["streak"] if today else prev[0] % 5 if prev else 0
            lines.append(f"\n🔥 Серия: {streak}/5 дней\nЗа все 3 задания: 3 000 DC. За 5 дней: 🔑 Отличника.")
            if active and today and (not today["claimed"]):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text="🎁 Бонус за все квесты",
                            callback_data=f"scd:{sid}:{uid}:{day}",
                        )
                    ]
                )
            buttons.append([InlineKeyboardButton(text="🔄 Обновить задания", callback_data="school:quests")])
            text = "\n".join(lines) + footer
        elif page == "path_top":
            async with c.execute(
                "SELECT uid,name,knowledge FROM school_players WHERE season=? AND knowledge>0 ORDER BY knowledge DESC,uid LIMIT 10",
                (sid,),
            ) as cur:
                leaders = await cur.fetchall()
            async with c.execute(
                "SELECT uid FROM school_players WHERE season=? AND knowledge>0 ORDER BY knowledge DESC,uid",
                (sid,),
            ) as cur:
                all_ranked = await cur.fetchall()
            rank = next(
                (index for index, row in enumerate(all_ranked, 1) if row["uid"] == uid),
                None,
            )
            lines = ["🏅 Топ-10 призового пути", ""]
            lines.extend(
                (
                    f"{index}. {row['name']} — {row['knowledge']:,} 📖 · уровень {min(50, row['knowledge'] // 100)}"
                    for index, row in enumerate(leaders, 1)
                )
            )
            if not leaders:
                lines.append("Пока никто не получил очки знаний.")
            lines.append(f"\nТвоё место: {rank or '—'} · {knowledge:,} 📖")
            text = "\n".join(lines) + footer
            buttons.append([InlineKeyboardButton(text="🔄 Обновить топ", callback_data="school:path_top")])
        elif page.startswith("path"):
            try:
                offset = max(0, min(4, int(page.split(":")[1])))
            except (IndexError, ValueError):
                offset = 0
            async with c.execute(
                "SELECT prize FROM school_claims WHERE season=? AND uid=?", (sid, uid)
            ) as cur:
                claimed = {r[0] for r in await cur.fetchall()}
            lines = [
                f"📖 Призовой путь · {knowledge}/5 000",
                f"Уровень: {min(50, knowledge // 100)}/50",
                "Каждые 100 📖 — новый уровень. Первый на 50-м получает NFT.",
                "",
            ]
            for level in range(offset * 10 + 1, offset * 10 + 11):
                status = "✅" if f"level:{level}" in claimed else "🎁" if knowledge >= level * 100 else "🔒"
                lines.append(f"{status} {level}. {event_reward_text(PATH_REWARDS[level])}")
                if active and status == "🎁":
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                text=f"Забрать уровень {level}",
                                callback_data=f"scl:{sid}:{uid}:{level}",
                            )
                        ]
                    )
            if active and any(
                (knowledge >= level * 100 and f"level:{level}" not in claimed for level in PATH_REWARDS)
            ):
                buttons.insert(
                    0,
                    [
                        InlineKeyboardButton(
                            text="🎁 Забрать все доступные награды",
                            callback_data=f"scla:{sid}:{uid}:{offset}",
                        )
                    ],
                )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=str(i * 10 + 1) + "–" + str(i * 10 + 10),
                        callback_data=f"school:path:{i}",
                    )
                    for i in range(5)
                ]
            )
            text = "\n".join(lines) + footer
        else:
            stage = int(season["boss_stage"] or 1)
            boss = SCHOOL_BOSSES.get(stage, SCHOOL_BOSSES[2])
            async with c.execute(
                "SELECT * FROM school_players WHERE season=? AND damage>0 ORDER BY damage DESC,reached,uid",
                (sid,),
            ) as cur:
                ranked = await cur.fetchall()
            rank = next((str(i) for i, r in enumerate(ranked, 1) if r["uid"] == uid), "—")
            if page == "top":
                text = f"🏆 Топ по урону · {boss['name']}\n\n" + (
                    "\n".join((f"{i}. {r['name']} — {r['damage']:,}" for i, r in enumerate(ranked[:10], 1)))
                    or "Урона пока нет."
                )
                text += f"\n\nТвоё место: {rank}\n{boss['top_text']}" + footer
                buttons.append([InlineKeyboardButton(text="🔄 Обновить топ", callback_data="school:top")])
            else:
                hours = max(0, int((season["ends"] - now) / 3600))
                text = f"{boss['name']}\n❤️ {season['hp']:,} / {season['max_hp']:,} HP\n⚔️ Твой урон: {(p['damage'] if p else 0):,}\n🏆 Твоё место: {rank}\n⏳ Осталось: {hours // 24} д. {hours % 24} ч."
                seals_removed = int(season["seal_stage"] or 0)
                if stage == 2 and seals_removed:
                    text += f"\n🔮 Снято печатей: {seals_removed} / {MAGISTER_SEAL_COUNT}"
                text += "\n\nПроигранные ставки наносят урон. Дуэли не учитываются. Очки знаний идут только в призовой путь."
                if stage == 1:
                    text += "\nЗа победу: от 10 000 урона — 10 000 DC; от 100 000 — также ключ Отличника. Последний удар: 50 000 DC. После победы появится Магистр Забвений."
                else:
                    text += "\nТоп-1, топ-2 и топ-3 получают NFT. Последний удар: 250 000 DC."
                if season["hp"] == 0:
                    text += "\n\n🏆 Магистр Забвений побеждён! Рейтинг зафиксирован."
                text += footer
                buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="school:boss")])
    buttons.extend(event_navigation())
    return (text.replace(",", " "), InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(F.text.regexp("(?i)^/?(ивент|квесты|путь|event|quests|path)(?:@\\w+)?$"))
async def school_command(message: Message):
    if not message.from_user or message.sender_chat or message.from_user.is_bot:
        return
    if message.chat.type != "private" and message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        return
    word = message.text.lower().lstrip("/").split("@")[0]
    page = "quests" if word in {"квесты", "quests"} else "path:0" if word in {"путь", "path"} else "boss"
    text, keyboard = await event_page(message.from_user.id, display_name(message.from_user), page)
    await message.answer(text, reply_markup=keyboard)


async def school_edit(callback, page):
    text, keyboard = await event_page(callback.from_user.id, display_name(callback.from_user), page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as error:
        if "message is not modified" not in str(error).lower():
            raise


async def deliver_school_gifts(bot: Bot, sid: int, uid: int) -> tuple[int, int]:
    """Send claimed path gifts immediately; failed gifts remain in the admin queue."""
    sent = failed = 0
    async with school_event.transaction() as c:
        async with c.execute(
            "SELECT id,amount FROM school_prizes WHERE season=? AND uid=? AND kind='gift' AND done=0 AND attempts<3 ORDER BY id",
            (sid, uid),
        ) as cur:
            prizes = await cur.fetchall()
    gift_sizes = {15: 5, 25: 10, 50: 15, 100: 20}
    for prize in prizes:
        try:
            gift_id = random.choice(GIFT_IDS[gift_sizes[prize["amount"]]])
            await send_bot_gift(bot, uid, gift_id)
            async with school_event.transaction() as c:
                await c.execute(
                    "UPDATE school_prizes SET done=1 WHERE id=? AND done=0",
                    (prize["id"],),
                )
            sent += 1
        except Exception as error:
            failed += 1
            async with school_event.transaction() as c:
                await c.execute(
                    "UPDATE school_prizes SET attempts=attempts+1 WHERE id=? AND done=0",
                    (prize["id"],),
                )
            logger.warning("Automatic school gift failed for %s: %s", uid, error)
    return (sent, failed)


@router.callback_query(F.data.startswith("school:"))
async def school_callback(callback: CallbackQuery):
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    await school_edit(callback, callback.data.split(":", 1)[1])
    await callback.answer()


@router.callback_query(
    F.data.startswith("scq:")
    | F.data.startswith("scd:")
    | F.data.startswith("scl:")
    | F.data.startswith("scla:")
)
async def school_claim_callback(callback: CallbackQuery, bot: Bot):
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    try:
        kind, sid, owner, value = callback.data.split(":", 3)
        if int(owner) != callback.from_user.id:
            await callback.answer("Это награда другого игрока.", show_alert=True)
            return
        category = {"scq": "quest", "scd": "day", "scl": "level", "scla": "levels"}[kind]
        result = await school_event.claim(
            int(sid), int(owner), display_name(callback.from_user), category, value
        )
        if kind in {"scl", "scla"}:
            sent, failed = await deliver_school_gifts(bot, int(sid), int(owner))
            if sent:
                result += f" Подарков поставлено в очередь: {sent}."
            if failed:
                result += f" Не удалось отправить: {failed}; оставлено в очереди админу."
    except (ValueError, KeyError):
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    await callback.answer(result[:190], show_alert=True)
    if kind == "scl":
        page = f"path:{(int(value) - 1) // 10}"
    elif kind == "scla":
        page = f"path:{max(0, min(4, int(value)))}"
    else:
        page = "quests"
    await school_edit(callback, page)


async def school_admin_page(callback, page=0):
    async with school_event.transaction() as c:
        season = await school_event.current(c, active=False)
        async with c.execute(
            "SELECT * FROM school_prizes WHERE done=0 ORDER BY id LIMIT 10 OFFSET ?",
            (page * 10,),
        ) as cur:
            prizes = await cur.fetchall()
        total = (await school_event.one(c, "SELECT COUNT(*) FROM school_prizes WHERE done=0"))[0]
    text = "🏫 Управление школьным ивентом\n"
    if season:
        text += f"Сезон #{season['id']} · HP {season['hp']:,}/{season['max_hp']:,}\n"
        text += (
            "Окончание: "
            + datetime.fromtimestamp(season["ends"], pytz.timezone("Europe/Moscow")).strftime(
                "%d.%m.%Y %H:%M МСК"
            )
            + "\n"
        )
    text += f"\n🎁 Ожидают ручной выдачи: {total}\nСначала выдай приз, затем отметь заявку.\n"
    buttons = [[InlineKeyboardButton(text="▶️ Запустить на 20 дней", callback_data="sca:startask")]]
    if season:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⏹ Завершить ивент",
                    callback_data=f"sca:stopask:{season['id']}",
                )
            ]
        )
    for row in prizes:
        text += f"\n#{row['id']} · ID {row['uid']} · {row['reason']} · {event_reward_text([(row['kind'], row['amount'])])}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Приз #{row['id']} выдан",
                    callback_data=f"sca:doneask:{row['id']}",
                )
            ]
        )
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sca:page:{page - 1}"))
    if (page + 1) * 10 < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sca:page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="sca:page:0"),
            InlineKeyboardButton(text="◀️ Админка", callback_data="admin_panel"),
        ]
    )
    try:
        await callback.message.edit_text(
            text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data.startswith("sca:"))
async def school_admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID or callback.message.chat.type != "private":
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1]
    needs_id = action in {"stop", "stopask", "done", "doneask", "page"}
    if (
        action not in {"start", "startask", "stop", "stopask", "done", "doneask", "page"}
        or len(parts) != (3 if needs_id else 2)
        or (needs_id and (not parts[2].isascii() or not parts[2].isdigit() or len(parts[2]) > 15))
    ):
        await callback.answer("Некорректная или устаревшая кнопка.", show_alert=True)
        return
    if action.endswith("ask"):
        confirm = callback.data.replace("ask", "", 1)
        descriptions = {
            "startask": "Запустить новый ивент на 20 дней с 5 000 000 HP?",
            "stopask": "Завершить ивент сейчас? Прогресс и получение наград остановятся. Награды за непобеждённого босса не выдаются.",
            "doneask": "Приз действительно выдан игроку?",
        }
        await callback.message.edit_text(
            descriptions[action],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Подтвердить", callback_data=confirm)],
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="sca:page:0")],
                ]
            ),
        )
        await callback.answer()
        return
    if action == "start":
        ok = await school_event.start()
        await callback.answer("Ивент запущен" if ok else "Ивент уже идёт", show_alert=True)
    elif action == "stop":
        await school_event.stop(int(parts[2]))
        await callback.answer("Ивент завершён")
    elif action == "done":
        async with school_event.transaction() as c:
            await c.execute(
                "UPDATE school_prizes SET done=1 WHERE id=? AND done=0",
                (int(parts[2]),),
            )
        await callback.answer("Выдача отмечена")
    else:
        await callback.answer()
    await school_admin_page(callback, max(0, int(parts[2])) if action == "page" else 0)


async def school_notifications(bot):
    try:
        async with school_event.transaction() as c:
            async with c.execute("SELECT * FROM school_outbox WHERE sent=0 ORDER BY id LIMIT 5") as cur:
                rows = await cur.fetchall()
        for row in rows:
            await bot.send_message(MAIN_CHAT_ID, row["text"])
            async with school_event.transaction() as c:
                await c.execute("UPDATE school_outbox SET sent=1 WHERE id=?", (row["id"],))
        async with school_event.transaction() as c:
            async with c.execute(
                "SELECT DISTINCT season,uid FROM school_prizes WHERE kind='gift' AND done=0 AND attempts<3 LIMIT 10"
            ) as cur:
                gift_owners = await cur.fetchall()
        for owner in gift_owners:
            await deliver_school_gifts(bot, owner["season"], owner["uid"])
    except Exception:
        logger.exception("School event notification failed")


async def bonus_notification_worker(bot: Bot) -> None:
    try:
        async with connect(db.path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT d.broadcast_id,d.user_id,b.amount,b.reward_type,b.case_id,d.attempts FROM bonus_deliveries d JOIN bonus_broadcasts b ON b.id=d.broadcast_id WHERE d.status=0 ORDER BY d.broadcast_id,d.user_id LIMIT 20"
            ) as cur:
                rows = await cur.fetchall()
        for row in rows:
            try:
                if row["reward_type"] == "case" and row["case_id"] in CASES:
                    amount_text = f"{row['amount']} ключ" if row["amount"] == 1 else f"{row['amount']} ключа"
                    notification = f"🎁 Администратор раздал всем игрокам кейсы!\n🔑 Тебе начислено: {amount_text} от кейса {CASES[row['case_id']]['title']}."
                else:
                    notification = f"🎁 Администратор раздал всем игрокам {row['amount']:,} DC!\n🪙 Монеты уже зачислены на твой баланс.".replace(
                        ",", " "
                    )
                await bot.send_message(row["user_id"], notification)
                status, error = (1, None)
            except Exception as exc:
                status = 2 if row["attempts"] >= 2 else 0
                error = str(exc)[:300]
            async with connect(db.path) as conn:
                await conn.execute(
                    "UPDATE bonus_deliveries SET status=?,attempts=attempts+1,error=? WHERE broadcast_id=? AND user_id=? AND status=0",
                    (status, error, row["broadcast_id"], row["user_id"]),
                )
                await conn.commit()
            await asyncio.sleep(0.05)
    except Exception:
        logger.exception("Bonus notification worker failed")


async def school_game(user, token, bet, won):
    damage, refund = await school_event.record(
        user.id,
        display_name(user),
        token,
        {"games": 1, "gamewin": int(won), "bets": bet},
        loss=0 if won else bet,
    )
    if not won:
        await jackpot_loss(user, max(0, bet - refund), f"{token}:jackpot")
    return f"\n📚 Урон боссу: {damage:,}. Возвращено: {refund:,} DC" if damage else ""


async def jackpot_loss(user, loss: int, token: str) -> tuple[int, int, int]:
    return await db.record_jackpot_loss(user.id, display_name(user), loss, token)


async def jackpot_draw_task(bot: Bot) -> None:
    for outcome in await db.draw_pending_jackpots():
        if outcome["kind"] != "winner":
            continue
        await queue_notice(
            db.path,
            MAIN_CHAT_ID,
            f"🏆 Джекпот дня разыгран!\nПобедитель: {outcome['name']}\nВыигрыш: {outcome['pool']:,} DC",
        )
        await queue_notice(
            db.path,
            outcome["user_id"],
            f"🏆 Ты выиграл Джекпот дня! Начислено: {outcome['pool']:,} DC",
        )


def mini_event_text(event: dict) -> str:
    reward = f"{event['reward']:,}".replace(",", " ")
    if event["kind"] == "first":
        return f"⚡ Быстрый мини-ивент!\n\nПервый, кто нажмёт кнопку, получит {reward} DC.\nУспей забрать награду!"
    if event["kind"] == "first_n":
        return f"⚡ Быстрый мини-ивент!\n\nПервые {event['max_participants']} участников получат по {reward} DC.\nНажми кнопку, чтобы участвовать."
    return f"🎟 Счастливый билет\n\nНажми кнопку до окончания регистрации.\nОдин участник случайно получит {reward} DC!"


def mini_event_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚡ Участвовать", callback_data=f"mini:join:{event_id}")]]
    )


@router.callback_query(F.data.startswith("mini:join:"))
async def mini_event_join_callback(callback: CallbackQuery, bot: Bot) -> None:
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    try:
        event_id = int(callback.data.removeprefix("mini:join:"))
    except (AttributeError, ValueError):
        await callback.answer("Событие не найдено.", show_alert=True)
        return
    result = await db.join_mini_event(event_id, callback.from_user.id, display_name(callback.from_user))
    status = result["status"]
    if status == "already":
        await callback.answer("Ты уже участвуешь.", show_alert=True)
        return
    if status in {"finished", "expired"}:
        await callback.answer("Событие уже завершено.", show_alert=True)
        return
    if result["kind"] == "ticket":
        await callback.answer("🎟 Ты участвуешь в розыгрыше!")
        return
    message = f"✅ Награда {result['reward']:,} DC зачислена!".replace(",", " ")
    if result["complete"]:
        try:
            await callback.message.edit_text(
                f"⚡ Мини-ивент завершён!\n\nВсе {result['max']} наград уже забраны."
            )
        except TelegramBadRequest:
            pass
    await callback.answer(message, show_alert=True)


async def mini_event_task(bot: Bot) -> None:
    try:
        outcomes = await db.finish_due_mini_events()
        for outcome in outcomes:
            event = outcome["event"]
            if outcome["kind"] == "expired" and event["kind"] == "ticket":
                try:
                    await bot.send_message(
                        event["chat_id"],
                        "⌛ Счастливый билет завершён: участников не было.",
                    )
                except Exception:
                    pass
        for event in await db.pending_mini_event_announcements():
            text = f"🎟 Счастливый билет разыгран!\n\n🏆 Победитель: {event['winner_name']}\n💰 Награда: {event['reward']:,} DC\n👥 Участников: {event['participant_count']}".replace(
                ",", " "
            )
            try:
                await bot.send_message(event["chat_id"], text)
            except Exception as error:
                logger.warning("Could not publish mini event result: %s", error)
                continue
            await db.mark_mini_event_announced(event["id"])
            try:
                await bot.send_message(
                    event["winner_id"],
                    f"🏆 Ты выиграл в событии «Счастливый билет»!\n💰 Зачислено: {event['reward']:,} DC".replace(
                        ",", " "
                    ),
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Mini event task failed")


async def golden_hour_task(bot: Bot) -> None:
    for hour, top in await school_event.finish_golden_hours():
        lines = ["🏁 Золотой час завершён!", "", "Лимиты бонусов сброшены для следующего запуска."]
        if top:
            lines.extend(["", "🏆 Топ Золотого часа:"])
            lines.extend(
                f"{place}. {player['name']} — ⚔️ +{player['bonus_damage']:,} урона · 🎮 {player['games']}/{GOLDEN_HOUR_GAMES_LIMIT} игр"
                for place, player in enumerate(top, 1)
            )
        try:
            await bot.send_message(REQUIRED_CHANNEL, "\n".join(lines).replace(",", " "))
        except Exception:
            logger.exception("Could not publish Golden Hour result")


PLAIN_COMMANDS = {
    "start",
    "help",
    "say",
    "channel",
    "mini",
    "vip",
    "unvip",
    "viplist",
    "ban",
    "unban",
    "banlist",
    "addmsgs",
    "removemsgs",
    "addday",
    "removeday",
    "addcoins",
    "removecoins",
    "addvisual",
    "removevisual",
    "createpromo",
    "deletepromo",
    "createcasepromo",
    "promos",
    "balance",
    "popolnit",
    "sendgift",
    "pending",
    "deliver",
    "deletepending",
    "premiumorders",
    "premiumdone",
    "premiumrefund",
    "stats",
    "top",
    "winstop",
    "cointop",
    "coins",
    "promo",
    "transfer",
    "daytop",
    "bonus",
    "cases",
    "slots",
    "roulette",
    "dice",
    "mines",
    "scratch",
    "coinflip",
    "lottery",
    "duel",
    "exchange",
    "admin",
    "bossdamage",
    "eventdays",
    "goldenhour",
    "broadcast",
    "supportgrant",
    "supportrevoke",
    "supportlist",
    "supportreply",
    "supportclose",
}
RUSSIAN_COMMANDS = {
    "старт": "start",
    "начать": "start",
    "помощь": "help",
    "канал": "channel",
    "мини": "mini",
    "кейсы": "cases",
    "баланс": "coins",
    "монеты": "coins",
    "обмен": "exchange",
    "бонус": "bonus",
    "стата": "stats",
    "статистика": "stats",
    "топ": "top",
    "победители": "winstop",
    "топкоинов": "cointop",
    "дневнойтоп": "daytop",
    "промо": "promo",
    "перевод": "transfer",
    "слоты": "slots",
    "рулетка": "roulette",
    "кубик": "dice",
    "мины": "mines",
    "скретч": "scratch",
    "скретчкарты": "scratch",
    "скретч-карты": "scratch",
    "монетка": "coinflip",
    "орелрешка": "coinflip",
    "орёлрешка": "coinflip",
    "лотерея": "lottery",
    "лотерея5x5": "lottery",
    "удалитьзаявку": "deletepending",
    "добавитьвизуал": "addvisual",
    "убратьвизуал": "removevisual",
    "админ": "admin",
    "админка": "admin",
    "дуэль": "duel",
    "дуель": "duel",
    "уронбоссу": "bossdamage",
    "ударитьбосса": "bossdamage",
    "дниивента": "eventdays",
    "золотойчас": "goldenhour",
    "раздать": "broadcast",
    "датьдоступ": "supportgrant",
    "убратьдоступ": "supportrevoke",
    "доступы": "supportlist",
    "ответ": "supportreply",
    "закрытьчат": "supportclose",
}


def parse_plain_command(text: str | None):
    if not text or text.startswith("/"):
        return None
    parts = text.strip().split()
    if not parts:
        return None
    command = RUSSIAN_COMMANDS.get(parts[0].lower(), parts[0].lower())
    if command not in PLAIN_COMMANDS:
        return None
    if command == "roulette" and len(parts) > 1:
        colors = {
            "красное": "red",
            "красный": "red",
            "черное": "black",
            "чёрное": "black",
            "черный": "black",
            "чёрный": "black",
        }
        parts[1] = colors.get(parts[1].lower(), parts[1])
    if command == "coinflip" and len(parts) > 1:
        sides = {
            "орел": "heads",
            "орёл": "heads",
            "решка": "tails",
            "heads": "heads",
            "tails": "tails",
        }
        parts[1] = sides.get(parts[1].lower(), parts[1])
    return (command, parts[1:])


def is_plain_command(message: Message) -> bool:
    return parse_plain_command(message.text) is not None


async def is_channel_subscriber(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = getattr(member.status, "value", member.status)
        return status in {"creator", "administrator", "member"} or (
            status == "restricted" and member.is_member
        )
    except Exception as e:
        logger.warning("Subscription check failed for %s: %s", user_id, e)
        return False


async def send_subscription_prompt(message: Message) -> None:
    await message.answer(
        "📢 Чтобы пользоваться ботом, подпишись на наш канал.",
        reply_markup=subscription_keyboard(),
    )


@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    if await db.is_banned(message.from_user.id):
        await message.answer(BAN_MESSAGE)
        return
    if message.from_user.username:
        await db.set_username(
            message.from_user.id,
            message.from_user.username,
            message.from_user.id,
            display_name(message.from_user),
        )
    await db.register_bot_user(message.from_user.id, display_name(message.from_user))
    if not await is_channel_subscriber(bot, message.from_user.id):
        await send_subscription_prompt(message)
        return
    support_access = await db.has_support_access(message.from_user.id)
    await message.answer(
        "👋 Добро пожаловать!\n\nВыберите действие:",
        reply_markup=start_keyboard(message.from_user.id == ADMIN_ID, support_access),
    )


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_channel_subscriber(bot, callback.from_user.id):
        await callback.answer("❌ Подписка пока не найдена.", show_alert=True)
        return
    await db.register_bot_user(callback.from_user.id, display_name(callback.from_user))
    support_access = await db.has_support_access(callback.from_user.id)
    await callback.message.edit_text(
        "👋 Добро пожаловать!\n\nВыберите действие:",
        reply_markup=start_keyboard(callback.from_user.id == ADMIN_ID, support_access),
    )
    await callback.answer("✅ Подписка подтверждена")


@router.callback_query(F.data == "support_open")
async def support_open_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    if not await db.has_support_access(user_id):
        await callback.answer("❌ У тебя нет доступа к чату с администратором.", show_alert=True)
        return
    if not await is_channel_subscriber(bot, user_id):
        await callback.answer("❌ Сначала подпишись на канал.", show_alert=True)
        return
    await db.set_support_session(user_id, True)
    await callback.message.answer(
        "💬 Диалог с администратором открыт.\n\nТеперь отправляй сюда сообщения, фото, видео, документы или голосовые — администратор ответит через бота.\n\n⚠️ Никогда не отправляй коды входа Telegram, пароль 2FA или строку сессии.",
        reply_markup=support_chat_keyboard(),
    )
    await callback.answer("Диалог открыт")


@router.callback_query(F.data == "support_close")
async def support_close_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    await db.set_support_session(user_id, False)
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔒 Пользователь {display_name(callback.from_user)} ({user_id}) закрыл диалог.",
        )
    except Exception as error:
        logger.warning("Could not notify admin about closed support chat: %s", error)
    await callback.message.edit_text("🔒 Диалог с администратором закрыт.")
    await callback.answer("Диалог закрыт")


async def parse_support_user_id(message: Message, usage: str) -> int | None:
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(usage)
        return None
    try:
        return int(args[1])
    except ValueError:
        await message.answer("❌ Укажи числовой ID пользователя.")
        return None


@router.message(Command("supportgrant"), F.chat.type == "private")
async def cmd_supportgrant(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    user_id = await parse_support_user_id(message, "Использование: supportgrant ID")
    if user_id is None:
        return
    await db.grant_support_access(user_id)
    name = await db.get_user_name(user_id)
    await message.answer(f"✅ Доступ к чату выдан: {name} ({user_id}).")
    try:
        await bot.send_message(
            user_id,
            "✅ Тебе открыт доступ к чату с администратором.\n\n⚠️ Не отправляй коды входа Telegram, пароль 2FA или строку сессии.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Открыть диалог", callback_data="support_open")]
                ]
            ),
        )
    except Exception as error:
        logger.info("Could not notify support user %s: %s", user_id, error)


@router.message(Command("supportrevoke"), F.chat.type == "private")
async def cmd_supportrevoke(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    user_id = await parse_support_user_id(message, "Использование: supportrevoke ID")
    if user_id is None:
        return
    removed = await db.revoke_support_access(user_id)
    await message.answer(
        "✅ Доступ отозван, диалог закрыт." if removed else "ℹ️ У пользователя не было доступа."
    )
    if removed:
        try:
            await bot.send_message(user_id, "🔒 Администратор закрыл диалог и отозвал доступ.")
        except Exception as error:
            logger.info("Could not notify revoked support user %s: %s", user_id, error)


@router.message(Command("supportlist"), F.chat.type == "private")
async def cmd_supportlist(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    users = await db.get_support_access_list()
    if not users:
        await message.answer("📭 Доступ к чату никому не выдан.")
        return
    text = "💬 Доступ к чату с администратором:\n\n" + "\n".join(
        (f"• {name} — {user_id}" for user_id, name, _ in users)
    )
    await message.answer(text[:4000])


@router.message(Command("supportreply"), F.chat.type == "private")
async def cmd_supportreply(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = (message.text or "").split(maxsplit=2)
    if len(args) != 3:
        await message.answer("Использование: supportreply ID текст")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("❌ Укажи числовой ID пользователя.")
        return
    if not await db.has_support_access(user_id):
        await message.answer("❌ У пользователя нет доступа к чату.")
        return
    if not await db.is_support_session_active(user_id):
        await message.answer("❌ Пользователь закрыл диалог.")
        return
    try:
        await bot.send_message(
            user_id,
            f"💬 Администратор:\n\n{args[2]}",
            reply_markup=support_chat_keyboard(),
        )
        await message.answer("✅ Ответ отправлен.")
    except Exception:
        logger.exception("Operation failed")
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(Command("supportclose"), F.chat.type == "private")
async def cmd_supportclose(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    user_id = await parse_support_user_id(message, "Использование: supportclose ID")
    if user_id is None:
        return
    await db.set_support_session(user_id, False)
    await message.answer(f"🔒 Диалог с {user_id} закрыт.")
    try:
        await bot.send_message(user_id, "🔒 Администратор закрыл диалог.")
    except Exception as error:
        logger.info("Could not notify closed support user %s: %s", user_id, error)


async def send_help(message: Message) -> None:
    await message.answer(
        "📖 Как играть\n\n1️⃣ Подпишись на канал и нажми «Проверить подписку».\n2️⃣ Пиши сообщения в основной группе — за них начисляются DC.\n3️⃣ Забирай ежедневный бонус в личке или основной группе: бонус.\n\n🎰 Игры — только в личке с ботом\nНажми «🎮 Игры» в главном меню и выбери игру и ставку кнопками.\n• слоты 50\n• рулетка красное 50\n• кубик 3 50\n• монетка орёл 50\n• лотерея 1000\n• мины 2500\n• скретч 1000\nВ минах открывай клетки и забирай выигрыш до того, как попадёшь на бомбу.\n\n⚔️ Дуэли — в основном чате и привязанном чате канала\n• дуэль 1000 — создать вызов на 1 000 DC\n• дуэль 1000 @username — вызвать конкретного игрока\nТакже можно ответить «дуэль 1000» на сообщение соперника.\nСоперник принимает дуэль кнопкой, победитель получает весь банк.\n\n💱 Полезное\n• баланс — твои DC\n• обмен — обмен DC на шанс, подарки или Premium\n• промо КОД — активировать промокод\n• перевод @username сумма — отправить DC игроку\n• стата — статистика в основном чате\n\nКоманды пишутся без /"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type != "private" and message.chat.id != MAIN_CHAT_ID:
        return
    if message.from_user and await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    await send_help(message)


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    await send_help(callback.message)
    await callback.answer()


@router.message(Command("say"), F.chat.type == "private")
async def cmd_say(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /say текст сообщения")
        return
    try:
        await bot.send_message(MAIN_CHAT_ID, args[1].strip())
        await message.answer("✅ Сообщение отправлено в чат.")
    except Exception:
        logger.exception("Operation failed")
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(Command("channel"), F.chat.type == "private")
async def cmd_channel(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: канал ТЕКСТ\nПример: канал Сегодня будет мини-ивент!")
        return
    try:
        await bot.send_message(REQUIRED_CHANNEL, parts[1].strip())
        await message.answer("✅ Сообщение опубликовано в новостном канале.")
    except Exception:
        logger.exception("Operation failed")
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(Command("mini"), F.chat.type == "private")
async def cmd_mini(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    usage = "Мини-ивенты публикуются в d_coins_channel:\n\nмини первый СУММА\nмини первые КОЛИЧЕСТВО СУММА\nмини билет СЕКУНДЫ СУММА\nмини отмена\n\nПримеры:\nмини первый 5000\nмини первые 10 1000\nмини билет 60 15000"
    if len(parts) < 2:
        await message.answer(usage)
        return
    action = parts[1].lower()
    if action in {"отмена", "cancel"}:
        event = await db.cancel_mini_event(REQUIRED_CHANNEL)
        if not event:
            await message.answer("ℹ️ Активных мини-ивентов в канале нет.")
            return
        try:
            if event.get("message_id"):
                await bot.edit_message_text(
                    chat_id=REQUIRED_CHANNEL,
                    message_id=event["message_id"],
                    text="❌ Мини-ивент отменён администратором.",
                )
            else:
                await bot.send_message(REQUIRED_CHANNEL, "❌ Мини-ивент отменён администратором.")
        except Exception:
            pass
        await message.answer("✅ Мини-ивент отменён.")
        return
    try:
        if action in {"первый", "first"} and len(parts) == 3:
            kind, max_players, reward, duration = ("first", 1, int(parts[2]), 300)
        elif action in {"первые", "first_n"} and len(parts) == 4:
            kind, max_players, reward, duration = (
                "first_n",
                int(parts[2]),
                int(parts[3]),
                300,
            )
        elif action in {"билет", "ticket"} and len(parts) == 4:
            kind, duration, reward = ("ticket", int(parts[2]), int(parts[3]))
            max_players = 0
        else:
            await message.answer(usage)
            return
    except ValueError:
        await message.answer("❌ Количество, время и награда должны быть целыми числами.\n\n" + usage)
        return
    if reward <= 0 or reward > 1000000000:
        await message.answer("❌ Награда должна быть от 1 до 1 000 000 000 DC.")
        return
    if kind == "first_n" and (not 1 <= max_players <= 100):
        await message.answer("❌ Можно указать от 1 до 100 участников.")
        return
    if not 10 <= duration <= 3600:
        await message.answer("❌ Время события должно быть от 10 до 3 600 секунд.")
        return
    status, event = await db.create_mini_event(kind, REQUIRED_CHANNEL, reward, max_players, duration)
    if status == "active_exists":
        await message.answer("❌ В канале уже идёт мини-ивент. Заверши его или напиши: мини отмена")
        return
    try:
        post = await bot.send_message(
            REQUIRED_CHANNEL,
            mini_event_text(event) + f"\n\n⏳ Время: {duration // 60} мин. {duration % 60} сек.",
            reply_markup=mini_event_keyboard(event["id"]),
        )
        await db.set_mini_event_message(event["id"], post.message_id)
        await message.answer(f"✅ Мини-ивент #{event['id']} опубликован в канале.")
    except Exception:
        logger.exception("Operation failed")
        await db.cancel_mini_event(REQUIRED_CHANNEL)
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(Command("vip"), F.chat.type == "private")
async def cmd_vip(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /vip user_id")
        return
    try:
        user_id = int(args[1].lstrip("@"))
    except ValueError:
        await message.answer("❌ Укажи числовой ID.")
        return
    await db.set_vip(user_id)
    await message.answer(f"✅ Пользователь {user_id} назначен VIP.")


@router.message(Command("unvip"), F.chat.type == "private")
async def cmd_unvip(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /unvip user_id")
        return
    try:
        user_id = int(args[1].lstrip("@"))
    except ValueError:
        await message.answer("❌ Укажи числовой ID.")
        return
    await db.remove_vip(user_id)
    await message.answer(f"✅ VIP статус снят с {user_id}.")


@router.message(Command("viplist"), F.chat.type == "private")
async def cmd_viplist(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    async with connect(db.path) as conn:
        async with conn.execute(
            "SELECT v.user_id, COALESCE(u.user_name, CAST(v.user_id AS TEXT)), COALESCE(u.msg_count, 0) FROM vip_users v LEFT JOIN user_stats u ON u.user_id = v.user_id AND u.chat_id = ?",
            (MAIN_CHAT_ID,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("👑 VIP пользователей нет.")
        return
    text = "👑 Список VIP:\n\n"
    for uid, name, msg_count in rows:
        text += f"• {name} ({uid}) — {msg_count} сообщ.\n"
    await message.answer(text)


@router.message(Command("ban"), F.chat.type == "private")
async def cmd_ban(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer("Использование: /ban user_id [причина]")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("❌ Укажи числовой ID.")
        return
    reason = args[2] if len(args) > 2 else ""
    await db.ban_user(user_id, reason)
    await message.answer(f"✅ Пользователь {user_id} заблокирован.")


@router.message(Command("unban"), F.chat.type == "private")
async def cmd_unban(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /unban user_id")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("❌ Укажи числовой ID.")
        return
    await db.unban_user(user_id)
    await message.answer(f"✅ Пользователь {user_id} разблокирован.")


@router.message(Command("banlist"), F.chat.type == "private")
async def cmd_banlist(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    ban_list = await db.get_ban_list()
    if not ban_list:
        await message.answer("✅ Список заблокированных пуст.")
        return
    text = "🚫 Заблокированные:\n\n"
    for uid, name, reason in ban_list:
        text += f"• {name} ({uid})"
        if reason:
            text += f" — {reason}"
        text += "\n"
    await message.answer(text)


@router.message(Command("addmsgs"), F.chat.type == "private")
async def cmd_addmsgs(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /addmsgs user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    inv_name = await db.get_user_name(user_id)
    new_count = msg_count + amount
    new_chance = min(round(chance + STEP * amount, 3), MAX_CHANCE)
    await db.update_user(user_id, MAIN_CHAT_ID, inv_name, new_chance, new_count, last_bonus)
    await message.answer(
        f"✅ Добавлено {amount} сообщений\n👤 {inv_name} ({user_id})\n💬 Сообщений: {new_count}\n📈 Шанс: {new_chance:.3f} балл."
    )


@router.message(Command("removemsgs"), F.chat.type == "private")
async def cmd_removemsgs(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /removemsgs user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    inv_name = await db.get_user_name(user_id)
    new_count = max(0, msg_count - amount)
    new_chance = max(round(chance - STEP * amount, 3), START_CHANCE)
    await db.update_user(user_id, MAIN_CHAT_ID, inv_name, new_chance, new_count, last_bonus)
    await message.answer(
        f"✅ Убрано {amount} сообщений\n👤 {inv_name} ({user_id})\n💬 Сообщений: {new_count}\n📈 Шанс: {new_chance:.3f} балл."
    )


@router.message(Command("addday"), F.chat.type == "private")
async def cmd_addday(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /addday user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи ID и количество.")
        return
    name = await db.get_user_name(user_id)
    await db.add_day_messages(user_id, MAIN_CHAT_ID, name, amount)
    await message.answer(f"✅ Добавлено {amount} сообщений в daytop\n👤 {name} ({user_id})")


@router.message(Command("removeday"), F.chat.type == "private")
async def cmd_removeday(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /removeday user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи ID и количество.")
        return
    await db.remove_day_messages(user_id, MAIN_CHAT_ID, amount)
    await message.answer(f"✅ Убрано {amount} сообщений из daytop\n👤 {user_id}")


@router.message(Command("addcoins"), F.chat.type == "private")
async def cmd_addcoins(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /addcoins user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    if amount <= 0:
        await message.answer("❌ Количество должно быть положительным.")
        return
    await db.add_coins(user_id, amount)
    shown_balance = await db.get_display_balance(user_id)
    name = await db.get_user_name(user_id)
    await message.answer(
        f"✅ Добавлено {amount} DC\n👤 {name} ({user_id})\n🪙 Баланс: {shown_balance} D-COINS"
    )


@router.message(Command("removecoins"), F.chat.type == "private")
async def cmd_removecoins(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /removecoins user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    if amount <= 0:
        await message.answer("❌ Количество должно быть положительным.")
        return
    ok = await db.remove_coins(user_id, amount)
    name = await db.get_user_name(user_id)
    balance, _ = await db.get_coins(user_id)
    if ok:
        shown_balance = await db.get_display_balance(user_id)
        await message.answer(
            f"✅ Убрано {amount} DC\n👤 {name} ({user_id})\n🪙 Баланс: {shown_balance} D-COINS"
        )
    else:
        await message.answer(f"❌ Недостаточно монет\n👤 {name} ({user_id})\n🪙 Баланс: {balance} D-COINS")


async def change_visual_command(message: Message, remove: bool = False) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    command_name = "removevisual" if remove else "addvisual"
    if len(args) != 3:
        await message.answer(f"Использование: {command_name} user_id количество")
        return
    try:
        user_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ ID и количество должны быть целыми числами.")
        return
    if amount <= 0:
        await message.answer("❌ Количество должно быть положительным.")
        return
    ok, displayed, real, visual = await db.change_visual_balance(user_id, -amount if remove else amount)
    name = await db.get_user_name(user_id)
    if not ok:
        await message.answer(
            f"❌ Недостаточно визуальных DC.\n👤 {name} ({user_id})\n👁 Визуальных: {visual:,} DC".replace(
                ",", " "
            )
        )
        return
    action = "Убрано" if remove else "Добавлено"
    await message.answer(
        f"✅ {action} {amount:,} визуальных DC\n👤 {name} ({user_id})\n🪙 Отображается: {displayed:,} DC\n💰 Реальных: {real:,} DC\n👁 Визуальных: {visual:,} DC".replace(
            ",", " "
        )
    )


@router.message(Command("addvisual"), F.chat.type == "private")
async def cmd_addvisual(message: Message) -> None:
    await change_visual_command(message)


@router.message(Command("removevisual"), F.chat.type == "private")
async def cmd_removevisual(message: Message) -> None:
    await change_visual_command(message, remove=True)


@router.message(Command("createpromo"), F.chat.type == "private")
async def cmd_createpromo(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args, hidden = parse_hidden_promo_args(message.text)
    if len(args) not in (3, 4):
        await message.answer(
            "Использование: /createpromo КОД награда_DC [лимит] [скрытый]\nЛимит не указывай для безлимитного промокода.\nФлаг «скрытый» отключает публикацию в канале."
        )
        return
    code = args[1].upper()
    if not 3 <= len(code) <= 32 or not all(
        (char.isascii() and char.isalnum() or char in "_-" for char in code)
    ):
        await message.answer("❌ Код: от 3 до 32 символов; разрешены латинские буквы, цифры, _ и -.")
        return
    try:
        reward = int(args[2])
        max_uses = int(args[3]) if len(args) == 4 else None
    except ValueError:
        await message.answer("❌ Награда и лимит должны быть целыми числами.")
        return
    if not await db.create_promo(code, reward, max_uses):
        await message.answer("❌ Не удалось создать промокод: проверь значения или выбери другой код.")
        return
    limit_text = str(max_uses) if max_uses is not None else "без лимита"
    visibility_text = "\n🔒 Скрытый: в канал не опубликован" if hidden else ""
    await message.answer(
        f"✅ Промокод {code} создан.\n🎁 Награда: {reward} DC\n👥 Активаций: {limit_text}{visibility_text}"
    )
    if not hidden and (
        not await publish_promo(
            bot,
            f"🎁 Новый промокод!\n\n🔑 Код: <code>{code}</code>\n🪙 Награда: {reward:,} DC\n👥 Активаций: {limit_text}\n\nАктивировать в боте: промо {code}".replace(
                ",", " "
            ),
        )
    ):
        await message.answer(
            "⚠️ Промокод создан, но не отправлен в канал. Проверь, что бот — администратор канала."
        )


@router.message(Command("deletepromo"), F.chat.type == "private")
async def cmd_deletepromo(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /deletepromo КОД")
        return
    if await db.delete_promo(args[1].upper()):
        await message.answer(f"✅ Промокод {args[1].upper()} удалён.")
    else:
        await message.answer("❌ Промокод не найден.")


@router.message(Command("createcasepromo"), F.chat.type == "private")
async def cmd_createcasepromo(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args, hidden = parse_hidden_promo_args(message.text)
    if len(args) not in (3, 4, 5):
        await message.answer(
            "Использование: /createcasepromo КОД КЕЙС [ключей] [лимит] [скрытый]\nПример: /createcasepromo BLOODFREE blood 1 100 скрытый"
        )
        return
    code = args[1].upper()
    case_id = args[2].lower()
    if not 3 <= len(code) <= 32 or not all(
        (char.isascii() and char.isalnum() or char in "_-" for char in code)
    ):
        await message.answer("❌ Код: от 3 до 32 символов; разрешены латинские буквы, цифры, _ и -.")
        return
    try:
        case_count = int(args[3]) if len(args) >= 4 else 1
        max_uses = int(args[4]) if len(args) == 5 else None
    except ValueError:
        await message.answer("❌ Количество ключей и лимит должны быть целыми числами.")
        return
    if not await db.create_case_promo(code, case_id, case_count, max_uses):
        await message.answer("❌ Не удалось создать промокод: проверь кейс, значения или код.")
        return
    limit_text = str(max_uses) if max_uses is not None else "без лимита"
    visibility_text = "\n🔒 Скрытый: в канал не опубликован" if hidden else ""
    await message.answer(
        f"✅ Промокод {code} создан.\n🎟 Кейс: {CASES[case_id]['title']} × {case_count}\n👥 Активаций: {limit_text}{visibility_text}"
    )
    if not hidden and (
        not await publish_promo(
            bot,
            f"🎁 Новый промокод!\n\n🔑 Код: <code>{code}</code>\n🎟 Награда: {CASES[case_id]['title']} × {case_count}\n👥 Активаций: {limit_text}\n\nАктивировать в боте: промо {code}",
        )
    ):
        await message.answer(
            "⚠️ Промокод создан, но не отправлен в канал. Проверь, что бот — администратор канала."
        )


@router.message(Command("promos"), F.chat.type == "private")
async def cmd_promos(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    promos = await db.get_promos()
    if not promos:
        await message.answer("📭 Активных промокодов нет.")
        return
    text = "🎟 Активные промокоды:\n\n"
    for code, reward, reward_type, case_id, case_count, max_uses, uses in promos:
        limit_text = f"{uses}/{max_uses}" if max_uses is not None else f"{uses}/∞"
        reward_text = f"{CASES[case_id]['title']} × {case_count}" if reward_type == "case" else f"{reward} DC"
        text += f"{code} — {reward_text} ({limit_text})\n"
    await message.answer(text)


@router.message(Command("balance"), F.chat.type == "private")
async def cmd_balance(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        star_balance = await bot.get_my_star_balance()
        await message.answer(f"💫 Баланс бота: {star_balance.amount} звёзд")
    except Exception:
        await message.answer("❌ Не удалось получить баланс.")


@router.message(Command("popolnit"), F.chat.type == "private")
async def cmd_popolnit(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    payload = await create_invoice_order(db.path, message.from_user.id, 0, POPOLNIT_AMOUNT)
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Пополнение бота",
        description=f"Пополнение на {POPOLNIT_AMOUNT} Stars",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Stars", amount=POPOLNIT_AMOUNT)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await validate_checkout(query, db.path)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, bot: Bot) -> None:
    result = await accept_payment(db, message)
    await message.answer(result)


@router.message(Command("sendgift"), F.chat.type == "private")
async def cmd_sendgift(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /sendgift user_id gift_id")
        return
    try:
        user_id = int(args[1])
        gift_id = args[2]
    except ValueError:
        await message.answer("❌ Укажи числовой ID и gift_id.")
        return
    try:
        sender = await send_bot_gift(bot, user_id, gift_id)
        await send_log(
            bot,
            f"🎁 Ручная выдача\n\n👤 {user_id}\n📦 {gift_id}\nОтправитель: {sender}",
        )
        await message.answer(f"✅ Подарок поставлен в очередь!\n👤 {user_id}\n📦 {gift_id}")
    except Exception:
        logger.exception("Operation failed")
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(Command("pending"), F.chat.type == "private")
async def cmd_pending(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    gifts = await db.get_pending_gifts()
    if not gifts:
        await message.answer("✅ Список невыданных подарков пуст!")
        return
    import datetime as dt

    star_balance = await bot.get_my_star_balance()
    balance_text = f"{star_balance.amount}⭐"
    text = f"📋 Невыданные подарки ({len(gifts)}):\n💫 Отправитель/баланс: {balance_text}\n\n"
    for g_id, uid, user_name, gift_id, reason, created_at in gifts:
        date = dt.datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
        text += f"#{g_id} | {user_name} ({uid})\n📦 {gift_id}\n📝 {reason} | {date}\n👉 deliver {g_id} — выдать\n🗑 deletepending {g_id} — удалить\n\n"
    await message.answer(text)


@router.message(Command("deliver"), F.chat.type == "private")
async def cmd_deliver(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /deliver id")
        return
    try:
        gift_db_id = int(args[1])
    except ValueError:
        await message.answer("❌ Укажи числовой ID из /pending")
        return
    gifts = await db.get_pending_gifts()
    gift = next((g for g in gifts if g[0] == gift_db_id), None)
    if not gift:
        await message.answer("❌ Подарок не найден.")
        return
    _, user_id, user_name, gift_id, reason, _ = gift
    try:
        sender = await send_bot_gift(bot, user_id, gift_id)
        await db.remove_pending_gift(gift_db_id)
        await send_log(
            bot,
            f"🎁 Отложенный подарок поставлен в очередь\n\n{user_name} ({user_id})\nОтправитель: {sender}",
        )
        await message.answer(
            f"✅ Подарок поставлен в очередь!\n👤 {user_name} ({user_id})\nОтправитель: {sender}"
        )
    except Exception:
        logger.exception("Operation failed")
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(Command("deletepending"), F.chat.type == "private")
async def cmd_deletepending(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: deletepending ID")
        return
    gift_db_id = int(parts[1])
    gift = next((item for item in await db.get_pending_gifts() if item[0] == gift_db_id), None)
    if not gift:
        await message.answer("❌ Заявка не найдена.")
        return
    _, user_id, user_name, gift_id, _, _ = gift
    await db.remove_pending_gift(gift_db_id)
    await message.answer(f"🗑 Заявка #{gift_db_id} удалена: {user_name} ({user_id}), подарок {gift_id}.")


@router.message(Command("premiumorders"), F.chat.type == "private")
async def cmd_premiumorders(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    orders = await db.get_premium_orders()
    if not orders:
        await message.answer("✅ Заявок на Premium нет.")
        return
    import datetime as dt

    text = f"💎 Заявки Premium на месяц ({len(orders)}):\n\n"
    for order_id, user_id, user_name, cost, created_at in orders:
        date = dt.datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
        text += f"#{order_id} | {user_name} ({user_id})\n🪙 {cost:,} DC | {date}\nПосле выдачи: premiumdone {order_id}\nВозврат: premiumrefund {order_id}\n\n".replace(
            ",", " "
        )
    await message.answer(text)


@router.message(Command("premiumdone"), F.chat.type == "private")
async def cmd_premiumdone(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: premiumdone ID")
        return
    order_id = int(parts[1])
    order = next((item for item in await db.get_premium_orders() if item[0] == order_id), None)
    if not order:
        await message.answer("❌ Заявка не найдена.")
        return
    _, user_id, user_name, _, _ = order
    await db.remove_premium_order(order_id)
    await message.answer(f"✅ Premium отмечен как выданный: {user_name} ({user_id}).")
    try:
        await bot.send_message(user_id, "💎 Premium на месяц выдан. Спасибо за обмен!")
    except Exception as e:
        logger.warning("Could not notify Premium recipient: %s", e)


@router.message(Command("premiumrefund"), F.chat.type == "private")
async def cmd_premiumrefund(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: premiumrefund ID")
        return
    order_id = int(parts[1])
    order = next((item for item in await db.get_premium_orders() if item[0] == order_id), None)
    if not order:
        await message.answer("❌ Заявка не найдена.")
        return
    _, user_id, user_name, cost, _ = order
    await db.add_coins(user_id, cost)
    shown_balance = await db.get_display_balance(user_id)
    await db.remove_premium_order(order_id)
    await message.answer(f"↩️ Возвращено {cost:,} DC игроку {user_name} ({user_id}).".replace(",", " "))
    try:
        await bot.send_message(
            user_id,
            f"↩️ Premium пока недоступен — тебе вернули {cost:,} DC.\n🪙 Баланс: {shown_balance:,} DC".replace(
                ",", " "
            ),
        )
    except Exception as e:
        logger.warning("Could not notify Premium refund recipient: %s", e)


@router.message(Command("broadcast"), F.chat.type == "private")
async def cmd_broadcast(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) >= 3 and parts[1].lower() in {"кейс", "case"}:
        aliases = {
            "школьный": "school",
            "school": "school",
            "студенческий": "student",
            "student": "student",
            "отличника": "excellent",
            "excellent": "excellent",
            "blood": "blood",
            "блад": "blood",
            "pantera": "pantera",
            "пантера": "pantera",
            "spider": "spider_man",
            "spider_man": "spider_man",
            "паук": "spider_man",
        }
        case_id = aliases.get(parts[2].lower())
        try:
            amount = int(parts[3]) if len(parts) >= 4 else 1
        except ValueError:
            await message.answer("❌ Количество ключей должно быть целым числом.")
            return
        if case_id not in CASES:
            await message.answer(
                "❌ Кейс не найден. Доступны: school, student, excellent, blood, pantera, spider_man."
            )
            return
        if amount <= 0 or amount > 100:
            await message.answer("❌ Можно раздать от 1 до 100 ключей каждому.")
            return
        broadcast_id, recipients = await db.create_case_broadcast(case_id, amount)
        await message.answer(
            f"✅ Раздача #{broadcast_id} создана.\n🔑 По {amount} ключей кейса {CASES[case_id]['title']} начислено: {recipients} игрокам.\n📨 Уведомления отправляются автоматически."
        )
        return
    if len(parts) != 2:
        await message.answer(
            "Раздача DC: раздать СУММА\nРаздача кейсов: раздать кейс НАЗВАНИЕ [КОЛИЧЕСТВО]\n\nПримеры:\nраздать 10000\nраздать кейс blood 1"
        )
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.answer("❌ Сумма должна быть целым числом.")
        return
    if amount <= 0 or amount > 1000000000:
        await message.answer("❌ Сумма должна быть от 1 до 1 000 000 000 DC.")
        return
    broadcast_id, recipients = await db.create_bonus_broadcast(amount)
    await message.answer(
        f"✅ Раздача #{broadcast_id} создана.\n🪙 По {amount:,} DC начислено: {recipients} игрокам.\n📨 Уведомления отправляются автоматически.".replace(
            ",", " "
        )
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏫 Ивент и выдача призов", callback_data="sca:page:0")],
            [
                InlineKeyboardButton(text="📋 Pending", callback_data="admin_pending"),
                InlineKeyboardButton(text="💎 Premium", callback_data="admin_premium"),
            ],
            [
                InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin_promos"),
                InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"),
            ],
            [InlineKeyboardButton(text="💰 Экономика", callback_data="admin_economy")],
            [InlineKeyboardButton(text="🛠 Команды", callback_data="admin_commands")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_panel")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")]]
    )


async def admin_dashboard_text(bot: Bot) -> str:
    pending_count = len(await db.get_pending_gifts())
    premium_count = len(await db.get_premium_orders())
    promo_count = len(await db.get_promos())
    ban_count = len(await db.get_ban_list())
    try:
        star_balance = (await bot.get_my_star_balance()).amount
        stars_text = f"{star_balance}⭐"
    except Exception:
        stars_text = "недоступен"
    updated_at = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%H:%M:%S МСК")
    return f"🛠 Админ-панель\n\n📋 Подарков в pending: {pending_count}\n💎 Заявок Premium: {premium_count}\n🎟 Активных промокодов: {promo_count}\n🚫 Заблокировано: {ban_count}\n⭐ Баланс бота: {stars_text}\n🔄 Обновлено: {updated_at}"


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(await admin_dashboard_text(bot), reply_markup=admin_panel_keyboard())


@router.message(Command("eventdays"), F.chat.type == "private")
async def cmd_eventdays(message: Message) -> None:
    if message.chat.type != "private" or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    try:
        days = int(parts[1]) if len(parts) == 2 else 0
    except ValueError:
        days = 0
    if days == 0 or abs(days) > 365:
        await message.answer(
            "Использование: дниивента +3 — продлить на 3 дня\nдниивента -2 — сократить на 2 дня\nУкажи целое число от -365 до 365, кроме нуля."
        )
        return
    status, new_end = await school_event.change_event_days(
        days, message.from_user.id, f"eventdays:{message.chat.id}:{message.message_id}"
    )
    if status != "updated":
        notices = {
            "missing": "❌ Ивент ещё не создан.",
            "stopped": "❌ Ивент остановлен вручную. Изменение срока его не запускает.",
            "duplicate": "ℹ️ Эта команда уже применена.",
        }
        await message.answer(notices[status])
        return
    deadline = datetime.fromtimestamp(new_end, pytz.timezone("Asia/Tashkent")).strftime("%d.%m.%Y %H:%M")
    remaining = new_end - time.time()
    if remaining > 0:
        minutes = int(remaining // 60)
        duration = f"⏳ Осталось: {minutes // 1440} д. {minutes % 1440 // 60} ч. {minutes % 60} мин."
    else:
        duration = "⌛ Новый срок уже наступил — ивент завершён."
    await message.answer(
        f"✅ Срок ивента изменён на {days:+d} дн.\n📅 Завершение: {deadline} (Ташкент)\n{duration}"
    )


@router.message(Command("goldenhour"), F.chat.type == "private")
async def cmd_goldenhour(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    action = parts[1].lower() if len(parts) > 1 else ""
    usage = "Золотой час:\nзолотойчас старт\nзолотойчас статус\nзолотойчас стоп"
    if action in {"старт", "start"}:
        status, hour = await school_event.start_golden_hour()
        if status == "no_event":
            await message.answer("❌ Нельзя запустить Золотой час: активного ивента нет.")
            return
        if status == "active":
            remaining = max(0, int(hour["ends"] - time.time()))
            await message.answer(f"⚡ Золотой час уже идёт. Осталось {remaining // 60} мин.")
            return
        text = (
            "⚡ ЗОЛОТОЙ ЧАС НАЧАЛСЯ!\n\n"
            "⏳ Длительность: 60 минут\n"
            "⚔️ Проигранные ставки наносят x2 урон боссу\n"
            "📚 Награды за квесты дают x2 очков знаний\n\n"
            "Лимиты:\n"
            f"• до {GOLDEN_HOUR_TOTAL_BONUS_DAMAGE_LIMIT:,} общего бонусного урона по боссу\n"
            f"• до {GOLDEN_HOUR_KNOWLEDGE_LIMIT} дополнительных 📖\n"
            f"• до {GOLDEN_HOUR_GAMES_LIMIT} игр в рейтинге\n\n"
            "После завершения все лимиты сбросятся."
        ).replace(",", " ")
        try:
            await bot.send_message(REQUIRED_CHANNEL, text)
        except Exception:
            logger.exception("Could not publish Golden Hour start")
            await message.answer("❌ Не удалось опубликовать Золотой час в канале.")
            return
        await message.answer("✅ Золотой час запущен на 60 минут.")
        return
    if action in {"статус", "status"}:
        hour = await school_event.golden_hour_status()
        if not hour:
            await message.answer("ℹ️ Сейчас Золотой час не активен.")
            return
        remaining = max(0, int(hour["ends"] - time.time()))
        await message.answer(
            f"⚡ Золотой час активен.\n⏳ Осталось: {remaining // 60} мин. {remaining % 60} сек.\n"
            f"⚔️ Бонусный урон: {hour['bonus_damage']:,} / {GOLDEN_HOUR_TOTAL_BONUS_DAMAGE_LIMIT:,} DC\n"
            f"📚 Лимит знаний: {GOLDEN_HOUR_KNOWLEDGE_LIMIT} 📖".replace(",", " ")
        )
        return
    if action in {"стоп", "stop"}:
        if not await school_event.stop_golden_hour():
            await message.answer("ℹ️ Активного Золотого часа нет.")
            return
        await golden_hour_task(bot)
        await message.answer("✅ Золотой час завершён. Итоги опубликованы в канале.")
        return
    await message.answer(usage)


@router.message(Command("bossdamage"), F.chat.type == "private")
async def cmd_bossdamage(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: уронбоссу СУММА\nПример: уронбоссу 100000")
        return
    try:
        amount = int(parts[1].replace("_", ""))
    except ValueError:
        await message.answer("❌ Урон должен быть целым числом.")
        return
    if amount <= 0 or amount > 1000000000:
        await message.answer("❌ Урон должен быть от 1 до 1 000 000 000.")
        return
    async with school_event.transaction() as connection:
        before = await school_event.current(connection)
    if not before or before["hp"] <= 0:
        await message.answer("❌ Сейчас нет живого активного босса.")
        return
    stage_before = int(before["boss_stage"] or 1)
    hp_before = int(before["hp"])
    damage, _ = await school_event.record(
        message.from_user.id,
        "Администратор",
        f"admin-damage:{message.chat.id}:{message.message_id}",
        loss=amount,
        manual=True,
    )
    async with school_event.transaction() as connection:
        after = await school_event.current(connection, active=False)
    if not damage or not after:
        await message.answer("ℹ️ Урон не применён: команда уже использована или ивент завершён.")
        return
    boss_name = SCHOOL_BOSSES.get(stage_before, SCHOOL_BOSSES[2])["name"]
    stage_after = int(after["boss_stage"] or 1)
    if stage_after != stage_before:
        next_boss = SCHOOL_BOSSES.get(stage_after, SCHOOL_BOSSES[2])["name"]
        status = f"🏆 {boss_name} побеждён!\n➡️ Новый босс: {next_boss}\n❤️ HP: {after['hp']:,}"
    elif after["hp"] <= 0:
        status = f"🏆 {boss_name} побеждён!"
    else:
        status = f"❤️ Осталось: {after['hp']:,} / {after['max_hp']:,} HP"
    ignored = max(0, amount - hp_before)
    ignored_text = f"\nℹ️ Лишний урон не учитывался: {ignored:,}." if ignored else ""
    await message.answer(
        f"✅ Боссу вручную нанесено {damage:,} урона.\n{status}{ignored_text}".replace(",", " ")
    )


@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(await admin_dashboard_text(bot), reply_markup=admin_panel_keyboard())
    await callback.answer()


async def render_admin_pending(callback: CallbackQuery, bot: Bot, notice: str = "") -> None:
    gifts = await db.get_pending_gifts()
    text = (notice + "\n\n" if notice else "") + f"📋 Pending-подарки: {len(gifts)}\n"
    buttons = []
    if not gifts:
        text += "\nОчередь пуста."
    else:
        text += "\nПоказаны первые 10 заявок:\n\n"
        for gift_id, user_id, user_name, telegram_gift_id, reason, _ in gifts[:10]:
            text += f"#{gift_id} — {user_name} ({user_id})\n🎁 {telegram_gift_id}\n📝 {reason}\n\n"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Выдать #{gift_id}",
                        callback_data=f"admin_deliver_{gift_id}",
                    ),
                    InlineKeyboardButton(
                        text=f"🗑 Удалить #{gift_id}",
                        callback_data=f"admin_dropgift_{gift_id}",
                    ),
                ]
            )
    buttons.append([InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")])
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "admin_pending")
async def admin_pending_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await render_admin_pending(callback, bot)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_deliver_"))
async def admin_deliver_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        gift_db_id = int(callback.data.removeprefix("admin_deliver_"))
    except (AttributeError, ValueError):
        await callback.answer("❌ Неверный ID", show_alert=True)
        return
    gift = next((item for item in await db.get_pending_gifts() if item[0] == gift_db_id), None)
    if not gift:
        await render_admin_pending(callback, bot, "ℹ️ Заявка уже обработана.")
        await callback.answer()
        return
    _, user_id, user_name, telegram_gift_id, _, _ = gift
    try:
        await send_bot_gift(bot, user_id, telegram_gift_id)
        await db.remove_pending_gift(gift_db_id)
        await send_log(
            bot,
            f"🎁 Подарок #{gift_db_id} поставлен в очередь через админ-панель\n{user_name} ({user_id})",
        )
        await render_admin_pending(callback, bot, f"✅ Подарок #{gift_db_id} поставлен в очередь.")
        await callback.answer("✅ Поставлен в очередь")
    except Exception:
        logger.exception("Operation failed")
        await callback.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.", show_alert=True)


@router.callback_query(F.data.startswith("admin_dropgift_"))
async def admin_dropgift_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        gift_db_id = int(callback.data.removeprefix("admin_dropgift_"))
    except (AttributeError, ValueError):
        await callback.answer("❌ Неверный ID", show_alert=True)
        return
    gift = next((item for item in await db.get_pending_gifts() if item[0] == gift_db_id), None)
    if gift:
        await db.remove_pending_gift(gift_db_id)
        notice = f"🗑 Заявка #{gift_db_id} удалена."
    else:
        notice = "ℹ️ Заявка уже обработана."
    await render_admin_pending(callback, bot, notice)
    await callback.answer()


async def render_admin_premium(callback: CallbackQuery, notice: str = "") -> None:
    orders = await db.get_premium_orders()
    text = (notice + "\n\n" if notice else "") + f"💎 Заявки Premium: {len(orders)}\n"
    buttons = []
    if not orders:
        text += "\nОчередь пуста."
    else:
        text += "\nПоказаны первые 10 заявок:\n\n"
        for order_id, user_id, user_name, cost, _ in orders[:10]:
            text += f"#{order_id} — {user_name} ({user_id})\n🪙 {cost:,} DC\n\n".replace(",", " ")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Выдан #{order_id}",
                        callback_data=f"admin_premiumdone_{order_id}",
                    ),
                    InlineKeyboardButton(
                        text=f"↩️ Возврат #{order_id}",
                        callback_data=f"admin_premiumrefund_{order_id}",
                    ),
                ]
            )
    buttons.append([InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")])
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "admin_premium")
async def admin_premium_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await render_admin_premium(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_premiumdone_"))
async def admin_premiumdone_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    raw_id = callback.data.removeprefix("admin_premiumdone_")
    if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 15:
        await callback.answer("Некорректный ID заявки.", show_alert=True)
        return
    order_id = int(raw_id)
    order = next((item for item in await db.get_premium_orders() if item[0] == order_id), None)
    if not order:
        await render_admin_premium(callback, "ℹ️ Заявка уже обработана.")
        await callback.answer()
        return
    _, user_id, user_name, _, _ = order
    await db.remove_premium_order(order_id)
    try:
        await bot.send_message(user_id, "💎 Premium на месяц выдан. Спасибо за обмен!")
    except Exception as e:
        logger.warning("Could not notify Premium recipient: %s", e)
    await render_admin_premium(callback, f"✅ Premium #{order_id} отмечен как выданный: {user_name}.")
    await callback.answer("✅ Готово")


@router.callback_query(F.data.startswith("admin_premiumrefund_"))
async def admin_premiumrefund_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    raw_id = callback.data.removeprefix("admin_premiumrefund_")
    if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 15:
        await callback.answer("Некорректный ID заявки.", show_alert=True)
        return
    order_id = int(raw_id)
    order = next((item for item in await db.get_premium_orders() if item[0] == order_id), None)
    if not order:
        await render_admin_premium(callback, "ℹ️ Заявка уже обработана.")
        await callback.answer()
        return
    _, user_id, user_name, cost, _ = order
    await db.add_coins(user_id, cost)
    shown_balance = await db.get_display_balance(user_id)
    await db.remove_premium_order(order_id)
    try:
        await bot.send_message(
            user_id,
            f"↩️ Тебе вернули {cost:,} DC. Баланс: {shown_balance:,} DC".replace(",", " "),
        )
    except Exception as e:
        logger.warning("Could not notify Premium refund recipient: %s", e)
    await render_admin_premium(callback, f"↩️ {cost:,} DC возвращено игроку {user_name}.".replace(",", " "))
    await callback.answer("↩️ Возвращено")


@router.callback_query(F.data == "admin_promos")
async def admin_promos_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    promos = await db.get_promos()
    text = f"🎟 Активные промокоды: {len(promos)}\n\n"
    buttons = []
    if promos:
        for code, reward, reward_type, case_id, case_count, max_uses, uses in promos[:30]:
            limit = f"{uses}/{max_uses}" if max_uses is not None else f"{uses}/∞"
            prize = (
                f"{CASES[case_id]['title']} × {case_count}"
                if reward_type == "case"
                else f"{reward:,} DC".replace(",", " ")
            )
            text += f"• {code} — {prize} ({limit})\n"
            callback_data = f"admin_promodel:{code}"
            confirm_data = f"admin_promoconfirm:{code}"
            if len(confirm_data.encode("utf-8")) <= 64:
                buttons.append([InlineKeyboardButton(text=f"🗑 {code}", callback_data=callback_data)])
    else:
        text += "Промокодов нет."
    text += (
        "\nСоздать: createpromo КОД DC ЛИМИТ [скрытый]\nКейс: createcasepromo КОД КЕЙС КЛЮЧИ ЛИМИТ [скрытый]"
    )
    buttons.append([InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")])
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promodel:"))
async def admin_promo_delete_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    code = callback.data.removeprefix("admin_promodel:")
    await callback.message.edit_text(
        f"🗑 Удалить промокод {code}?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить",
                        callback_data=f"admin_promoconfirm:{code}",
                    )
                ],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_promos")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promoconfirm:"))
async def admin_promo_delete_confirm_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    code = callback.data.removeprefix("admin_promoconfirm:")
    await db.delete_promo(code)
    await admin_promos_callback(callback)


async def render_admin_users(callback: CallbackQuery) -> None:
    async with connect(db.path) as conn:
        async with conn.execute("SELECT COUNT(*) FROM vip_users") as cur:
            vip_count = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT v.user_id, COALESCE(u.user_name, CAST(v.user_id AS TEXT)) FROM vip_users v LEFT JOIN user_stats u ON u.user_id=v.user_id AND u.chat_id=? LIMIT 20",
            (MAIN_CHAT_ID,),
        ) as cur:
            vip_rows = await cur.fetchall()
    bans = await db.get_ban_list()
    buttons = []
    text = f"👥 Пользователи\n\n👑 VIP: {vip_count} (первые 20; полный список: /viplist)\n"
    text += "\n".join((f"• {name} ({uid})" for uid, name in vip_rows)) if vip_rows else "Нет VIP"
    for uid, name in vip_rows:
        buttons.append(
            [InlineKeyboardButton(text=f"👑 Убрать VIP: {name}", callback_data=f"admin_unvip:{uid}")]
        )
    text += f"\n\n🚫 Заблокировано: {len(bans)}\n"
    text += (
        "\n".join((f"• {name} ({uid}) — {reason or 'без причины'}" for uid, name, reason in bans[:20]))
        if bans
        else "Список пуст"
    )
    for uid, name, _ in bans[:20]:
        buttons.append(
            [InlineKeyboardButton(text=f"🔓 Разбанить: {name}", callback_data=f"admin_unban:{uid}")]
        )
    text += "\n\nДобавление: vip ID или ban ID ПРИЧИНА"
    buttons.append([InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")])
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await render_admin_users(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_unvip:"))
async def admin_unvip_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        user_id = int(callback.data.removeprefix("admin_unvip:"))
    except (AttributeError, ValueError):
        await callback.answer("❌ Неверный ID", show_alert=True)
        return
    await db.remove_vip(user_id)
    await render_admin_users(callback)
    await callback.answer("✅ VIP снят")


@router.callback_query(F.data.startswith("admin_unban:"))
async def admin_unban_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        user_id = int(callback.data.removeprefix("admin_unban:"))
    except (AttributeError, ValueError):
        await callback.answer("❌ Неверный ID", show_alert=True)
        return
    await db.unban_user(user_id)
    await render_admin_users(callback)
    await callback.answer("✅ Пользователь разбанен")


ECONOMY_PRICE_META = {
    "chance_price": ("📈 +1 балл шанса", 1000),
    "gift_15": ("🎁 Подарок 15⭐", 10000),
    "gift_25": ("🎁 Подарок 25⭐", 10000),
    "gift_50": ("🎁 Подарок 50⭐", 10000),
    "gift_100": ("🎁 Подарок 100⭐", 10000),
    "premium_1m": ("💎 Premium 1 месяц", 50000),
}


def admin_economy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Цены обмена", callback_data="admin_econ_exchange")],
            [InlineKeyboardButton(text="⭐ Покупка DC", callback_data="admin_econ_stars")],
            [InlineKeyboardButton(text="🎲 Шансы кейсов", callback_data="admin_econ_cases")],
            [InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")],
        ]
    )


async def render_admin_economy(callback: CallbackQuery) -> None:
    text = "💰 Управление экономикой\n\nВсе изменения применяются сразу и сохраняются после перезапуска бота.\nВыбери раздел:"
    await callback.message.edit_text(text, reply_markup=admin_economy_keyboard())


@router.callback_query(F.data == "admin_economy")
async def admin_economy_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await render_admin_economy(callback)
    await callback.answer()


async def render_admin_exchange_prices(callback: CallbackQuery) -> None:
    buttons = []
    lines = ["🎁 Цены обмена", "", "➖/➕ меняют цену на указанный шаг:"]
    for key, (label, step) in ECONOMY_PRICE_META.items():
        value = economy_price(key)
        lines.append(f"{label}: {value:,} DC (шаг {step:,})".replace(",", " "))
        buttons.append(
            [
                InlineKeyboardButton(text="➖", callback_data=f"admin_eprice:{key}:down"),
                InlineKeyboardButton(text=f"{value:,} DC".replace(",", " "), callback_data="admin_noop"),
                InlineKeyboardButton(text="➕", callback_data=f"admin_eprice:{key}:up"),
            ]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Экономика", callback_data="admin_economy")])
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data == "admin_econ_exchange")
async def admin_exchange_prices_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await render_admin_exchange_prices(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_eprice:"))
async def admin_exchange_price_adjust_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        _, key, direction = callback.data.split(":", 2)
        _, step = ECONOMY_PRICE_META[key]
    except (AttributeError, KeyError, ValueError):
        await callback.answer("❌ Настройка не найдена", show_alert=True)
        return
    old_value = economy_price(key)
    new_value = max(step, old_value + (step if direction == "up" else -step))
    if new_value == old_value:
        await callback.answer(f"Минимальная цена: {step:,} DC".replace(",", " "), show_alert=True)
        return
    ECONOMY[key] = new_value
    await db.set_setting(f"economy:{key}", new_value)
    await render_admin_exchange_prices(callback)
    await callback.answer(f"✅ {old_value:,} → {new_value:,} DC".replace(",", " "))


async def render_admin_star_packages(callback: CallbackQuery) -> None:
    buttons = []
    lines = ["⭐ Цены покупки D-COINS", "", "➖/➕ меняют стоимость пакета на 10⭐:"]
    for dc_amount, stars in STAR_DC_PACKAGES.items():
        lines.append(f"{dc_amount:,} DC — {stars}⭐".replace(",", " "))
        buttons.append(
            [
                InlineKeyboardButton(text="➖10⭐", callback_data=f"admin_star:{dc_amount}:down"),
                InlineKeyboardButton(text=f"{dc_amount // 1000}K · {stars}⭐", callback_data="admin_noop"),
                InlineKeyboardButton(text="➕10⭐", callback_data=f"admin_star:{dc_amount}:up"),
            ]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Экономика", callback_data="admin_economy")])
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data == "admin_econ_stars")
async def admin_star_packages_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await render_admin_star_packages(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_star:"))
async def admin_star_package_adjust_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        _, dc_text, direction = callback.data.split(":", 2)
        dc_amount = int(dc_text)
        old_value = STAR_DC_PACKAGES[dc_amount]
    except (AttributeError, KeyError, ValueError):
        await callback.answer("❌ Пакет не найден", show_alert=True)
        return
    new_value = max(1, old_value + (10 if direction == "up" else -10))
    if new_value == old_value:
        await callback.answer("Минимальная цена: 1⭐", show_alert=True)
        return
    STAR_DC_PACKAGES[dc_amount] = new_value
    await db.set_setting(f"stars:{dc_amount}", new_value)
    await render_admin_star_packages(callback)
    await callback.answer(f"✅ {old_value}⭐ → {new_value}⭐")


def case_prize_label(reward: tuple) -> str:
    kind, value, _ = case_reward_parts(reward)
    return f"🎁 {value}⭐" if kind == "gift" else f"🪙 {value:,} DC".replace(",", " ")


def admin_case_select_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"🎲 {case['title']}", callback_data=f"admin_case:{case_id}")]
        for case_id, case in CASES.items()
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Экономика", callback_data="admin_economy")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_econ_cases")
async def admin_case_select_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🎲 Шансы кейсов\n\nВыбери кейс. Сумма шансов всегда останется равна 100%.",
        reply_markup=admin_case_select_keyboard(),
    )
    await callback.answer()


async def render_admin_case_chances(callback: CallbackQuery, case_id: str) -> None:
    case = CASES[case_id]
    lines = [
        f"🎲 {case['title']} — шансы",
        "",
        "Монеты меняются на 1%, подарки — на 0.05%:",
    ]
    buttons = []
    for index, reward in enumerate(case["rewards"]):
        kind, _, chance = case_reward_parts(reward)
        label = case_prize_label(reward)
        lines.append(f"{label}: {chance:.2f}%")
        step_label = "0.05" if kind == "gift" else "1"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"➖{step_label}",
                    callback_data=f"admin_caseadj:{case_id}:{index}:down",
                ),
                InlineKeyboardButton(text=f"{label} · {chance:.2f}%", callback_data="admin_noop"),
                InlineKeyboardButton(
                    text=f"➕{step_label}",
                    callback_data=f"admin_caseadj:{case_id}:{index}:up",
                ),
            ]
        )
    buttons.append([InlineKeyboardButton(text="◀️ Все кейсы", callback_data="admin_econ_cases")])
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("admin_case:"))
async def admin_case_chances_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    case_id = callback.data.removeprefix("admin_case:")
    if case_id not in CASES:
        await callback.answer("❌ Кейс не найден", show_alert=True)
        return
    await render_admin_case_chances(callback, case_id)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_caseadj:"))
async def admin_case_chance_adjust_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        _, case_id, index_text, direction = callback.data.split(":", 3)
        index = int(index_text)
        rewards = CASES[case_id]["rewards"]
        kind, _, old_chance = case_reward_parts(rewards[index])
    except (AttributeError, KeyError, ValueError, IndexError):
        await callback.answer("❌ Награда не найдена", show_alert=True)
        return
    step = 0.05 if kind == "gift" else 1.0
    new_chance = max(0.01, min(99.0, old_chance + (step if direction == "up" else -step)))
    if abs(new_chance - old_chance) < 1e-06:
        await callback.answer("Достигнут предел изменения", show_alert=True)
        return
    other_total = sum((case_reward_parts(item)[2] for i, item in enumerate(rewards) if i != index))
    if other_total <= 0:
        await callback.answer("❌ Нельзя пересчитать остальные шансы", show_alert=True)
        return
    scale = (100.0 - new_chance) / other_total
    for i, reward in enumerate(rewards):
        chance = new_chance if i == index else case_reward_parts(reward)[2] * scale
        rewards[i] = case_reward_tuple(reward, chance)
    await save_case_chances(case_id)
    await render_admin_case_chances(callback, case_id)
    await callback.answer(f"✅ {old_chance:.2f}% → {new_chance:.2f}%")


@router.callback_query(F.data == "admin_noop")
async def admin_noop_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "admin_commands")
async def admin_commands_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🛠 Основные админ-команды\n\naddcoins ID СУММА / removecoins ID СУММА\naddvisual ID СУММА / removevisual ID СУММА\nдатьдоступ ID / убратьдоступ ID / доступы\nответ ID ТЕКСТ / закрытьчат ID\naddmsgs ID КОЛ-ВО / removemsgs ID КОЛ-ВО\nvip ID / unvip ID / ban ID ПРИЧИНА / unban ID\ncreatepromo КОД DC [ЛИМИТ] [скрытый]\ncreatecasepromo КОД КЕЙС [КЛЮЧИ] [ЛИМИТ] [скрытый]\npromos / pending / premiumorders\nsay ТЕКСТ — сообщение в основную группу\nканал ТЕКСТ — публикация в новостном канале\nмини первый СУММА / мини первые КОЛ-ВО СУММА\nмини билет СЕКУНДЫ СУММА / мини отмена\nраздать СУММА — начислить DC всем пользователям бота\nраздать кейс НАЗВАНИЕ [КОЛ-ВО] — выдать всем ключи\nуронбоссу СУММА — вручную уменьшить HP босса\nдниивента +3 / дниивента -2 — изменить срок ивента\npopolnit — пополнить баланс звёзд бота",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    chance, msg_count, _ = await db.get_user(user_id, message.chat.id)
    wins = await db.get_wins_count(user_id, message.chat.id)
    balance = await db.get_display_balance(user_id)
    await message.reply(
        f"📊 Статистика:\n\n📈 Шанс: {chance:.3f} балл.\n💬 Сообщений: {msg_count}\n🏆 Побед: {wins}\n🪙 D-COINS: {balance}"
    )


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    top = await db.get_top(message.chat.id)
    text = "🏆 Топ участников:\n\n"
    for i, (name, chance, count) in enumerate(top, start=1):
        text += f"{i}. {name} — {chance:.3f} балл. ({count} сообщ.)\n"
    await message.reply(text)


@router.message(Command("winstop"))
async def cmd_winstop(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    top = await db.get_wins_top(message.chat.id)
    if not top:
        await message.reply("🏆 Побед пока нет.")
        return
    text = "🏆 Топ победителей:\n\n"
    for i, (name, cnt) in enumerate(top, start=1):
        text += f"{i}. {name} — {cnt} поб.\n"
    await message.reply(text)


@router.message(Command("cointop"))
async def cmd_cointop(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    top = await db.get_coins_top(10)
    if not top:
        await message.reply("🪙 D-COINS ни у кого нет.")
        return
    text = "🪙 Топ по D-COINS:\n\n"
    for i, (_, name, bal) in enumerate(top, start=1):
        text += f"{i}. {name} — {bal} DC\n"
    await message.reply(text)


@router.message(Command("coins"))
async def cmd_coins(message: Message) -> None:
    if message.chat.type != "private" and message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    balance = await db.get_display_balance(message.from_user.id)
    await message.reply(f"🪙 Твой баланс: {balance} D-COINS")


@router.message(Command("promo"), F.chat.type == "private")
async def cmd_promo(message: Message) -> None:
    if await db.is_banned(message.from_user.id):
        await message.answer(BAN_MESSAGE)
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /promo КОД")
        return
    code = args[1].upper()
    (
        status,
        reward,
        reward_type,
        case_id,
        case_count,
        uses,
        max_uses,
    ) = await db.redeem_promo(code, message.from_user.id)
    if status == "success":
        limit_text = f"{uses}/{max_uses}" if max_uses is not None else f"{uses}/∞"
        if reward_type == "case":
            keys = await db.get_case_keys(message.from_user.id, case_id)
            await message.answer(
                f"✅ Промокод активирован!\n🎟 Получено: {CASES[case_id]['title']} × {case_count}\n🔑 Ключей: {keys}\n👥 Активаций: {limit_text}"
            )
        else:
            balance = await db.get_display_balance(message.from_user.id)
            await message.answer(
                f"✅ Промокод активирован!\n🎁 Получено: {reward} DC\n🪙 Баланс: {balance} DC\n👥 Активаций: {limit_text}"
            )
    elif status == "already_used":
        await message.answer("❌ Ты уже активировал этот промокод.")
    elif status == "limit_reached":
        await message.answer("❌ Лимит активаций этого промокода исчерпан.")
    else:
        await message.answer("❌ Промокод не найден или уже отключён.")


@router.message(Command("transfer"))
async def cmd_transfer(message: Message, bot: Bot) -> None:
    sender_id = message.from_user.id
    if await db.is_banned(sender_id):
        await message.reply(BAN_MESSAGE)
        return
    if message.chat.id == MAIN_CHAT_ID and message.from_user.username:
        await db.set_username(
            sender_id,
            message.from_user.username,
            MAIN_CHAT_ID,
            display_name(message.from_user),
        )
    args = message.text.split()
    target_id = None
    target_name = None
    if len(args) == 3 and args[1].startswith("@"):
        username = args[1][1:].strip()
        try:
            amount = int(args[2])
        except ValueError:
            await message.reply("❌ Сумма должна быть целым числом.")
            return
        target = await db.find_user_by_username(username)
        if not target:
            await message.reply(
                "❌ Пользователь с таким username не найден.\nПользователь должен хотя бы один раз написать в основной группе, чтобы бот знал его ID."
            )
            return
        target_id, target_name = target
        try:
            recipient = await bot.get_chat(target_id)
        except Exception:
            await message.reply(
                "Не удалось подтвердить владельца username. Используйте ответ на его сообщение."
            )
            return
        if not recipient.username or recipient.username.lower() != username.lower():
            await message.reply("Username изменился. Используйте ответ на сообщение получателя.")
            return
    elif len(args) == 2 and message.reply_to_message and message.reply_to_message.from_user:
        try:
            amount = int(args[1])
        except ValueError:
            await message.reply("❌ Сумма должна быть целым числом.")
            return
        if message.reply_to_message.sender_chat or message.reply_to_message.from_user.is_bot:
            await message.reply("Переводы доступны только пользователям, не каналам и ботам.")
            return
        target_user = message.reply_to_message.from_user
        target_id = target_user.id
        target_name = display_name(target_user)
        if target_user.username:
            await db.set_username(target_id, target_user.username, MAIN_CHAT_ID, target_name)
    else:
        await message.reply(
            "❌ Использование:\n/transfer @username сумма\nили ответь на сообщение командой /transfer сумма"
        )
        return
    if amount <= 0:
        await message.reply("❌ Сумма должна быть больше 0 D-COINS.")
        return
    if target_id == sender_id:
        await message.reply("❌ Нельзя переводить D-COINS самому себе.")
        return
    if await db.is_banned(target_id):
        await message.reply("❌ Нельзя переводить D-COINS заблокированному пользователю.")
        return
    ok, sender_balance, recipient_balance = await db.transfer_coins(sender_id, target_id, amount)
    if not ok:
        balance, _ = await db.get_coins(sender_id)
        await message.reply(f"❌ Недостаточно D-COINS.\n💰 Реальный баланс: {balance} DC")
        return
    sender_balance = await db.get_display_balance(sender_id)
    recipient_balance = await db.get_display_balance(target_id)
    sender_name = display_name(message.from_user)
    if not target_name:
        target_name = await db.get_user_name(target_id)
    await message.reply(
        f"✅ Перевод выполнен!\n\n👤 Получатель: {target_name}\n💸 Переведено: {amount} DC\n🪙 Твой баланс: {sender_balance} DC"
    )
    try:
        await bot.send_message(
            target_id,
            f"💰 Тебе перевели {amount} D-COINS!\n\n👤 От: {sender_name}\n🪙 Твой баланс: {recipient_balance} DC",
        )
    except Exception as e:
        logger.info("Не удалось уведомить получателя %s: %s", target_id, e)


@router.message(Command("daytop"))
async def cmd_daytop(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        return
    top = await db.get_daily_top(message.chat.id)
    if not top:
        await message.reply("📊 Сегодня сообщений ещё нет.")
        return
    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%d.%m.%Y")
    text = f"📊 Топ активных за {today}:\n\n"
    for i, (name, count) in enumerate(top, start=1):
        text += f"{i}. {name} — {count} сообщ.\n"
    await message.reply(text)


@router.message(Command("bonus"))
async def cmd_bonus(message: Message) -> None:
    if message.chat.type != "private" and message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    name = display_name(message.from_user)
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    balance, last_coin_bonus = await db.get_coins(user_id)
    now = time.time()
    is_vip = await db.is_vip(user_id)
    chance_text = ""
    coin_text = ""
    if now - last_bonus >= BONUS_COOLDOWN:
        bonus_amount = round(random.uniform(0.05, 0.2), 3)
        new_chance = min(round(chance + bonus_amount, 3), MAX_CHANCE)
        await db.update_user(user_id, MAIN_CHAT_ID, name, new_chance, msg_count, now)
        chance_text = f"📈 Шанс: +{bonus_amount:.3f} балл. → {new_chance:.3f} балл."
    else:
        left = BONUS_COOLDOWN - (now - last_bonus)
        h, m = (int(left // 3600), int(left % 3600 // 60))
        chance_text = f"⏳ Шанс-бонус через: {h} ч. {m} мин."
    if now - last_coin_bonus >= COINS_BONUS_CD:
        coins_amount = COINS_VIP_BONUS if is_vip else COINS_BONUS
        await db.add_coins(user_id, coins_amount)
        new_balance = await db.get_display_balance(user_id)
        await db.set_coin_bonus_time(user_id)
        await school_event.record(
            user_id, name, f"bonus:{message.chat.id}:{message.message_id}", {"bonus": 1}
        )
        coin_text = f"🪙 D-COINS: +{coins_amount} → {new_balance} DC"
    else:
        left = COINS_BONUS_CD - (now - last_coin_bonus)
        h, m = (int(left // 3600), int(left % 3600 // 60))
        coin_text = f"⏳ Бонус монет через: {h} ч. {m} мин."
    await message.reply(f"🎁 Ежедневный бонус:\n\n{chance_text}\n{coin_text}")


@router.message(Command("cases"), F.chat.type == "private")
async def cmd_cases(message: Message) -> None:
    if await db.is_banned(message.from_user.id):
        await message.answer(BAN_MESSAGE)
        return
    await send_cases_menu(message, message.from_user.id)


async def send_cases_menu(message: Message, user_id: int) -> None:
    await message.answer("📦 Выбери кейс:", reply_markup=cases_keyboard())


async def show_pandora(callback: CallbackQuery, notice: str = "") -> None:
    remaining = await db.pandora_remaining(callback.from_user.id)
    status = (
        "✅ Можно открыть прямо сейчас!"
        if remaining <= 0
        else f"⏳ Следующее открытие через {format_pandora_wait(remaining)}."
    )
    text = f"🎁 Ящик Пандоры\n\nОдин бесплатный ящик для каждого игрока раз в 5 дней.\n\nВнутри может быть:\n🪙 от 1 000 до 50 000 DC\n🔑 ключ от любого кейса\n📈 +1 балл шанса\n🎁 Telegram-подарок от 15⭐ до 100⭐\n💎 Premium на месяц\n\n{status}"
    if notice:
        text = notice + "\n\n" + text
    await callback.message.edit_text(text, reply_markup=pandora_keyboard(remaining))


@router.callback_query(F.data == "pandora_view")
async def pandora_view_callback(callback: CallbackQuery) -> None:
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    await show_pandora(callback)
    await callback.answer()


@router.callback_query(F.data == "pandora_wait")
async def pandora_wait_callback(callback: CallbackQuery) -> None:
    remaining = await db.pandora_remaining(callback.from_user.id)
    if remaining <= 0:
        await show_pandora(callback)
        await callback.answer("🎁 Ящик уже доступен!")
    else:
        await callback.answer(
            f"Следующее открытие через {format_pandora_wait(remaining)}.",
            show_alert=True,
        )


@router.callback_query(F.data == "pandora_open")
async def pandora_open_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    claimed, remaining = await db.claim_pandora(user_id)
    if not claimed:
        await show_pandora(callback, "ℹ️ Этот Ящик Пандоры уже был открыт.")
        await callback.answer(f"Следующий через {format_pandora_wait(remaining)}.", show_alert=True)
        return
    kind, value, _ = random.choices(PANDORA_REWARDS, weights=[reward[2] for reward in PANDORA_REWARDS], k=1)[
        0
    ]
    user_name = display_name(callback.from_user)
    if kind == "coins":
        await db.add_coins(user_id, int(value))
        prize_text = f"🪙 {int(value):,} DC".replace(",", " ")
    elif kind == "key":
        await db.add_case_keys(user_id, str(value), 1)
        prize_text = f"🔑 Ключ от кейса {CASES[str(value)]['title']}"
    elif kind == "chance":
        chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
        new_chance = min(MAX_CHANCE, round(chance + float(value), 3))
        await db.update_user(user_id, MAIN_CHAT_ID, user_name, new_chance, msg_count, last_bonus)
        prize_text = f"📈 +{float(value):g} балл. шанса — теперь {new_chance:.3f} балл."
    elif kind == "gift":
        gift_key = {15: 5, 25: 10, 50: 15, 100: 20}[int(value)]
        gift_id = random.choice(GIFT_IDS[gift_key])
        try:
            await send_bot_gift(bot, user_id, gift_id)
            prize_text = f"🎁 Telegram-подарок за {int(value)}⭐ — в очереди доставки!"
        except Exception as error:
            await db.add_pending_gift(
                user_id,
                user_name,
                gift_id,
                f"Ящик Пандоры, подарок {int(value)}⭐: {error}",
            )
            prize_text = f"🎁 Telegram-подарок за {int(value)}⭐ — добавлен в очередь выдачи."
    else:
        order_id = await db.add_premium_order(user_id, user_name, 0)
        prize_text = f"💎 Premium на месяц — заявка #{order_id} передана администратору!"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🎁 Ящик Пандоры: выпал Premium\n👤 {user_name} ({user_id})\n📋 Заявка #{order_id}",
            )
        except Exception as error:
            logger.warning("Could not notify admin about Pandora Premium: %s", error)
    balance = await db.get_display_balance(user_id)
    remaining = await db.pandora_remaining(user_id)
    await callback.message.edit_text(
        f"🎁 Ящик Пандоры открыт!\n\n🎉 Выпало: {prize_text}\n🪙 Баланс: {balance:,} DC\n\nСледующий бесплатный ящик через {format_pandora_wait(remaining)}.".replace(
            ",", " "
        ),
        reply_markup=pandora_keyboard(remaining),
    )
    await send_game_log(
        bot,
        f"🎁 Ящик Пандоры\n👤 {user_name} ({user_id})\n🎉 {prize_text}\n🪙 Баланс: {balance:,} DC".replace(
            ",", " "
        ),
    )
    await callback.answer("🎁 Награда получена!")


async def show_case(callback: CallbackQuery, case_id: str) -> None:
    case = CASES[case_id]
    keys = await db.get_case_keys(callback.from_user.id, case_id)
    rewards = []
    for reward in case["rewards"]:
        if isinstance(reward[0], str):
            kind, value, _ = reward
            label = f"🎁 Подарок {value}⭐" if kind == "gift" else f"{value:,} DC".replace(",", " ")
        else:
            value, _ = reward
            label = f"{value:,} DC".replace(",", " ")
        rewards.append(f"• {label}")
    price_text = (
        "🏆 Эксклюзив: только за ключ"
        if case.get("key_only")
        else f"💰 Цена: {case['price']:,} DC".replace(",", " ")
    )
    await callback.message.edit_text(
        f"📦 Кейс {case['title']}\n\n{price_text}\n🔑 Твоих ключей: {keys}\n\n🎁 Возможные награды:\n"
        + "\n".join(rewards),
        reply_markup=case_detail_keyboard(case_id),
    )


@router.callback_query(F.data.startswith("case_view_"))
async def case_view_callback(callback: CallbackQuery) -> None:
    case_id = callback.data.removeprefix("case_view_")
    if case_id not in CASES:
        await callback.answer("Кейс не найден", show_alert=True)
        return
    await show_case(callback, case_id)
    await callback.answer()


@router.callback_query(F.data == "cases")
async def cases_callback(callback: CallbackQuery) -> None:
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    await callback.message.edit_text("📦 Выбери кейс:", reply_markup=cases_keyboard())
    await callback.answer()


async def open_case(callback: CallbackQuery, bot: Bot, case_id: str) -> None:
    user_id = callback.from_user.id
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    if user_id in case_open_cooldowns:
        await callback.answer(f"⏳ Следующее открытие через {CASE_OPEN_COOLDOWN} сек.", show_alert=True)
        return
    case = CASES[case_id]
    case_open_cooldowns[user_id] = True
    payment = await db.open_case(user_id, case_id, case["price"], case.get("key_only", False))
    if payment == "key_required":
        case_open_cooldowns.pop(user_id, None)
        await callback.answer(
            "🔑 Этот эксклюзивный кейс открывается только ключом из ивента, квеста или промокода.",
            show_alert=True,
        )
        return
    if payment == "insufficient":
        case_open_cooldowns.pop(user_id, None)
        balance, _ = await db.get_coins(user_id)
        await callback.answer(f"❌ Нужно {case['price']} DC, реальный баланс: {balance}", show_alert=True)
        return
    if isinstance(case["rewards"][0][0], str):
        if payment == "coins":
            await school_event.record(
                user_id,
                display_name(callback.from_user),
                f"case:{callback.id}",
                {"case": 1},
            )
        kind, reward, _ = random.choices(case["rewards"], weights=[item[2] for item in case["rewards"]], k=1)[
            0
        ]
    else:
        reward, _ = random.choices(case["rewards"], weights=[item[1] for item in case["rewards"]], k=1)[0]
        kind = "coins"
    if kind == "coins":
        await db.add_coins(user_id, reward)
        prize_text = f"🎉 Выпало: {reward:,} DC".replace(",", " ")
    else:
        gift_key = {15: 5, 25: 10, 50: 15, 100: 20}[reward]
        gift_id = random.choice(GIFT_IDS[gift_key])
        try:
            await send_bot_gift(bot, user_id, gift_id)
            prize_text = f"🎁 Выпал подарок {reward}⭐\n✅ Подарок поставлен в очередь доставки."
        except Exception as e:
            await db.add_pending_gift(
                user_id,
                await db.get_user_name(user_id),
                gift_id,
                f"кейс {case['title']}: {e}",
            )
            prize_text = f"🎁 Выпал подарок {reward}⭐\n⏳ Добавлен в очередь выдачи."
    new_balance = await db.get_display_balance(user_id)
    keys = await db.get_case_keys(user_id, case_id)
    payment_text = (
        "🔑 Использован ключ кейса"
        if payment == "key"
        else f"💸 Списано: {case['price']:,} DC".replace(",", " ")
    )
    result_text = f"📦 {case['title']} открыт!\n\n{prize_text}\n{payment_text}\n🪙 Баланс: {new_balance:,} DC\n🔑 Ключей: {keys}".replace(
        ",", " "
    )
    await callback.message.edit_text(result_text, reply_markup=case_detail_keyboard(case_id))
    await send_game_log(
        bot,
        f"📦 Открыт кейс {case['title']}\n👤 {display_name(callback.from_user)} ({user_id})\n{payment_text}\n{prize_text}\n🪙 Баланс: {new_balance:,} DC".replace(
            ",", " "
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "case_open_blood")
async def open_blood_case(callback: CallbackQuery, bot: Bot) -> None:
    await open_case(callback, bot, "blood")


@router.callback_query(F.data == "case_open_pantera")
async def open_pantera_case(callback: CallbackQuery, bot: Bot) -> None:
    await open_case(callback, bot, "pantera")


@router.callback_query(F.data == "case_open_spider_man")
async def open_spider_man_case(callback: CallbackQuery, bot: Bot) -> None:
    await open_case(callback, bot, "spider_man")


@router.callback_query(F.data == "case_open_school")
async def open_school_case(callback: CallbackQuery, bot: Bot) -> None:
    await open_case(callback, bot, "school")


@router.callback_query(F.data == "case_open_student")
async def open_student_case(callback: CallbackQuery, bot: Bot) -> None:
    await open_case(callback, bot, "student")


@router.callback_query(F.data == "case_open_excellent")
async def open_excellent_case(callback: CallbackQuery, bot: Bot) -> None:
    await open_case(callback, bot, "excellent")


@router.callback_query(F.data == "games")
async def games_menu(callback: CallbackQuery) -> None:
    pending_game_bets.pop(callback.from_user.id, None)
    await callback.message.edit_text(
        f"🎮 Игры\n\nВыбери игру. После этого бот попросит отправить сумму ставки сообщением.\nМинимальная ставка: {CASINO_MIN_BET} DC. Ставки списываются только с реального баланса.",
        reply_markup=games_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, bot: Bot) -> None:
    pending_game_bets.pop(callback.from_user.id, None)
    if not await is_channel_subscriber(bot, callback.from_user.id):
        await callback.message.edit_text(
            "📢 Чтобы пользоваться ботом, подпишись на наш канал.",
            reply_markup=subscription_keyboard(),
        )
        await callback.answer("Нужна подписка", show_alert=True)
        return
    support_access = await db.has_support_access(callback.from_user.id)
    await callback.message.edit_text(
        "👋 Добро пожаловать!\n\nВыберите действие:",
        reply_markup=start_keyboard(callback.from_user.id == ADMIN_ID, support_access),
    )
    await callback.answer()


def jackpot_next_draw_text() -> str:
    tz = pytz.timezone("Europe/Moscow")
    now = datetime.now(tz)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = int((midnight - now).total_seconds())
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"сегодня в 00:00 МСК (через {hours} ч. {minutes} мин.)"


async def show_jackpot(callback: CallbackQuery) -> None:
    summary = await db.jackpot_summary(callback.from_user.id)
    if summary["tickets"] >= JACKPOT_TICKET_LIMIT:
        ticket_note = "🎟 Лимит билетов на сегодня достигнут: 20 / 20."
    else:
        until_ticket = JACKPOT_TICKET_STEP - summary["loss"] % JACKPOT_TICKET_STEP
        ticket_note = f"🎟 Твои билеты: {summary['tickets']} / {JACKPOT_TICKET_LIMIT}\nДо следующего билета: {until_ticket:,} DC чистого проигрыша.".replace(
            ",", " "
        )
    text = f"🏆 Джекпот дня\n\n💰 Банк: {summary['pool']:,} DC\n👥 Участников: {summary['players']}\n⏳ Розыгрыш: {jackpot_next_draw_text()}\n\n{ticket_note}\n\nЗа каждые 5 000 DC чистого проигрыша в играх начисляется 1 билет.\nМаксимум — 20 билетов за день. Дуэли не участвуют.".replace(
        ",", " "
    )
    await callback.message.edit_text(text, reply_markup=jackpot_keyboard())


@router.callback_query(F.data == "jackpot:view")
async def jackpot_view_callback(callback: CallbackQuery) -> None:
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    await show_jackpot(callback)
    await callback.answer()


@router.callback_query(F.data == "jackpot:top")
async def jackpot_top_callback(callback: CallbackQuery) -> None:
    rows = await db.jackpot_top()
    if rows:
        ranking = "\n".join((f"{index}. {row[0]} — {row[1]} бил." for index, row in enumerate(rows, 1)))
    else:
        ranking = "Пока никто не получил билет."
    await callback.message.edit_text(f"📊 Топ билетов сегодня\n\n{ranking}", reply_markup=jackpot_keyboard())
    await callback.answer()


@router.callback_query(F.data == "jackpot:winners")
async def jackpot_winners_callback(callback: CallbackQuery) -> None:
    rows = await db.jackpot_winners()
    if rows:
        history = "\n".join((f"• {row[2]} — {row[0]}: {row[1]:,} DC".replace(",", " ") for row in rows))
    else:
        history = "Розыгрышей пока не было."
    await callback.message.edit_text(f"🏅 Последние победители\n\n{history}", reply_markup=jackpot_keyboard())
    await callback.answer()


async def ask_game_bet(callback: CallbackQuery, game: str, option: str = "-") -> None:
    names = {
        "slots": "🎰 Слоты",
        "mines": "💣 Мины",
        "scratch": "🎟 Скретч-карты",
        "roulette": "🎡 Рулетка",
        "dice": "🎲 Кубик",
        "coinflip": "🪙 Орёл и решка",
        "lottery": "🎫 Лотерея 5×5",
    }
    details = ""
    if game == "roulette":
        details = "\nЦвет: " + ("🔴 красное" if option == "red" else "⚫ чёрное")
    elif game == "dice":
        details = f"\nВыбранное число: {option}"
    elif game == "coinflip":
        details = "\nВыбрано: " + ("🦅 орёл" if option == "heads" else "🔵 решка")
    pending_game_bets[callback.from_user.id] = {
        "game": game,
        "option": option,
        "expires": time.time() + 120,
    }
    await callback.message.edit_text(
        f"{names[game]}{details}\n\n✍️ Отправь сумму ставки одним сообщением.\nМинимум: {CASINO_MIN_BET} DC\n\nПример: 13750",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="games")]]
        ),
    )


@router.callback_query(F.data.startswith("game:"))
async def game_options_menu(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    game = parts[1] if len(parts) > 1 else ""
    option = parts[2] if len(parts) > 2 else None
    if game in {"slots", "mines", "scratch", "lottery"}:
        await ask_game_bet(callback, game)
    elif game == "roulette" and option in {"red", "black"}:
        await ask_game_bet(callback, game, option)
    elif game == "roulette":
        await callback.message.edit_text(
            "🎡 Рулетка\n\nВыбери цвет:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔴 Красное", callback_data="game:roulette:red"),
                        InlineKeyboardButton(text="⚫ Чёрное", callback_data="game:roulette:black"),
                    ],
                    [InlineKeyboardButton(text="⬅️ Все игры", callback_data="games")],
                ]
            ),
        )
    elif game == "dice" and option in {"1", "2", "3", "4", "5", "6"}:
        await ask_game_bet(callback, game, option)
    elif game == "dice":
        await callback.message.edit_text(
            "🎲 Кубик\n\nКакое число выпадет?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=str(number), callback_data=f"game:dice:{number}")
                        for number in range(1, 4)
                    ],
                    [
                        InlineKeyboardButton(text=str(number), callback_data=f"game:dice:{number}")
                        for number in range(4, 7)
                    ],
                    [InlineKeyboardButton(text="⬅️ Все игры", callback_data="games")],
                ]
            ),
        )
    elif game == "coinflip" and option in {"heads", "tails"}:
        await ask_game_bet(callback, game, option)
    elif game == "coinflip":
        await callback.message.edit_text(
            "🪙 Орёл и решка\n\nВыбери сторону:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🦅 Орёл", callback_data="game:coinflip:heads"),
                        InlineKeyboardButton(text="🔵 Решка", callback_data="game:coinflip:tails"),
                    ],
                    [InlineKeyboardButton(text="⬅️ Все игры", callback_data="games")],
                ]
            ),
        )
    else:
        await callback.answer("Игра не найдена.", show_alert=True)
        return
    await callback.answer()


async def launch_selected_game(message: Message, bot: Bot, game: str, option: str, bet: int) -> None:
    commands = {
        "slots": f"/slots {bet}",
        "mines": f"/mines {bet}",
        "scratch": f"/scratch {bet}",
        "roulette": f"/roulette {option} {bet}",
        "dice": f"/dice {option} {bet}",
        "coinflip": f"/coinflip {option} {bet}",
        "lottery": f"/lottery {bet}",
    }
    command = commands.get(game)
    if command is None:
        await message.reply("❌ Игра не найдена.")
        return
    game_message = message.model_copy(update={"text": command})
    if game == "mines":
        await cmd_mines(game_message)
    elif game == "scratch":
        await cmd_scratch(game_message, bot)
    elif game == "slots":
        await cmd_slots(game_message, bot)
    elif game == "roulette":
        await cmd_roulette(game_message, bot)
    elif game == "coinflip":
        await cmd_coinflip(game_message, bot)
    elif game == "lottery":
        await cmd_lottery(game_message, bot)
    else:
        await cmd_dice(game_message, bot)


def has_pending_game_bet(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in pending_game_bets)


@router.message(F.chat.type == "private", has_pending_game_bet)
async def receive_inline_game_bet(message: Message, bot: Bot) -> None:
    pending = pending_game_bets.get(message.from_user.id)
    if not pending:
        return
    if pending["expires"] < time.time():
        pending_game_bets.pop(message.from_user.id, None)
        await message.reply("⌛ Время выбора ставки вышло. Открой раздел «🎮 Игры» заново.")
        return
    text = (message.text or "").strip()
    if text.lower() in {"отмена", "cancel"}:
        pending_game_bets.pop(message.from_user.id, None)
        await message.reply("❌ Выбор ставки отменён.", reply_markup=games_keyboard())
        return
    normalized = text.replace(" ", "").replace("_", "")
    if not normalized.isdigit():
        await message.reply("❌ Отправь только сумму числом. Например: 13750\nДля отмены напиши: отмена")
        return
    bet = int(normalized)
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC. Отправь другую сумму.")
        return
    if bet > 1000000000:
        await message.reply("❌ Максимальная ставка: 1 000 000 000 DC. Отправь другую сумму.")
        return
    pending_game_bets.pop(message.from_user.id, None)
    await launch_selected_game(message, bot, pending["game"], pending["option"], bet)


@router.callback_query(F.data.startswith("gb:"))
async def inline_game_bet(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, game, option, raw_bet = callback.data.split(":", 3)
        bet = int(raw_bet)
    except (AttributeError, ValueError):
        await callback.answer("Некорректная ставка.", show_alert=True)
        return
    if bet not in GAME_BET_PRESETS:
        await callback.answer("Этой ставки нет в меню.", show_alert=True)
        return
    if game == "roulette" and option not in {"red", "black"}:
        await callback.answer("Сначала выбери цвет.", show_alert=True)
        return
    if game == "dice" and option not in {"1", "2", "3", "4", "5", "6"}:
        await callback.answer("Сначала выбери число.", show_alert=True)
        return
    if game == "coinflip" and option not in {"heads", "tails"}:
        await callback.answer("Сначала выбери сторону.", show_alert=True)
        return
    if game not in {
        "slots",
        "mines",
        "scratch",
        "roulette",
        "dice",
        "coinflip",
        "lottery",
    }:
        await callback.answer("Игра не найдена.", show_alert=True)
        return
    await callback.answer("🎮 Запускаю игру")
    message = callback.message.model_copy(update={"from_user": callback.from_user})
    await launch_selected_game(message, bot, game, option, bet)


@router.message(Command("slots"))
async def cmd_slots(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Использование: /slots [ставка]\nПример: /slots 50")
        return
    try:
        bet = int(args[1])
    except ValueError:
        await message.reply("❌ Ставка должна быть числом.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    s1 = random.choice(SLOT_SYMBOLS)
    s2 = random.choice(SLOT_SYMBOLS)
    s3 = random.choice(SLOT_SYMBOLS)
    if s1 == s2 == s3:
        win = bet * 2
        await db.add_coins(user_id, win)
        await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, True)
        new_balance = await db.get_display_balance(user_id)
        await message.reply(
            f"🎰 {s1} {s2} {s3}\n\n✅ Ты выиграл!\n💸 Ставка: {bet} DC\n🏆 Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC"
        )
        await send_game_log(
            bot,
            f"🎰 Слоты\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC\n✅ Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC",
        )
    else:
        boss_note = await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, False)
        balance_after = await db.get_display_balance(user_id)
        await message.reply(
            f"🎰 {s1} {s2} {s3}\n\n❌ Не повезло!\n💸 Ставка: {bet} DC\n🪙 Баланс: {balance_after} DC{boss_note}"
        )
        await send_game_log(
            bot,
            f"🎰 Слоты\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC\n❌ Проигрыш\n🪙 Баланс: {balance_after} DC",
        )


@router.message(Command("roulette"))
async def cmd_roulette(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) < 3:
        await message.reply("Использование: /roulette [red/black] [ставка]\nПример: /roulette red 50")
        return
    color = args[1].lower()
    if color not in ("red", "black"):
        await message.reply("❌ Выбери цвет: red или black\nПример: /roulette red 50")
        return
    try:
        bet = int(args[2])
    except ValueError:
        await message.reply("❌ Ставка должна быть числом.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    result_color = random.choice(ROULETTE_WHEEL)
    emoji_map = {"red": "🔴", "black": "⚫", "green": "🟢"}
    result_emoji = emoji_map[result_color]
    chosen_emoji = "🔴" if color == "red" else "⚫"
    if result_color == color:
        win = bet * 2
        await db.add_coins(user_id, win)
        await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, True)
        new_balance = await db.get_display_balance(user_id)
        await message.reply(
            f"🎡 Выпало: {result_emoji}\n\n✅ Ты выиграл!\n💸 Ставка: {bet} DC на {chosen_emoji}\n🏆 Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC"
        )
        await send_game_log(
            bot,
            f"🎡 Рулетка\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {chosen_emoji}\nВыпало: {result_emoji}\n✅ Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC",
        )
    else:
        boss_note = await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, False)
        new_balance = await db.get_display_balance(user_id)
        await message.reply(
            f"🎡 Выпало: {result_emoji}\n\n❌ Не повезло!\n💸 Ставка: {bet} DC на {chosen_emoji}\n🪙 Баланс: {new_balance} DC{boss_note}"
        )
        await send_game_log(
            bot,
            f"🎡 Рулетка\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {chosen_emoji}\nВыпало: {result_emoji}\n❌ Проигрыш\n🪙 Баланс: {new_balance} DC",
        )


@router.message(Command("dice"))
async def cmd_dice(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) < 3:
        await message.reply("Использование: /dice [число 1-6] [ставка]\nПример: /dice 3 50")
        return
    try:
        number = int(args[1])
        bet = int(args[2])
    except ValueError:
        await message.reply("❌ Число и ставка должны быть числами.")
        return
    if number < 1 or number > 6:
        await message.reply("❌ Число должно быть от 1 до 6.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    rolled = random.randint(1, 6)
    if rolled == number:
        win = bet * 2
        await db.add_coins(user_id, win)
        await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, True)
        new_balance = await db.get_display_balance(user_id)
        await message.reply(
            f"🎲 Выпало: {rolled}\n\n✅ Угадал!\n💸 Ставка: {bet} DC на {number}\n🏆 Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC"
        )
        await send_game_log(
            bot,
            f"🎲 Кубик\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {number}\nВыпало: {rolled}\n✅ Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC",
        )
    else:
        boss_note = await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, False)
        new_balance = await db.get_display_balance(user_id)
        await message.reply(
            f"🎲 Выпало: {rolled}\n\n❌ Не угадал! (ты выбрал {number})\n💸 Ставка: {bet} DC\n🪙 Баланс: {new_balance} DC{boss_note}"
        )
        await send_game_log(
            bot,
            f"🎲 Кубик\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {number}\nВыпало: {rolled}\n❌ Проигрыш\n🪙 Баланс: {new_balance} DC",
        )


@router.message(Command("coinflip"))
async def cmd_coinflip(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) != 3:
        await message.reply("Использование: монетка орёл/решка ставка\nПример: монетка орёл 1000")
        return
    side = args[1].lower()
    aliases = {
        "орел": "heads",
        "орёл": "heads",
        "решка": "tails",
        "heads": "heads",
        "tails": "tails",
    }
    side = aliases.get(side, side)
    if side not in {"heads", "tails"}:
        await message.reply("❌ Выбери сторону: орёл или решка.")
        return
    try:
        bet = int(args[2])
    except ValueError:
        await message.reply("❌ Ставка должна быть числом.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    result = random.choice(("heads", "tails"))
    result_text = "🦅 Орёл" if result == "heads" else "🔵 Решка"
    chosen_text = "🦅 Орёл" if side == "heads" else "🔵 Решка"
    if result == side:
        prize = bet * 2
        await db.add_coins(user_id, prize)
        await school_game(message.from_user, f"coinflip:{message.chat.id}:{message.message_id}", bet, True)
        balance_after = await db.get_display_balance(user_id)
        text = f"🪙 Выпало: {result_text}\n\n✅ Ты выиграл!\n💸 Ставка: {bet:,} DC на {chosen_text}\n🏆 Выигрыш: {prize:,} DC\n🪙 Баланс: {balance_after:,} DC".replace(
            ",", " "
        )
        log_result = f"✅ Выигрыш: {prize} DC"
    else:
        boss_note = await school_game(message.from_user, f"coinflip:{message.chat.id}:{message.message_id}", bet, False)
        balance_after = await db.get_display_balance(user_id)
        text = f"🪙 Выпало: {result_text}\n\n❌ Ты проиграл.\n💸 Ставка: {bet:,} DC на {chosen_text}\n🪙 Баланс: {balance_after:,} DC{boss_note}".replace(
            ",", " "
        )
        log_result = "❌ Проигрыш"
    await message.reply(text)
    await send_game_log(
        bot,
        f"🪙 Орёл и решка\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {chosen_text}\nВыпало: {result_text}\n{log_result}\n🪙 Баланс: {balance_after} DC",
    )


async def start_mines_game(message: Message) -> None:
    user_id = message.from_user.id
    if await db.is_banned(user_id):
        await message.reply(BAN_MESSAGE)
        return
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.reply("Использование: /mines ставка\nПример: /mines 2500")
        return
    try:
        bet = int(args[1])
    except ValueError:
        await message.reply("❌ Ставка должна быть числом.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    if not await db.remove_coins(user_id, bet):
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    casino_bet_cooldowns[user_id] = True
    game = {
        "game": "mines",
        "token": secrets.token_hex(12),
        "bet": bet,
        "mines": set(random.sample(range(MINES_GRID_SIZE), MINES_COUNT)),
        "opened": set(),
        "chat_id": message.chat.id,
        "expires": time.time() + CASINO_TIMEOUT,
    }
    active_games[user_id] = game
    sent = await message.reply(mines_text(game), reply_markup=mines_keyboard(game))
    game["message_id"] = sent.message_id


@router.message(Command("mines"))
async def cmd_mines(message: Message) -> None:
    if message.chat.type != "private":
        return
    await start_mines_game(message)


@router.message(F.chat.type == "private", F.text.regexp("(?i)^мины\\s+\\d+\\s*$"))
async def cmd_mines_text(message: Message) -> None:
    await start_mines_game(message)


@router.callback_query(F.data.startswith("mines_cell_"))
async def mines_open_cell(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game["expires"] <= time.time() or game.get("game") != "mines":
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    if callback.message.message_id != game.get("message_id") or callback.message.chat.id != game["chat_id"]:
        await callback.answer("Это поле от другой игры.", show_alert=True)
        return
    try:
        cell = int(callback.data.removeprefix("mines_cell_"))
    except (ValueError, AttributeError):
        await callback.answer("Некорректная клетка.", show_alert=True)
        return
    if cell < 0 or cell >= MINES_GRID_SIZE or cell in game["opened"]:
        await callback.answer("Эта клетка уже открыта.", show_alert=True)
        return
    if cell in game["mines"]:
        active_games.pop(user_id, None)
        boss_note = await school_game(callback.from_user, f"mines:{game['token']}", game["bet"], False)
        balance = await db.get_display_balance(user_id)
        await callback.message.edit_text(
            "💣 Игра завершена!\n💵 Вы проиграли." + boss_note,
            reply_markup=mines_keyboard(game, reveal=True),
        )
        await send_game_log(
            bot,
            f"💣 Мины\n👤 {display_name(callback.from_user)} ({user_id})\n💸 Ставка: {game['bet']} DC\n❌ Проигрыш\n🪙 Баланс: {balance} DC",
        )
        await callback.answer("💥 Мина!")
        return
    game["opened"].add(cell)
    await school_event.record(
        user_id,
        display_name(callback.from_user),
        f"safe:{game['token']}:{cell}",
        {"safe": 1},
    )
    if len(game["opened"]) == MINES_GRID_SIZE - MINES_COUNT:
        prize = mines_prize(game["bet"], len(game["opened"]))
        active_games.pop(user_id, None)
        await db.add_coins(user_id, prize)
        await school_game(callback.from_user, f"mines:{game['token']}", game["bet"], True)
        balance = await db.get_display_balance(user_id)
        await callback.message.edit_text(
            f"🏆 Поле очищено!\n💵 Выигрыш: x{mines_multiplier(len(game['opened'])):.2f} | {prize:,} DC\n🪙 Баланс: {balance:,} DC".replace(
                ",", " "
            ),
            reply_markup=mines_keyboard(game, reveal=True),
        )
        await send_game_log(
            bot,
            f"💣 Мины\n👤 {display_name(callback.from_user)} ({user_id})\n💸 Ставка: {game['bet']} DC\n🏆 Поле очищено: +{prize} DC\n🪙 Баланс: {balance} DC",
        )
    else:
        await callback.message.edit_text(mines_text(game), reply_markup=mines_keyboard(game))
    await callback.answer()


@router.callback_query(F.data == "mines_cashout")
async def mines_cashout(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game["expires"] <= time.time() or game.get("game") != "mines":
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    if callback.message.message_id != game.get("message_id") or callback.message.chat.id != game["chat_id"]:
        await callback.answer("Это поле от другой игры.", show_alert=True)
        return
    safe_opened = len(game["opened"])
    if not safe_opened:
        await callback.answer("Открой хотя бы одну клетку.", show_alert=True)
        return
    prize = mines_prize(game["bet"], safe_opened)
    active_games.pop(user_id, None)
    await db.add_coins(user_id, prize)
    await school_game(callback.from_user, f"mines:{game['token']}", game["bet"], True)
    balance = await db.get_display_balance(user_id)
    await callback.message.edit_text(
        f"✅ Вы забрали выигрыш!\n💵 Выигрыш: x{mines_multiplier(safe_opened):.2f} | {prize:,} DC\n🪙 Баланс: {balance:,} DC".replace(
            ",", " "
        ),
        reply_markup=mines_keyboard(game, reveal=True),
    )
    await send_game_log(
        bot,
        f"💣 Мины\n👤 {display_name(callback.from_user)} ({user_id})\n💸 Ставка: {game['bet']} DC\n✅ Забрал: {prize} DC (x{mines_multiplier(safe_opened):.2f})\n🪙 Баланс: {balance} DC",
    )
    await callback.answer("✅ Выигрыш зачислен")


@router.callback_query(F.data == "mines_done")
async def mines_done(callback: CallbackQuery) -> None:
    await callback.answer("Игра уже завершена.")


async def settle_scratch(user, game: dict) -> tuple[str, str, int]:
    user_id = user.id
    game["opened"] = set(range(9))
    if game["win"]:
        prize = game["bet"] * SCRATCH_MULTIPLIER
        await db.add_coins(user_id, prize)
        await school_game(user, f"scratch:{game['token']}", game["bet"], True)
        balance = await db.get_display_balance(user_id)
        result = f"🎟 Скретч-карты\n\n🎉 ПОБЕДА! Найдены 3 одинаковых символа.\n💰 Выигрыш: {prize:,} DC (×{SCRATCH_MULTIPLIER})\n🪙 Баланс: {balance:,} DC".replace(
            ",", " "
        )
        log_result = f"✅ Выигрыш: {prize} DC"
    else:
        boss_note = await school_game(user, f"scratch:{game['token']}", game["bet"], False)
        balance = await db.get_display_balance(user_id)
        result = f"🎟 Скретч-карты\n\n❌ Трёх одинаковых символов нет. Ты проиграл.\n💸 Ставка: {game['bet']:,} DC\n🪙 Баланс: {balance:,} DC{boss_note}".replace(
            ",", " "
        )
        log_result = "❌ Проигрыш"
    return (result, log_result, balance)


async def start_scratch_game(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    if await db.is_banned(user_id):
        await message.reply(BAN_MESSAGE)
        return
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.reply("Использование: скретч ставка\nПример: скретч 1000")
        return
    try:
        bet = int(args[1])
    except ValueError:
        await message.reply("❌ Ставка должна быть числом.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    if not await db.remove_coins(user_id, bet):
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    casino_bet_cooldowns[user_id] = True
    win = random.random() < SCRATCH_WIN_CHANCE
    game = {
        "game": "scratch",
        "token": secrets.token_hex(12),
        "bet": bet,
        "board": create_scratch_board(win),
        "opened": set(range(9)),
        "win": win,
        "balance": await db.get_display_balance(user_id),
        "chat_id": message.chat.id,
        "expires": time.time() + CASINO_TIMEOUT,
    }
    result, log_result, balance = await settle_scratch(message.from_user, game)
    await message.reply(result, reply_markup=scratch_keyboard(game, reveal=True))
    await send_game_log(
        bot,
        f"🎟 Скретч-карты\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC\n{log_result}\n🪙 Баланс: {balance} DC",
    )


@router.message(Command("scratch"))
async def cmd_scratch(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    await start_scratch_game(message, bot)


async def finish_scratch_game(callback: CallbackQuery, bot: Bot, game: dict) -> None:
    user_id = callback.from_user.id
    active_games.pop(user_id, None)
    result, log_result, balance = await settle_scratch(callback.from_user, game)
    await callback.message.edit_text(result, reply_markup=scratch_keyboard(game, reveal=True))
    await send_game_log(
        bot,
        f"🎟 Скретч-карты\n👤 {display_name(callback.from_user)} ({user_id})\n💸 Ставка: {game['bet']} DC\n{log_result}\n🪙 Баланс: {balance} DC",
    )


@router.callback_query(F.data.startswith("scratch_cell_"))
async def scratch_open_cell(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game["expires"] <= time.time() or game.get("game") != "scratch":
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    if callback.message.message_id != game.get("message_id") or callback.message.chat.id != game["chat_id"]:
        await callback.answer("Это поле от другой игры.", show_alert=True)
        return
    try:
        cell = int(callback.data.removeprefix("scratch_cell_"))
    except (AttributeError, ValueError):
        await callback.answer("Некорректная клетка.", show_alert=True)
        return
    if cell < 0 or cell >= 9 or cell in game["opened"]:
        await callback.answer("Эта клетка уже открыта.")
        return
    game["opened"].add(cell)
    if len(game["opened"]) == 9:
        await finish_scratch_game(callback, bot, game)
    else:
        await callback.message.edit_text(scratch_text(game), reply_markup=scratch_keyboard(game))
    await callback.answer()


@router.callback_query(F.data == "scratch_reveal")
async def scratch_reveal(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game["expires"] <= time.time() or game.get("game") != "scratch":
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    if callback.message.message_id != game.get("message_id") or callback.message.chat.id != game["chat_id"]:
        await callback.answer("Это поле от другой игры.", show_alert=True)
        return
    await finish_scratch_game(callback, bot, game)
    await callback.answer("🎟 Карта открыта")


@router.callback_query(F.data == "scratch_done")
async def scratch_done(callback: CallbackQuery) -> None:
    await callback.answer("Игра уже завершена.")


async def start_lottery_game(message: Message) -> None:
    user_id = message.from_user.id
    if await db.is_banned(user_id):
        await message.reply(BAN_MESSAGE)
        return
    if user_id in active_games:
        await message.reply("🎰 У тебя уже есть активная игра! Сначала заверши её.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.reply("Использование: лотерея ставка\nПример: лотерея 1000")
        return
    try:
        bet = int(args[1])
    except ValueError:
        await message.reply("❌ Ставка должна быть числом.")
        return
    if not CASINO_MIN_BET <= bet <= MAX_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    if not await db.remove_coins(user_id, bet):
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n💰 Реальный баланс: {balance} DC")
        return
    casino_bet_cooldowns[user_id] = True
    board = list(LOTTERY_MULTIPLIERS)
    random.shuffle(board)
    game = {
        "game": "lottery",
        "token": secrets.token_hex(12),
        "bet": bet,
        "board": board,
        "chat_id": message.chat.id,
        "expires": time.time() + CASINO_TIMEOUT,
    }
    active_games[user_id] = game
    sent = await message.reply(lottery_text(game), reply_markup=lottery_keyboard(game))
    game["message_id"] = sent.message_id


@router.message(Command("lottery"))
async def cmd_lottery(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    await start_lottery_game(message)


@router.callback_query(F.data.startswith("lottery_cell_"))
async def lottery_open_ticket(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game["expires"] <= time.time() or game.get("game") != "lottery":
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    if callback.message.message_id != game.get("message_id") or callback.message.chat.id != game["chat_id"]:
        await callback.answer("Это поле от другой игры.", show_alert=True)
        return
    try:
        cell = int(callback.data.removeprefix("lottery_cell_"))
    except (AttributeError, ValueError):
        await callback.answer("Некорректный билет.", show_alert=True)
        return
    if cell < 0 or cell >= 25:
        await callback.answer("Некорректный билет.", show_alert=True)
        return
    active_games.pop(user_id, None)
    multiplier = float(game["board"][cell])
    prize = int(game["bet"] * multiplier)
    if prize:
        await db.add_coins(user_id, prize)
    if multiplier >= 1:
        boss_note = await school_game(callback.from_user, f"lottery:{game['token']}", game["bet"], True)
    else:
        net_loss = game["bet"] - prize
        damage, refund = await school_event.record(
            user_id,
            display_name(callback.from_user),
            f"lottery:{game['token']}",
            {"games": 1, "gamewin": 0, "bets": game["bet"]},
            loss=net_loss,
        )
        await jackpot_loss(
            callback.from_user,
            max(0, net_loss - refund),
            f"lottery:{game['token']}:jackpot",
        )
        boss_note = (
            f"\n📚 Урон боссу: {damage:,}. Возвращено: {refund:,} DC".replace(",", " ") if damage else ""
        )
    balance = await db.get_display_balance(user_id)
    if multiplier > 1:
        headline = "🎉 Победа!"
    elif multiplier == 1:
        headline = "↩️ Ставка вернулась."
    elif multiplier > 0:
        headline = "😕 Вернулась только часть ставки."
    else:
        headline = "❌ Билет оказался пустым."
    bet_text = f"{game['bet']:,}".replace(",", " ")
    prize_text = f"{prize:,}".replace(",", " ")
    balance_text = f"{balance:,}".replace(",", " ")
    result = f"🎫 Лотерея завершена!\n\n{headline}\n🎯 Коэффициент: {lottery_multiplier_text(multiplier)}\n💸 Ставка: {bet_text} DC\n💰 Выплата: {prize_text} DC\n🪙 Баланс: {balance_text} DC{boss_note}"
    await callback.message.edit_text(result, reply_markup=lottery_keyboard(game, reveal=True, selected=cell))
    await send_game_log(
        bot,
        f"🎫 Лотерея 5×5\n👤 {display_name(callback.from_user)} ({user_id})\n💸 Ставка: {bet_text} DC\n🎯 Коэффициент: {lottery_multiplier_text(multiplier)}\n💰 Выплата: {prize_text} DC\n🪙 Баланс: {balance_text} DC",
    )
    await callback.answer(f"🎯 {lottery_multiplier_text(multiplier)}")


@router.callback_query(F.data == "lottery_done")
async def lottery_done(callback: CallbackQuery) -> None:
    await callback.answer("Игра уже завершена.")


@router.message(Command("exchange"))
async def cmd_exchange(message: Message) -> None:
    if message.chat.type != "private" and message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    balance = await db.get_display_balance(message.from_user.id)
    await message.reply(
        f"💱 Обмен D-COINS\n\n🪙 Твой баланс: {balance} DC\n\nВыбери что хочешь получить:",
        reply_markup=exchange_keyboard(balance),
    )


@router.callback_query(F.data == "buy_dc_menu")
async def buy_dc_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⭐ Покупка D-COINS за звёзды\n\nАктуальные цены указаны на кнопках.\nВыбери пакет:",
        reply_markup=buy_dc_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "buy_dc_back")
async def buy_dc_back(callback: CallbackQuery) -> None:
    balance = await db.get_display_balance(callback.from_user.id)
    await callback.message.edit_text(
        f"💱 Обмен D-COINS\n\n🪙 Твой баланс: {balance:,} DC\n\nВыбери что хочешь получить:".replace(
            ",", " "
        ),
        reply_markup=exchange_keyboard(balance),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("buy_dc_"))
async def buy_dc_package(callback: CallbackQuery, bot: Bot) -> None:
    try:
        amount = int(callback.data.removeprefix("buy_dc_"))
        stars = STAR_DC_PACKAGES[amount]
    except (ValueError, KeyError):
        await callback.answer("Пакет не найден.", show_alert=True)
        return
    payload = await create_invoice_order(db.path, callback.from_user.id, amount, stars)
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"{amount:,} D-COINS",
        description=f"{amount:,} DC за {stars} Stars",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="D-COINS", amount=stars)],
    )
    await callback.answer("Счёт отправлен в личные сообщения")


@router.callback_query(F.data == "exch_chance")
async def exch_chance(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    cost = economy_price("chance_price")
    chance, _, _ = await db.get_user(user_id, MAIN_CHAT_ID)
    if chance >= MAX_CHANCE:
        await callback.answer("Достигнут предел накопленного шанса.", show_alert=True)
        return
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    balance, _ = await db.get_coins(user_id)
    if balance < cost:
        await callback.answer(f"❌ Нужно {cost} DC, реальный баланс: {balance}", show_alert=True)
        return
    if not await db.remove_coins(user_id, cost):
        balance, _ = await db.get_coins(user_id)
        await callback.answer(f"❌ Нужно {cost} DC, реальный баланс: {balance}", show_alert=True)
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    new_chance = min(round(chance + 1.0, 3), MAX_CHANCE)
    name = await db.get_user_name(user_id)
    await db.update_user(user_id, MAIN_CHAT_ID, name, new_chance, msg_count, last_bonus)
    new_balance = await db.get_display_balance(user_id)
    await callback.message.edit_text(
        f"✅ Обменял {cost} DC на +1 балл шанса\n🪙 Баланс: {new_balance} DC\n📈 Новый шанс: {new_chance:.3f} балл.",
        reply_markup=exchange_keyboard(new_balance),
    )
    await callback.answer()


async def process_exchange_gift(
    callback: CallbackQuery, cost: int, gift_key: int, reward_label: str, bot: Bot
) -> None:
    uid = callback.from_user.id
    gift_ids = GIFT_IDS.get(gift_key, [])
    if not gift_ids:
        await callback.answer("Подарок временно недоступен.", show_alert=True)
        return
    if not await db.remove_coins(uid, cost):
        await callback.answer("Недостаточно DC.", show_alert=True)
        return
    await send_bot_gift(bot, uid, random.choice(gift_ids))
    balance = await db.get_display_balance(uid)
    await callback.message.edit_text(
        f"✅ Обменял {cost} DC на подарок {reward_label}\n🪙 Баланс: {balance} DC\n⏳ Подарок поставлен в очередь доставки.",
        reply_markup=exchange_keyboard(balance),
    )
    await callback.answer()


@router.callback_query(F.data == "exch_gift_15")
async def exch_gift_15(callback: CallbackQuery, bot: Bot) -> None:
    await process_exchange_gift(callback, economy_price("gift_15"), 5, "15⭐", bot)


@router.callback_query(F.data == "exch_gift_25")
async def exch_gift_25(callback: CallbackQuery, bot: Bot) -> None:
    await process_exchange_gift(callback, economy_price("gift_25"), 10, "25⭐", bot)


@router.callback_query(F.data == "exch_gift_50")
async def exch_gift_50(callback: CallbackQuery, bot: Bot) -> None:
    await process_exchange_gift(callback, economy_price("gift_50"), 15, "50⭐", bot)


@router.callback_query(F.data == "exch_gift_100")
async def exch_gift_100(callback: CallbackQuery, bot: Bot) -> None:
    await process_exchange_gift(callback, economy_price("gift_100"), 20, "100⭐", bot)


@router.callback_query(F.data == "exch_premium_1m")
async def exch_premium_1m(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    cost = economy_price("premium_1m")
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    if not await db.remove_coins(user_id, cost):
        balance, _ = await db.get_coins(user_id)
        await callback.answer(
            f"❌ Нужно {cost:,} DC, реальный баланс: {balance:,}".replace(",", " "),
            show_alert=True,
        )
        return
    name = await db.get_user_name(user_id)
    try:
        order_id = await db.add_premium_order(user_id, name, cost)
    except Exception:
        await db.add_coins(user_id, cost)
        logger.exception("Could not create Premium order")
        await callback.answer("❌ Не удалось создать заявку. DC возвращены.", show_alert=True)
        return
    new_balance = await db.get_display_balance(user_id)
    await callback.message.edit_text(
        f"✅ Заявка #{order_id} на Premium на месяц создана\n🪙 Списано: {cost:,} DC\n🪙 Баланс: {new_balance:,} DC\n\n💎 Premium будет выдан вручную в ближайшее время.".replace(
            ",", " "
        ),
        reply_markup=exchange_keyboard(new_balance),
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💎 Новая заявка Premium на месяц\n\n#{order_id} | {name} ({user_id})\n🪙 {cost:,} DC\n\nПосле выдачи: premiumdone {order_id}".replace(
                ",", " "
            ),
        )
        await send_log(
            bot,
            f"💎 Обмен на Premium\n\n#{order_id} | {name} ({user_id})\n{cost:,} DC".replace(",", " "),
        )
    except Exception as e:
        logger.warning("Could not notify about Premium order: %s", e)
    await callback.answer("✅ Заявка создана")


def is_duel_chat(chat) -> bool:
    return chat.type in {"group", "supergroup"} and chat.id in {
        MAIN_CHAT_ID,
        CHANNEL_CHAT_ID,
    }


async def resolve_channel_chat(bot: Bot) -> int:
    """Определяет привязанный чат обязательного канала, если ID не задан вручную."""
    global CHANNEL_CHAT_ID
    if CHANNEL_CHAT_ID:
        logger.info("Чат канала для дуэлей: %s", CHANNEL_CHAT_ID)
        return CHANNEL_CHAT_ID
    try:
        channel = await bot.get_chat(REQUIRED_CHANNEL)
        CHANNEL_CHAT_ID = int(getattr(channel, "linked_chat_id", 0) or 0)
        if CHANNEL_CHAT_ID:
            logger.info("Автоматически найден чат канала для дуэлей: %s", CHANNEL_CHAT_ID)
        else:
            logger.warning("У канала %s не найден привязанный чат", REQUIRED_CHANNEL)
    except Exception as error:
        logger.warning("Не удалось определить привязанный чат канала: %s", error)
    return CHANNEL_CHAT_ID


@router.message(Command("duel"))
async def cmd_duel(message: Message) -> None:
    if not is_duel_chat(message.chat):
        return
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        await message.reply("❌ Дуэль нельзя создать от имени канала.")
        return
    user_id = message.from_user.id
    if message.from_user.username:
        await db.set_username(
            user_id,
            message.from_user.username,
            message.chat.id,
            display_name(message.from_user),
        )
    if await db.is_banned(user_id):
        await message.reply(BAN_MESSAGE)
        return
    args = (message.text or "").split()
    if len(args) not in {2, 3}:
        await message.reply(
            f"Использование: дуэль СТАВКА\nИли: дуэль СТАВКА @username\nТакже можно ответить этой командой на сообщение игрока.\nМинимум {DUEL_MIN_BET} DC, максимум {DUEL_MAX_BET:,} DC".replace(
                ",", " "
            )
        )
        return
    try:
        bet = int(args[1].replace(" ", ""))
    except ValueError:
        await message.reply("❌ Ставка должна быть целым числом.")
        return
    if bet < DUEL_MIN_BET or bet > DUEL_MAX_BET:
        await message.reply(
            f"❌ Ставка должна быть от {DUEL_MIN_BET} до {DUEL_MAX_BET:,} DC.".replace(",", " ")
        )
        return
    if user_id in duel_by_user:
        await message.reply("⚔️ У тебя уже есть активная дуэль.")
        return
    if user_id in duel_cooldowns:
        await message.reply(f"⏳ Дуэль доступна раз в {DUEL_COOLDOWN} секунд.")
        return
    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(
            f"❌ Для дуэли нужно {bet:,} DC, реальный баланс: {balance:,} DC.".replace(",", " ")
        )
        return
    target_id = None
    target_name = None
    reply = message.reply_to_message
    if reply and reply.from_user and (not reply.from_user.is_bot) and (not reply.sender_chat):
        if len(args) == 3:
            await message.reply("❌ Выбери один способ: ответ на сообщение или @username.")
            return
        target_id = reply.from_user.id
        target_name = display_name(reply.from_user)
    elif reply and len(args) == 2:
        await message.reply("❌ Нельзя вызвать на дуэль канал или бота.")
        return
    elif len(args) == 3:
        target_username = args[2]
        if not target_username.startswith("@"):
            await message.reply("❌ Укажи игрока через @username или ответь на его сообщение.")
            return
        target = await db.find_user_by_username(target_username)
        if not target:
            await message.reply(
                "❌ Игрок не найден. Пусть он сначала напишет сообщение в основной чат или вызови его ответом на сообщение."
            )
            return
        target_id, target_name = target
    if target_id == user_id:
        await message.reply("❌ Нельзя вызвать на дуэль самого себя.")
        return
    if target_id and target_id in duel_by_user:
        await message.reply("⚔️ У этого игрока уже есть активная дуэль.")
        return
    duel_id = f"{user_id:x}{random.getrandbits(32):08x}"
    while duel_id in active_duels:
        duel_id = f"{user_id:x}{random.getrandbits(32):08x}"
    challenger_name = display_name(message.from_user)
    target_text = (
        f"Вызов для: {target_name}\nТолько этот игрок может принять дуэль."
        if target_id
        else "Кто примет вызов?"
    )
    sent = await message.reply(
        f"⚔️ Вызов на дуэль!\n\nИгрок: {challenger_name}\nСтавка каждого: {bet:,} DC\nБанк победителя: {bet * 2:,} DC\nШанс победы: 50/50\n\n{target_text}".replace(
            ",", " "
        ),
        reply_markup=duel_keyboard(duel_id, bet),
    )
    active_duels[duel_id] = {
        "challenger_id": user_id,
        "challenger_name": challenger_name,
        "opponent_id": None,
        "target_id": target_id,
        "target_name": target_name,
        "bet": bet,
        "chat_id": message.chat.id,
        "message_id": sent.message_id,
        "expires": time.time() + DUEL_TIMEOUT,
        "status": "open",
    }
    duel_by_user[user_id] = duel_id
    duel_cooldowns[user_id] = True


@router.callback_query(F.data.startswith("duel_cancel:"))
async def duel_cancel_callback(callback: CallbackQuery) -> None:
    duel_id = callback.data.removeprefix("duel_cancel:")
    duel = active_duels.get(duel_id)
    if not duel or duel.get("status") != "open":
        await callback.answer("Дуэль уже закрыта.", show_alert=True)
        return
    if callback.from_user.id != duel["challenger_id"]:
        await callback.answer("Отменить дуэль может только её автор.", show_alert=True)
        return
    release_duel(duel_id)
    await callback.message.edit_text(
        f"❌ {duel['challenger_name']} отменил дуэль на {duel['bet']:,} DC.".replace(",", " ")
    )
    await callback.answer("Дуэль отменена")


@router.callback_query(F.data.startswith("duel_accept:"))
async def duel_accept_callback(callback: CallbackQuery, bot: Bot) -> None:
    duel_id = callback.data.removeprefix("duel_accept:")
    duel = active_duels.get(duel_id)
    if not duel or duel.get("status") != "open":
        await callback.answer("Дуэль уже принята или закрыта.", show_alert=True)
        return
    opponent_id = callback.from_user.id
    challenger_id = duel["challenger_id"]
    if opponent_id == challenger_id:
        await callback.answer("Нельзя принять собственную дуэль.", show_alert=True)
        return
    target_id = duel.get("target_id")
    if target_id is not None and opponent_id != target_id:
        await callback.answer(
            f"Эта дуэль предназначена для {duel.get('target_name') or 'другого игрока'}.",
            show_alert=True,
        )
        return
    if opponent_id in duel_by_user:
        await callback.answer("У тебя уже есть активная дуэль.", show_alert=True)
        return
    if opponent_id in duel_cooldowns:
        await callback.answer(f"Следующая дуэль доступна через {DUEL_COOLDOWN} секунд.", show_alert=True)
        return
    if await db.is_banned(opponent_id) or await db.is_banned(challenger_id):
        release_duel(duel_id)
        await callback.message.edit_text("🚫 Дуэль отменена: один из участников заблокирован.")
        await callback.answer("Дуэль отменена", show_alert=True)
        return
    duel["status"] = "processing"
    duel["opponent_id"] = opponent_id
    duel_by_user[opponent_id] = duel_id
    opponent_name = display_name(callback.from_user)
    winner_id = random.choice((challenger_id, opponent_id))
    bet = duel["bet"]
    try:
        settled, challenger_balance, opponent_balance = await db.settle_duel(
            challenger_id, opponent_id, bet, winner_id
        )
    except Exception as e:
        logger.exception("Duel settlement failed: %s", e)
        duel["status"] = "open"
        duel["opponent_id"] = None
        duel_by_user.pop(opponent_id, None)
        await callback.answer("❌ Ошибка проведения дуэли. Попробуй ещё раз.", show_alert=True)
        return
    if not settled:
        duel["status"] = "open"
        duel["opponent_id"] = None
        duel_by_user.pop(opponent_id, None)
        if challenger_balance < bet:
            release_duel(duel_id)
            await callback.message.edit_text(
                f"❌ Дуэль отменена: у {duel['challenger_name']} больше нет {bet:,} DC.".replace(",", " ")
            )
            await callback.answer("У автора недостаточно DC", show_alert=True)
        else:
            await callback.answer(
                f"❌ Для принятия нужно {bet:,} DC, у тебя {opponent_balance:,} DC.".replace(",", " "),
                show_alert=True,
            )
        return
    duel_cooldowns[challenger_id] = True
    duel_cooldowns[opponent_id] = True
    release_duel(duel_id)
    for participant, participant_name in (
        (challenger_id, duel["challenger_name"]),
        (opponent_id, opponent_name),
    ):
        await school_event.record(
            participant,
            participant_name,
            f"duel:{duel_id}",
            {
                "duels": 1,
                "duelwin": int(participant == winner_id),
                "games": 1,
                "gamewin": int(participant == winner_id),
                "bets": bet,
            },
        )
    winner_name = duel["challenger_name"] if winner_id == challenger_id else opponent_name
    loser_name = opponent_name if winner_id == challenger_id else duel["challenger_name"]
    winner_balance = await db.get_display_balance(winner_id)
    await callback.message.edit_text(
        f"⚔️ Дуэль завершена!\n\n{duel['challenger_name']} VS {opponent_name}\nСтавка каждого: {bet:,} DC\n🏆 Победитель: {winner_name}\n💰 Выигрыш: {bet * 2:,} DC\n🪙 Баланс победителя: {winner_balance:,} DC\n\n{loser_name} проиграл ставку.".replace(
            ",", " "
        )
    )
    await send_game_log(
        bot,
        f"⚔️ Дуэль\n{duel['challenger_name']} ({challenger_id}) VS {opponent_name} ({opponent_id})\n💸 Ставка: {bet:,} DC с каждого\n🏆 Победитель: {winner_name} ({winner_id})\n💰 Банк: {bet * 2:,} DC".replace(
            ",", " "
        ),
    )
    await callback.answer(f"🏆 Победил {winner_name}!"[:190])


PRIVATE_PLAIN_COMMANDS = {
    "start",
    "say",
    "channel",
    "mini",
    "vip",
    "unvip",
    "viplist",
    "ban",
    "unban",
    "banlist",
    "addmsgs",
    "removemsgs",
    "addday",
    "removeday",
    "addcoins",
    "removecoins",
    "addvisual",
    "removevisual",
    "createpromo",
    "deletepromo",
    "createcasepromo",
    "promos",
    "balance",
    "popolnit",
    "sendgift",
    "pending",
    "deliver",
    "deletepending",
    "premiumorders",
    "premiumdone",
    "premiumrefund",
    "promo",
    "cases",
    "slots",
    "roulette",
    "dice",
    "mines",
    "scratch",
    "coinflip",
    "lottery",
    "admin",
    "bossdamage",
    "eventdays",
    "goldenhour",
    "broadcast",
    "supportgrant",
    "supportrevoke",
    "supportlist",
    "supportreply",
    "supportclose",
}
GROUP_PLAIN_COMMANDS = {"stats", "top", "winstop", "cointop", "daytop"}
BOT_ARGUMENT_COMMANDS = {
    "start",
    "say",
    "channel",
    "mini",
    "balance",
    "popolnit",
    "sendgift",
    "createpromo",
    "createcasepromo",
    "pending",
    "deliver",
    "premiumdone",
    "premiumrefund",
    "transfer",
    "slots",
    "roulette",
    "dice",
    "scratch",
    "coinflip",
    "lottery",
    "admin",
    "goldenhour",
    "supportgrant",
    "supportrevoke",
    "supportreply",
    "supportclose",
}
PLAIN_COMMAND_HANDLERS = {
    "start": cmd_start,
    "help": cmd_help,
    "say": cmd_say,
    "channel": cmd_channel,
    "mini": cmd_mini,
    "vip": cmd_vip,
    "unvip": cmd_unvip,
    "viplist": cmd_viplist,
    "ban": cmd_ban,
    "unban": cmd_unban,
    "banlist": cmd_banlist,
    "addmsgs": cmd_addmsgs,
    "removemsgs": cmd_removemsgs,
    "addday": cmd_addday,
    "removeday": cmd_removeday,
    "addcoins": cmd_addcoins,
    "removecoins": cmd_removecoins,
    "addvisual": cmd_addvisual,
    "removevisual": cmd_removevisual,
    "createpromo": cmd_createpromo,
    "deletepromo": cmd_deletepromo,
    "createcasepromo": cmd_createcasepromo,
    "promos": cmd_promos,
    "balance": cmd_balance,
    "popolnit": cmd_popolnit,
    "sendgift": cmd_sendgift,
    "pending": cmd_pending,
    "deliver": cmd_deliver,
    "deletepending": cmd_deletepending,
    "premiumorders": cmd_premiumorders,
    "premiumdone": cmd_premiumdone,
    "premiumrefund": cmd_premiumrefund,
    "stats": cmd_stats,
    "top": cmd_top,
    "winstop": cmd_winstop,
    "cointop": cmd_cointop,
    "coins": cmd_coins,
    "promo": cmd_promo,
    "transfer": cmd_transfer,
    "daytop": cmd_daytop,
    "bonus": cmd_bonus,
    "cases": cmd_cases,
    "slots": cmd_slots,
    "roulette": cmd_roulette,
    "dice": cmd_dice,
    "mines": cmd_mines,
    "scratch": cmd_scratch,
    "coinflip": cmd_coinflip,
    "lottery": cmd_lottery,
    "duel": cmd_duel,
    "exchange": cmd_exchange,
    "admin": cmd_admin,
    "bossdamage": cmd_bossdamage,
    "eventdays": cmd_eventdays,
    "goldenhour": cmd_goldenhour,
    "broadcast": cmd_broadcast,
    "supportgrant": cmd_supportgrant,
    "supportrevoke": cmd_supportrevoke,
    "supportlist": cmd_supportlist,
    "supportreply": cmd_supportreply,
    "supportclose": cmd_supportclose,
}


@router.message(is_plain_command)
async def plain_command_handler(message: Message, bot: Bot) -> None:
    if (
        message.chat.type == "private"
        and message.from_user
        and (message.from_user.id != ADMIN_ID)
        and await db.is_support_session_active(message.from_user.id)
    ):
        await support_user_message_handler(message, bot)
        return
    parsed = parse_plain_command(message.text)
    if not parsed:
        return
    command, args = parsed
    if command in PRIVATE_PLAIN_COMMANDS and message.chat.type != "private":
        return
    if command in GROUP_PLAIN_COMMANDS and message.chat.id != MAIN_CHAT_ID:
        return
    if command == "duel" and (not is_duel_chat(message.chat)):
        return
    if command == "bonus" and message.chat.type != "private" and (message.chat.id != MAIN_CHAT_ID):
        return
    command_message = message.model_copy(
        update={"text": "/" + command + (" " + " ".join(args) if args else "")}
    )
    handler = PLAIN_COMMAND_HANDLERS[command]
    if command in BOT_ARGUMENT_COMMANDS:
        await handler(command_message, bot)
    else:
        await handler(command_message)


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID)
async def support_admin_reply_handler(message: Message, bot: Bot) -> None:
    """Администратор отвечает реплаем на любое сообщение диалога."""
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message:
        return
    user_id = await db.get_support_user_by_admin_message(message.reply_to_message.message_id)
    if user_id is None:
        return
    if not await db.has_support_access(user_id):
        await message.answer("❌ Доступ пользователя уже отозван.")
        return
    if not await db.is_support_session_active(user_id):
        await message.answer("❌ Пользователь уже закрыл диалог.")
        return
    try:
        await bot.send_message(user_id, "💬 Ответ администратора:", reply_markup=support_chat_keyboard())
        await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)
        await db.link_support_message(message.message_id, user_id)
        await message.answer("✅ Ответ отправлен пользователю.")
    except Exception:
        logger.exception("Operation failed")
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(F.chat.type == "private")
async def support_user_message_handler(message: Message, bot: Bot) -> None:
    """Пересылает все типы сообщений активного разрешённого диалога админу."""
    if not message.from_user or message.from_user.id == ADMIN_ID or message.from_user.is_bot:
        return
    user_id = message.from_user.id
    if not await db.has_support_access(user_id):
        return
    if not await db.is_support_session_active(user_id):
        return
    if await db.is_banned(user_id):
        return
    username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    try:
        header = await bot.send_message(
            ADMIN_ID,
            f"💬 Сообщение из диалога\n\n👤 {display_name(message.from_user)}\n🔗 {username}\n🆔 {user_id}\n\nОтветь реплаем на это сообщение или на сообщение ниже.",
        )
        copied = await bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await db.link_support_message(header.message_id, user_id)
        await db.link_support_message(copied.message_id, user_id)
        await message.answer(
            "✅ Сообщение доставлено администратору.",
            reply_markup=support_chat_keyboard(),
        )
    except Exception:
        logger.exception("Support relay failed for %s", user_id)
        await message.answer("❌ Не удалось выполнить запрос. Ошибка записана в журнал.")


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_handler(message: Message, bot: Bot) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        if message.chat.id == CHANNEL_CHAT_ID:
            msg_text = message.text or message.caption
            if (
                msg_text
                and (not msg_text.startswith("/"))
                and message.from_user
                and (not message.from_user.is_bot)
                and (not message.sender_chat)
            ):
                user_id = message.from_user.id
                name = display_name(message.from_user)
                if await db.is_banned(user_id):
                    return
                if message.from_user.username:
                    await db.set_username(user_id, message.from_user.username, message.chat.id, name)
                await school_event.record(
                    user_id,
                    name,
                    f"message:{message.chat.id}:{message.message_id}",
                    {"messages": 1},
                )
            return
        if not CHANNEL_CHAT_ID:
            return
        if message.chat.id not in {LOG_CHAT_ID, GAME_LOG_CHAT_ID}:
            try:
                await bot.leave_chat(message.chat.id)
            except Exception as e:
                logger.warning("leave_chat failed: %s", e)
        return
    msg_text = message.text or message.caption
    if not msg_text or msg_text.startswith("/"):
        return
    if message.sender_chat:
        user_id = message.sender_chat.id
        name = message.sender_chat.title or str(message.sender_chat.id)
    elif message.from_user and (not message.from_user.is_bot):
        user_id = message.from_user.id
        name = display_name(message.from_user)
    else:
        return
    if not message.sender_chat and message.from_user:
        await db.set_username(user_id, message.from_user.username, message.chat.id, name)
    cache_key = (user_id, message.chat.id)
    if await db.is_banned(user_id):
        return
    if cache_key in cooldowns:
        return
    cooldowns[cache_key] = True
    await db.increment_daily(user_id, message.chat.id, name)
    if message.sender_chat:
        chance, count, last_bonus = await db.get_user(user_id, message.chat.id)
        await db.update_user(user_id, message.chat.id, name, chance, count + 1, last_bonus)
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, message.chat.id)
    is_vip = await db.is_vip(user_id)
    if not message.sender_chat and message.from_user and (not message.from_user.is_bot):
        await school_event.record(
            user_id,
            name,
            f"message:{message.chat.id}:{message.message_id}",
            {"messages": 1},
        )
    coins_earned = COINS_VIP_PER_MSG if is_vip else COINS_PER_MSG
    await db.add_coins(user_id, coins_earned)
    chance_step = 0.003 if is_vip else 0.002
    if msg_count < 150:
        is_win = False
    else:
        is_win = gift_roll_wins(chance)
    if is_win:
        await db.add_win(user_id, message.chat.id, name, chance)
        await send_bot_gift(bot, user_id, random.choice(WIN_GIFT_IDS))
        await db.update_user(user_id, message.chat.id, name, START_CHANCE, 0, last_bonus)
        await queue_notice(
            db.path,
            message.chat.id,
            f"🏆 Поздравляем, {name}!\n📈 Накоплено: {chance:.3f} балл.\n🎁 Подарок поставлен в очередь доставки.",
        )
        await queue_notice(db.path, ADMIN_ID, f"🏆 Победитель: {name} ({user_id}). Подарок в очереди.")
        await send_log(bot, f"🏆 Победитель: {name} ({user_id}); накоплено {chance:.3f} балл.")
    else:
        new_chance = min(round(chance + chance_step, 3), MAX_CHANCE)
        await db.update_user(user_id, message.chat.id, name, new_chance, msg_count + 1, last_bonus)


async def daily_reset_task(bot: Bot) -> None:
    await db.clear_old_daily()
    await housekeeping(db.path)


# ==================== RUNTIME ====================


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
        if not user or user.is_bot:
            if isinstance(event, Message) and event.sender_chat:
                return await group_handler(event, data["bot"])
            return None
        if isinstance(event, Message) and (event.text or "").split(maxsplit=1)[0:1] == ["/paysupport"]:
            return await payments_support(event)
        if isinstance(event, CallbackQuery) and (not isinstance(event.message, Message)):
            await event.answer("Сообщение недоступно. Откройте меню заново.")
            return None
        if isinstance(event, CallbackQuery) and (event.data or "").startswith(("admin_", "sca:")):
            if user.id != ADMIN_ID or event.message.chat.type != "private":
                await event.answer("Нет доступа", show_alert=True)
                return None
        if user.id != ADMIN_ID and await db.is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer(BAN_MESSAGE, show_alert=True)
            return None
        if isinstance(event, Message):
            parts = (event.text or "").split()
            if any(
                (
                    p.lstrip("-+").isdecimal() and (len(p.lstrip("-+")) > 15 or not p.lstrip("-+").isascii())
                    for p in parts
                )
            ):
                await event.answer("Число слишком велико или содержит неподдерживаемые цифры.")
                return None
            await db.set_username(user.id, user.username, MAIN_CHAT_ID, display_name(user))
            if event.text and event.text.split()[0].lower() in {
                "/cancel",
                "cancel",
                "отмена",
            }:
                pending_game_bets.pop(user.id, None)
                await db.set_support_session(user.id, False)
                await event.answer("Ввод отменён. Оплаченная игра остаётся доступной до её завершения.")
                return None
        private = (
            event.message.chat.type == "private"
            if isinstance(event, CallbackQuery)
            else event.chat.type == "private"
        )
        support = isinstance(event, Message) and (event.text or "").startswith(
            ("/paysupport", "/help", "/start")
        )
        if private and user.id != ADMIN_ID and (not support):
            if not await is_channel_subscriber(data["bot"], user.id):
                message = event.message if isinstance(event, CallbackQuery) else event
                await send_subscription_prompt(message)
                return None
        if (
            isinstance(event, Message)
            and private
            and (user.id != ADMIN_ID)
            and (not (event.text or "").startswith("/"))
            and await db.is_support_session_active(user.id)
        ):
            return await support_user_message_handler(event, data["bot"])
        return await handler(event, data)


async def payments_support(message: Message):
    await message.answer(
        f"По вопросам оплаты обратитесь к администратору: tg://user?id={ADMIN_ID}. Сохраните квитанцию Telegram и идентификатор платежа."
    )


async def ingest(bot, store, stop):
    offset = None
    while not stop.is_set():
        try:
            updates = await bot(
                GetUpdates(
                    offset=offset,
                    timeout=20,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                ),
                request_timeout=25,
            )
            normal = []
            for update in updates:
                if update.pre_checkout_query:
                    try:
                        await validate_checkout(update.pre_checkout_query.as_(bot), store.path)
                    except Exception:
                        logger.exception("Pre-checkout validation failed")
                else:
                    normal.append(update)
                if update.callback_query:
                    try:
                        await update.callback_query.as_(bot).answer()
                    except Exception:
                        logger.info("Early callback acknowledgement failed")
            await store.enqueue(normal)
            if updates:
                offset = updates[-1].update_id + 1
        except TelegramConflictError:
            # Второй процесс с тем же токеном может кратковременно появиться во
            # время Railway redeploy. Не останавливаем весь бот: ждём и снова
            # пытаемся забрать polling.
            logger.error("Another poller uses this token; retrying in 10 seconds")
            await asyncio.sleep(10)
        except (TelegramNetworkError, TelegramRetryAfter) as exc:
            await asyncio.sleep(getattr(exc, "retry_after", 3))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Polling failed; retrying in 5 seconds")
            await asyncio.sleep(5)


async def process_one(dp, bot, store):
    async with aiosqlite.connect(store.inbox_path) as c:
        async with c.execute(
            "SELECT i.id,i.payload,i.attempts,s.seed FROM inbox i JOIN action_randomness s ON s.id=i.id WHERE i.status='pending' ORDER BY i.id LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return False
    rng_token = begin_draw(row[3])
    try:
        async with store.transaction(f"update:{row[0]}") as c:
            async with c.execute("SELECT 1 FROM processed_updates WHERE id=?", (row[0],)) as cur:
                processed = await cur.fetchone()
            if not processed:
                await dp.feed_update(bot, Update.model_validate_json(row[1], context={"bot": bot}))
                await c.execute("INSERT INTO processed_updates VALUES (?,?)", (row[0], time.time()))
        async with aiosqlite.connect(store.inbox_path) as c:
            await c.execute("UPDATE inbox SET status='done',error=NULL WHERE id=?", (row[0],))
            await c.commit()
    except Exception as exc:
        logger.exception("Update %s failed", row[0])
        async with aiosqlite.connect(store.inbox_path) as c:
            status = "failed" if row[2] >= 2 else "pending"
            await c.execute(
                "UPDATE inbox SET attempts=attempts+1,status=?,error=? WHERE id=?",
                (status, type(exc).__name__, row[0]),
            )
            await c.commit()
        if status == "failed":
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"Обновление #{row[0]} требует проверки: {type(exc).__name__}. /runtimeissues",
                )
            except Exception:
                logger.exception("Cannot report failed update")
        await asyncio.sleep(1)
    finally:
        current_rng.reset(rng_token)
    return True


async def consume(dp, bot, store):
    while True:
        if not await process_one(dp, bot, store):
            await asyncio.sleep(0.2)


async def periodic(bot, store, function, interval):
    while True:
        try:
            async with store.transaction(f"job:{function.__name__}:{time.time_ns()}"):
                await function(bot)
        except Exception:
            logger.exception("Periodic task %s failed", function.__name__)
        await asyncio.sleep(interval)


async def background_periodic(bot, function, interval):
    """Безопасный запуск служебных задач без блокировки обработки команд."""
    while True:
        try:
            await function(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background task %s failed", function.__name__)
        await asyncio.sleep(interval)


async def supervise(name, factory, stop):
    """Не даёт одной фоновой задаче выключить весь бот."""
    while not stop.is_set():
        try:
            await factory()
            if not stop.is_set():
                logger.error("Worker %s stopped unexpectedly; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker %s crashed; restarting", name)
        if not stop.is_set():
            await asyncio.sleep(3)


async def runtime_issues(message: Message):
    if message.from_user.id != ADMIN_ID or message.chat.type != "private":
        return
    async with connect(db.path) as c:
        lines = ["Очереди, требующие внимания (до 50 записей):"]
        for sql, label in [
            (
                "SELECT id,status,error FROM gift_jobs WHERE status IN ('unknown','failed') LIMIT 50",
                "Подарок",
            ),
            (
                "SELECT id,status,attempts FROM notices WHERE status='failed' LIMIT 50",
                "Уведомление",
            ),
            ("SELECT charge,uid,created FROM payment_review LIMIT 50", "Платёж"),
        ]:
            async with c.execute(sql) as cursor:
                lines.extend((f"{label}: {tuple(row)}" for row in await cursor.fetchall()))
    async with aiosqlite.connect(str(db.path) + ".inbox") as c:
        async with c.execute("SELECT id,status,error FROM inbox WHERE status='failed' LIMIT 50") as cursor:
            lines.extend((f"Update: {tuple(row)}" for row in await cursor.fetchall()))
    await message.answer("\n".join(lines))


async def resolve_gift(message: Message):
    if message.from_user.id != ADMIN_ID or message.chat.type != "private":
        return
    parts = (message.text or "").split()
    if (
        len(parts) != 3
        or not parts[1].isascii()
        or (not parts[1].isdigit())
        or (parts[2] not in {"sent", "retry", "cancel"})
    ):
        await message.answer(
            "После сверки выдачи: /giftresolve ID sent|retry|cancel. retry допустим только если подтверждено, что подарок НЕ отправлен."
        )
        return
    status = {"sent": "sent", "retry": "pending", "cancel": "cancelled"}[parts[2]]
    async with connect(db.path) as c:
        cursor = await c.execute(
            "UPDATE gift_jobs SET status=?,error=NULL WHERE id=? AND status IN ('unknown','failed')",
            (status, int(parts[1])),
        )
        await c.commit()
    await message.answer(
        "Статус обновлён." if cursor.rowcount else "Нет заявки с таким ID в состоянии failed/unknown."
    )


async def main():
    validate_config()
    Path(db.path).resolve().parent.mkdir(parents=True, exist_ok=True)
    await db.init()
    await school_event.init()
    await load_runtime_settings()
    bot = Bot(TOKEN, session=AiohttpSession(timeout=10))
    bot.session.middleware(TelegramTransport())
    dp = Dispatcher(disable_fsm=True)
    dp.message.register(payments_support, Command("paysupport"))
    dp.message.register(runtime_issues, Command("runtimeissues"))
    dp.message.register(resolve_gift, Command("giftresolve"))
    dp.include_router(router)
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    background = []
    try:
        info = await bot.get_webhook_info()
        if info.url:
            logging.warning("Активен webhook %s; отключаю его для polling.", info.url)
            await bot.delete_webhook(drop_pending_updates=False)
        await resolve_channel_chat(bot)
        background = [
            asyncio.create_task(gift_worker(bot, db.path), name="gifts"),
            asyncio.create_task(notice_worker(bot, db.path), name="notices"),
        ]
        for function, interval in (
            (casino_timeout_checker, 5),
            (duel_timeout_checker, 5),
            (school_notifications, 5),
            (bonus_notification_worker, 2),
            (mini_event_task, 5),
            (golden_hour_task, 15),
            (jackpot_draw_task, 30),
            (daily_reset_task, 3600),
        ):
            background.append(
                asyncio.create_task(
                    background_periodic(bot, function, interval), name=function.__name__
                )
            )
        logger.info("Bot started with standard Telegram polling")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await dp.storage.close()
        await bot.session.close()


# ==================== MAINTENANCE ====================


def maintenance_main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["backup", "check", "retry-update"])
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    path = Path(os.getenv("DB_PATH", "activity.db")).resolve()
    if not path.is_file():
        parser.error("Database does not exist")
    with FileLock(str(path) + ".polling.lock", timeout=0):
        with sqlite3.connect(path) as source:
            if args.command == "backup":
                if not args.value:
                    parser.error("Specify a new backup filename")
                target = Path(args.value).resolve()
                if target.exists() or Path(str(target) + ".inbox").exists():
                    parser.error("Backup destination already exists")
                target.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(target) as destination:
                    source.backup(destination)
                inbox = Path(str(path) + ".inbox")
                if inbox.is_file():
                    with (
                        sqlite3.connect(inbox) as receipt,
                        sqlite3.connect(str(target) + ".inbox") as destination,
                    ):
                        receipt.backup(destination)
                print(target)
            elif args.command == "check":
                print(source.execute("PRAGMA integrity_check").fetchone()[0])
                print(
                    "Invalid balances:",
                    source.execute(
                        "SELECT COUNT(*) FROM coins WHERE balance IS NULL OR balance<0 OR typeof(balance)<>'integer'"
                    ).fetchone()[0],
                )
            else:
                if not args.value or not args.value.isascii() or (not args.value.isdigit()):
                    parser.error("Specify numeric update ID from /runtimeissues")
                inbox = Path(str(path) + ".inbox")
                if not inbox.is_file():
                    parser.error("Receipt database does not exist")
                with sqlite3.connect(inbox) as receipt:
                    cursor = receipt.execute(
                        "UPDATE inbox SET status='pending',attempts=0,error=NULL WHERE id=? AND status='failed'",
                        (int(args.value),),
                    )
                    print("Queued:", cursor.rowcount)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    if len(sys.argv) > 1:
        maintenance_main()
    else:
        asyncio.run(main())
