import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from math import comb

import aiosqlite
import pytz
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from cachetools import TTLCache
from dotenv import load_dotenv
import os

# =========================
# CONFIG
# =========================

load_dotenv()

TOKEN        = os.getenv("BOT_TOKEN", "")
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))
LOG_CHAT_ID  = int(os.getenv("LOG_CHAT_ID", "0"))
GAME_LOG_CHAT_ID = int(os.getenv("GAME_LOG_CHAT_ID", "0"))
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
# Обязательная подписка перед открытием меню бота.
REQUIRED_CHANNEL = "@d_coins_channel"
REQUIRED_CHANNEL_URL = "https://t.me/d_coins_channel"
# Можно переопределить отдельным ID/username в Railway, если промокоды нужны в другом канале.
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", REQUIRED_CHANNEL)

COOLDOWN_SECONDS   = 1
START_CHANCE       = 0.1
STEP               = 0.002
MAX_CHANCE         = 100.0
# Розыгрыш подарка за общение: диапазон 0..2000 вдвое реже прежнего 0..1000.
# Накопленный и купленный шанс игроков при этом сохраняется.
GIFT_WIN_ROLL_MAX  = 2000.0
BONUS_COOLDOWN     = 43200

# D-COINS
COINS_START        = 10
COINS_PER_MSG      = 1
COINS_VIP_PER_MSG  = 2
COINS_BONUS        = 20
COINS_VIP_BONUS    = 30
COINS_BONUS_CD     = 43200

# Казино
CASINO_MIN_BET     = 5
CASINO_TIMEOUT     = 300  # 5 минут
CASINO_BET_COOLDOWN = 10  # секунд между ставками одного пользователя
CASE_OPEN_COOLDOWN = 5    # секунд между открытиями кейса

# Дуэли в основном чате
DUEL_MIN_BET       = 5
DUEL_MAX_BET       = 100_000
DUEL_COOLDOWN      = 10
DUEL_TIMEOUT       = 300

# Обмен
EXCHANGE_CHANCE    = 5000   # 5 000 DC = +1% шанса
EXCHANGE_GIFT_15   = 40000
EXCHANGE_GIFT_25   = 70000
EXCHANGE_GIFT_50   = 140000
EXCHANGE_GIFT_100  = 280000
EXCHANGE_PREMIUM_1_MONTH = 500000

# Покупка DC за Telegram Stars: 100 000 DC = 200⭐, до 1 000 000 DC.
STAR_DC_PACKAGES = {amount: (amount // 100_000) * 200 for amount in range(100_000, 1_000_001, 100_000)}

# Кейсы
CASES = {
    "blood": {
        "title": "BLOOD",
        "price": 5000,
        "rewards": [
            ("coins", 300, 10), ("coins", 500, 12), ("coins", 700, 14),
            ("coins", 1000, 16), ("coins", 1500, 17), ("coins", 2500, 14),
            ("coins", 4000, 8), ("coins", 7000, 6),
            ("gift", 15, 2), ("gift", 25, 0.7), ("gift", 50, 0.3),
        ],
    },
    "pantera": {
        "title": "PANTERA",
        "price": 10000,
        "rewards": [
            ("coins", 1000, 16), ("coins", 5000, 21), ("coins", 6000, 21),
            ("coins", 8000, 18), ("coins", 9000, 13), ("coins", 12000, 6),
            ("coins", 15000, 3), ("gift", 15, 1), ("gift", 25, 0.5),
            ("gift", 50, 0.3), ("gift", 100, 0.2),
        ],
    },
    "spider_man": {
        "title": "SPIDER MAN",
        "price": 7500,
        "rewards": [
            ("coins", 500, 12), ("coins", 1000, 18), ("coins", 1500, 20),
            ("coins", 2500, 20), ("coins", 3500, 15), ("coins", 5000, 9),
            ("coins", 7500, 4), ("coins", 10000, 1), ("coins", 15000, 0.2),
            ("gift", 15, 0.6), ("gift", 25, 0.15), ("gift", 50, 0.05),
        ],
    },
    "school": {
        "title": "ШКОЛЬНЫЙ",
        "price": 3000,
        "rewards": [
            ("coins", 250, 22.3), ("coins", 500, 25.25),
            ("coins", 1000, 24), ("coins", 1500, 15),
            ("coins", 2500, 8), ("coins", 4000, 3.5),
            ("coins", 7000, 1.7), ("gift", 15, 0.2),
            ("gift", 25, 0.04), ("gift", 50, 0.01),
        ],
    },
    "student": {
        "title": "СТУДЕНЧЕСКИЙ",
        "price": 12000,
        "rewards": [
            ("coins", 1000, 22.4), ("coins", 3000, 22.3),
            ("coins", 5000, 20), ("coins", 7500, 15),
            ("coins", 10000, 10), ("coins", 15000, 6),
            ("coins", 25000, 2.5), ("coins", 40000, 1.4),
            ("gift", 15, 0.3), ("gift", 25, 0.07),
            ("gift", 50, 0.02), ("gift", 100, 0.01),
        ],
    },
    "excellent": {
        "title": "КЕЙС ОТЛИЧНИКА",
        "price": None,
        "key_only": True,
        "rewards": [
            ("coins", 3000, 18), ("coins", 8000, 20),
            ("coins", 12000, 20), ("coins", 20000, 15),
            ("coins", 30000, 10), ("coins", 45000, 7),
            ("coins", 70000, 4), ("coins", 100000, 2),
            ("gift", 15, 2), ("gift", 25, 1),
            ("gift", 50, 0.6), ("gift", 100, 0.4),
        ],
    },
}

# Изменяемые настройки экономики. Значения загружаются из SQLite при старте,
# поэтому правки из админ-панели переживают перезапуск и новый деплой.
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

# Наборы ID Telegram-подарков для кейсов и обменов.
GIFT_IDS = {
    5:  ["5170145012310081615", "5170233102089322756"],
    10: ["5170250947678437525", "5168103777563050263"],
    15: ["5170144170496491616", "5170314324215857265",
         "5170564780938756245", "6028601630662853006"],
    20: ["5168043875654172773", "5170690322832818290",
         "5170521118301225164"],
}

WIN_GIFT_IDS = [
    "5170233102089322756",
    "5170233102089322756",  # <- вставь второй ID подарка
]

# Активные игры казино: user_id -> {"game": str, "bet": int, "data": dict, "expires": float}
active_games: dict = {}
# Активные дуэли: duel_id -> данные дуэли; пользователь может находиться только в одной.
active_duels: dict[str, dict] = {}
duel_by_user: dict[int, str] = {}

logger = logging.getLogger(__name__)

# =========================
# DATABASE
# =========================

class Database:

    def __init__(self, path: str = "activity.db"):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id    INTEGER,
                    chat_id    INTEGER,
                    user_name  TEXT,
                    chance     REAL    DEFAULT 0.1,
                    msg_count  INTEGER DEFAULT 0,
                    last_bonus REAL    DEFAULT 0,
                    PRIMARY KEY (user_id, chat_id)
                )
            """)
            # Username нужен для /transfer @username.
            # Добавляем колонку безопасно для уже существующей базы.
            async with db.execute("PRAGMA table_info(user_stats)") as cur:
                columns = [row[1] for row in await cur.fetchall()]
            if "username" not in columns:
                await db.execute("ALTER TABLE user_stats ADD COLUMN username TEXT")

            # Реферальная программа отключена: удаляем её устаревшие данные
            # при первом запуске обновлённой версии.
            await db.execute("DROP TABLE IF EXISTS referrals")
            await db.execute("DROP TABLE IF EXISTS invite_links")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS wins (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    chat_id    INTEGER NOT NULL,
                    user_name  TEXT,
                    chance     REAL,
                    won_at     REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_gifts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    user_name   TEXT,
                    gift_id     TEXT NOT NULL,
                    reason      TEXT,
                    created_at  REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS premium_orders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    user_name   TEXT,
                    cost        INTEGER NOT NULL,
                    created_at  REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS star_coin_purchases (
                    payment_charge_id TEXT PRIMARY KEY,
                    user_id           INTEGER NOT NULL,
                    dc_amount         INTEGER NOT NULL,
                    star_amount       INTEGER NOT NULL,
                    created_at        REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vip_users (
                    user_id INTEGER PRIMARY KEY
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id   INTEGER PRIMARY KEY,
                    reason    TEXT,
                    banned_at REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    user_id   INTEGER,
                    chat_id   INTEGER,
                    user_name TEXT,
                    date      TEXT NOT NULL,
                    msg_count INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, chat_id, date)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS coins (
                    user_id         INTEGER PRIMARY KEY,
                    balance         INTEGER DEFAULT 10,
                    last_coin_bonus REAL    DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code       TEXT PRIMARY KEY COLLATE NOCASE,
                    reward     INTEGER NOT NULL,
                    reward_type TEXT NOT NULL DEFAULT 'coins',
                    case_id    TEXT,
                    case_count INTEGER,
                    max_uses   INTEGER,
                    uses       INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS promo_activations (
                    code       TEXT NOT NULL COLLATE NOCASE,
                    user_id    INTEGER NOT NULL,
                    activated_at REAL NOT NULL,
                    PRIMARY KEY (code, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS case_keys (
                    user_id  INTEGER NOT NULL,
                    case_id  TEXT NOT NULL,
                    amount   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, case_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id    INTEGER PRIMARY KEY,
                    user_name  TEXT,
                    started_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bonus_broadcasts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount     INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bonus_deliveries (
                    broadcast_id INTEGER NOT NULL,
                    user_id      INTEGER NOT NULL,
                    status       INTEGER NOT NULL DEFAULT 0,
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    error        TEXT,
                    PRIMARY KEY (broadcast_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key        TEXT PRIMARY KEY,
                    value      REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            async with db.execute("PRAGMA table_info(promo_codes)") as cur:
                promo_columns = [row[1] for row in await cur.fetchall()]
            if "reward_type" not in promo_columns:
                await db.execute("ALTER TABLE promo_codes ADD COLUMN reward_type TEXT NOT NULL DEFAULT 'coins'")
            if "case_id" not in promo_columns:
                await db.execute("ALTER TABLE promo_codes ADD COLUMN case_id TEXT")
            if "case_count" not in promo_columns:
                await db.execute("ALTER TABLE promo_codes ADD COLUMN case_count INTEGER")
            # Удалённый кейс KARAPUZ больше нельзя открыть или получить по промокоду.
            await db.execute(
                "DELETE FROM promo_activations WHERE code IN "
                "(SELECT code FROM promo_codes WHERE case_id='karapuz')"
            )
            await db.execute("DELETE FROM promo_codes WHERE case_id='karapuz'")
            await db.execute("DELETE FROM case_keys WHERE case_id='karapuz'")
            await db.execute("DELETE FROM app_settings WHERE key LIKE 'case_chance:karapuz:%'")
            # До появления bot_users точного списка /start не было. Один раз переносим
            # известных владельцев баланса; дальше новые пользователи пишутся по /start.
            await db.execute(
                "INSERT OR IGNORE INTO bot_users(user_id,user_name,started_at) "
                "SELECT c.user_id, COALESCE((SELECT u.user_name FROM user_stats u "
                "WHERE u.user_id=c.user_id ORDER BY u.chat_id LIMIT 1), CAST(c.user_id AS TEXT)), ? "
                "FROM coins c",
                (time.time(),),
            )
            await db.commit()

    # --------------------------------------------------
    # BAN
    # --------------------------------------------------

    async def ban_user(self, user_id: int, reason: str = "") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?)",
                (user_id, reason, time.time())
            )
            await db.commit()

    async def unban_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
            await db.commit()

    async def is_banned(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)
            ) as cur:
                return await cur.fetchone() is not None

    async def get_ban_list(self) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT u.user_id, COALESCE(s.user_name, CAST(u.user_id AS TEXT)), u.reason "
                "FROM banned_users u "
                "LEFT JOIN user_stats s ON s.user_id = u.user_id AND s.chat_id = ?",
                (MAIN_CHAT_ID,)
            ) as cur:
                return await cur.fetchall()

    # --------------------------------------------------
    # PENDING GIFTS
    # --------------------------------------------------

    async def add_pending_gift(self, user_id: int, user_name: str, gift_id: str, reason: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO pending_gifts (user_id, user_name, gift_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_name, gift_id, reason, time.time())
            )
            await db.commit()

    async def get_pending_gifts(self) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id, user_id, user_name, gift_id, reason, created_at FROM pending_gifts ORDER BY created_at ASC"
            ) as cur:
                return await cur.fetchall()

    async def remove_pending_gift(self, gift_db_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM pending_gifts WHERE id=?", (gift_db_id,))
            await db.commit()

    # --------------------------------------------------
    # PREMIUM ORDERS
    # --------------------------------------------------

    async def add_premium_order(self, user_id: int, user_name: str, cost: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO premium_orders (user_id, user_name, cost, created_at) VALUES (?, ?, ?, ?)",
                (user_id, user_name, cost, time.time()),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_premium_orders(self) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id, user_id, user_name, cost, created_at FROM premium_orders ORDER BY created_at ASC"
            ) as cur:
                return await cur.fetchall()

    async def remove_premium_order(self, order_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM premium_orders WHERE id=?", (order_id,))
            await db.commit()

    async def credit_star_coin_purchase(
        self, payment_charge_id: str, user_id: int, dc_amount: int, star_amount: int
    ) -> tuple[bool, int]:
        """Зачисляет покупку один раз; повторный платёж Telegram не дублирует DC."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO star_coin_purchases "
                    "(payment_charge_id, user_id, dc_amount, star_amount, created_at) VALUES (?, ?, ?, ?, ?)",
                    (payment_charge_id, user_id, dc_amount, star_amount, time.time()),
                )
                if cursor.rowcount != 1:
                    async with db.execute("SELECT balance FROM coins WHERE user_id=?", (user_id,)) as cur:
                        row = await cur.fetchone()
                    await db.rollback()
                    return False, row[0] if row else COINS_START
                await db.execute(
                    "INSERT INTO coins (user_id, balance) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
                    (user_id, COINS_START + dc_amount, dc_amount),
                )
                async with db.execute("SELECT balance FROM coins WHERE user_id=?", (user_id,)) as cur:
                    row = await cur.fetchone()
                await db.commit()
                return True, row[0]
            except Exception:
                await db.rollback()
                raise

    async def get_settings(self) -> dict[str, float]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT key, value FROM app_settings") as cur:
                return {key: float(value) for key, value in await cur.fetchall()}

    async def set_setting(self, key: str, value: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, float(value), time.time()),
            )
            await db.commit()

    # --------------------------------------------------
    # USER STATS
    # --------------------------------------------------

    async def get_user(self, user_id: int, chat_id: int) -> tuple:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT chance, msg_count, last_bonus FROM user_stats WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
            ) as cur:
                row = await cur.fetchone()
        return row if row else (START_CHANCE, 0, 0.0)

    async def update_user(self, user_id: int, chat_id: int, user_name: str, chance: float, msg_count: int, last_bonus: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO user_stats (user_id, chat_id, user_name, chance, msg_count, last_bonus)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    user_name  = excluded.user_name,
                    chance     = excluded.chance,
                    msg_count  = excluded.msg_count,
                    last_bonus = excluded.last_bonus
            """, (user_id, chat_id, user_name, chance, msg_count, last_bonus))
            await db.commit()

    async def get_user_name(self, user_id: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT user_name FROM user_stats WHERE user_id=? AND chat_id=?",
                (user_id, MAIN_CHAT_ID)
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
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO user_stats (user_id, chat_id, user_name, username)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    username = excluded.username,
                    user_name = COALESCE(excluded.user_name, user_stats.user_name)
                """,
                (user_id, chat_id, user_name or str(user_id), normalized)
            )
            await db.commit()

    async def find_user_by_username(self, username: str):
        username = username.lower().lstrip("@")
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT user_id, user_name FROM user_stats WHERE username=? AND chat_id=? LIMIT 1",
                (username, MAIN_CHAT_ID)
            ) as cur:
                return await cur.fetchone()

    async def transfer_coins(self, sender_id: int, recipient_id: int, amount: int):
        if amount <= 0 or sender_id == recipient_id:
            return False, None, None

        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Создаём баланс с начальным количеством, если пользователь ещё не получал монеты.
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (sender_id, COINS_START)
                )
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (recipient_id, COINS_START)
                )

                cur = await db.execute(
                    "UPDATE coins SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (amount, sender_id, amount)
                )
                if cur.rowcount != 1:
                    await db.rollback()
                    return False, None, None

                await db.execute(
                    "UPDATE coins SET balance=balance+? WHERE user_id=?",
                    (amount, recipient_id)
                )
                await db.commit()

                async with db.execute("SELECT balance FROM coins WHERE user_id=?", (sender_id,)) as cur:
                    sender_row = await cur.fetchone()
                async with db.execute("SELECT balance FROM coins WHERE user_id=?", (recipient_id,)) as cur:
                    recipient_row = await cur.fetchone()
                return True, sender_row[0], recipient_row[0]
            except Exception:
                await db.rollback()
                raise

    async def get_top(self, chat_id: int, limit: int = 5) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT user_name, chance, msg_count FROM user_stats WHERE chat_id=? ORDER BY chance DESC LIMIT ?",
                (chat_id, limit)
            ) as cur:
                return await cur.fetchall()

    # --------------------------------------------------
    # WINS
    # --------------------------------------------------

    async def add_win(self, user_id: int, chat_id: int, user_name: str, chance: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO wins (user_id, chat_id, user_name, chance, won_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, chat_id, user_name, chance, time.time())
            )
            await db.commit()

    async def get_wins_count(self, user_id: int, chat_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM wins WHERE user_id=? AND chat_id=?", (user_id, chat_id)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def get_wins_top(self, chat_id: int, limit: int = 10) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT user_name, COUNT(*) as cnt FROM wins WHERE chat_id=? GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
                (chat_id, limit)
            ) as cur:
                return await cur.fetchall()

    # --------------------------------------------------
    # VIP
    # --------------------------------------------------

    async def set_vip(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO vip_users (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def remove_vip(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM vip_users WHERE user_id=?", (user_id,))
            await db.commit()

    async def is_vip(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT 1 FROM vip_users WHERE user_id=?", (user_id,)) as cur:
                return await cur.fetchone() is not None

    # --------------------------------------------------
    # DAILY STATS
    # --------------------------------------------------

    async def increment_daily(self, user_id: int, chat_id: int, user_name: str) -> None:
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO daily_stats (user_id, chat_id, user_name, date, msg_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(user_id, chat_id, date) DO UPDATE SET
                    user_name = excluded.user_name,
                    msg_count = msg_count + 1
            """, (user_id, chat_id, user_name, today))
            await db.commit()

    async def get_daily_top(self, chat_id: int, limit: int = 10) -> list:
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT user_name, msg_count FROM daily_stats "
                "WHERE chat_id=? AND date=? ORDER BY msg_count DESC LIMIT ?",
                (chat_id, today, limit)
            ) as cur:
                return await cur.fetchall()

    async def clear_old_daily(self) -> None:
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM daily_stats WHERE date < ?", (today,))
            await db.commit()

    async def add_day_messages(self, user_id: int, chat_id: int, user_name: str, amount: int):
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO daily_stats (user_id, chat_id, user_name, date, msg_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id, date)
                DO UPDATE SET msg_count = msg_count + ?, user_name = excluded.user_name
            """, (user_id, chat_id, user_name, today, amount, amount))
            await db.commit()

    async def remove_day_messages(self, user_id: int, chat_id: int, amount: int):
        today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                UPDATE daily_stats SET msg_count = MAX(msg_count - ?, 0)
                WHERE user_id=? AND chat_id=? AND date=?
            """, (amount, user_id, chat_id, today))
            await db.commit()

    # --------------------------------------------------
    # COINS
    # --------------------------------------------------

    async def get_coins(self, user_id: int) -> tuple:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT balance, last_coin_bonus FROM coins WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
        return row if row else (COINS_START, 0.0)

    async def add_coins(self, user_id: int, amount: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO coins (user_id, balance) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?
            """, (user_id, COINS_START + amount, amount))
            await db.commit()
            async with db.execute("SELECT balance FROM coins WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else COINS_START

    async def remove_coins(self, user_id: int, amount: int) -> bool:
        if amount <= 0:
            return False
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO coins (user_id, balance) VALUES (?, ?)",
                    (user_id, COINS_START),
                )
                cursor = await db.execute(
                    "UPDATE coins SET balance = balance - ? "
                    "WHERE user_id = ? AND balance >= ?",
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
        if challenger_id == opponent_id or bet < DUEL_MIN_BET or bet > DUEL_MAX_BET:
            return False, 0, 0
        async with aiosqlite.connect(self.path) as db:
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
                    return False, challenger_balance, opponent_balance
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
                return True, final_balances[challenger_id], final_balances[opponent_id]
            except Exception:
                await db.rollback()
                raise

    async def set_coin_bonus_time(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO coins (user_id, last_coin_bonus) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_coin_bonus = ?
            """, (user_id, time.time(), time.time()))
            await db.commit()

    async def get_coins_top(self, limit: int = 10) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("""
                SELECT c.user_id, COALESCE(u.user_name, CAST(c.user_id AS TEXT)), c.balance
                FROM coins c
                LEFT JOIN user_stats u ON u.user_id = c.user_id AND u.chat_id = ?
                ORDER BY c.balance DESC LIMIT ?
            """, (MAIN_CHAT_ID, limit)) as cur:
                return await cur.fetchall()

    # --------------------------------------------------
    # PROMO CODES
    # --------------------------------------------------

    async def create_promo(self, code: str, reward: int, max_uses: int | None) -> bool:
        if reward <= 0 or (max_uses is not None and max_uses <= 0):
            return False
        async with aiosqlite.connect(self.path) as db:
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
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO promo_codes (code, reward, reward_type, case_id, case_count, max_uses, created_at) "
                    "VALUES (?, 0, 'case', ?, ?, ?, ?)",
                    (code, case_id, case_count, max_uses, time.time()),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def delete_promo(self, code: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("DELETE FROM promo_codes WHERE code=?", (code,))
            await db.commit()
        return cursor.rowcount == 1

    async def get_promos(self) -> list:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT code, reward, reward_type, case_id, case_count, max_uses, uses "
                "FROM promo_codes ORDER BY created_at DESC"
            ) as cur:
                return await cur.fetchall()

    async def get_case_keys(self, user_id: int, case_id: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT amount FROM case_keys WHERE user_id=? AND case_id=?", (user_id, case_id)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def register_bot_user(self, user_id: int, user_name: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO bot_users(user_id,user_name,started_at) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET user_name=excluded.user_name",
                (user_id, user_name, time.time()),
            )
            await db.commit()

    async def create_bonus_broadcast(self, amount: int) -> tuple[int, int]:
        async with aiosqlite.connect(self.path, timeout=30) as db:
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
                        "INSERT INTO coins(user_id,balance) VALUES (?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
                        (user_id, COINS_START + amount, amount),
                    )
                    await db.execute(
                        "INSERT INTO bonus_deliveries(broadcast_id,user_id) VALUES (?,?)",
                        (broadcast_id, user_id),
                    )
                await db.commit()
                return broadcast_id, len(users)
            except Exception:
                await db.rollback()
                raise

    async def open_case(self, user_id: int, case_id: str, price: int | None, key_only: bool = False) -> str:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE case_keys SET amount = amount - 1 "
                    "WHERE user_id=? AND case_id=? AND amount > 0",
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
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT reward, reward_type, case_id, case_count, max_uses, uses "
                    "FROM promo_codes WHERE code=?", (code,)
                ) as cur:
                    promo = await cur.fetchone()
                if not promo:
                    await db.rollback()
                    return "not_found", None, None, None, None, None, None

                reward, reward_type, case_id, case_count, max_uses, uses = promo
                async with db.execute(
                    "SELECT 1 FROM promo_activations WHERE code=? AND user_id=?", (code, user_id)
                ) as cur:
                    already_used = await cur.fetchone()
                if already_used:
                    await db.rollback()
                    return "already_used", None, None, None, None, None, None
                if max_uses is not None and uses >= max_uses:
                    await db.rollback()
                    return "limit_reached", None, None, None, None, None, None

                await db.execute(
                    "INSERT INTO promo_activations (code, user_id, activated_at) VALUES (?, ?, ?)",
                    (code, user_id, time.time()),
                )
                if reward_type == "case":
                    await db.execute(
                        "INSERT INTO case_keys (user_id, case_id, amount) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id, case_id) DO UPDATE SET amount = amount + ?",
                        (user_id, case_id, case_count, case_count),
                    )
                else:
                    await db.execute(
                        "INSERT INTO coins (user_id, balance) VALUES (?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
                        (user_id, COINS_START + reward, reward),
                    )
                await db.execute("UPDATE promo_codes SET uses = uses + 1 WHERE code=?", (code,))
                await db.commit()
                return "success", reward, reward_type, case_id, case_count, uses + 1, max_uses
            except Exception:
                await db.rollback()
                raise


db = Database(os.getenv("DB_PATH", "activity.db"))


def economy_price(key: str) -> int:
    return int(ECONOMY[key])


def case_reward_parts(reward: tuple) -> tuple[str, int, float]:
    if isinstance(reward[0], str):
        kind, value, weight = reward
    else:
        value, weight = reward
        kind = "coins"
    return kind, int(value), float(weight)


def case_reward_tuple(original: tuple, weight: float) -> tuple:
    if isinstance(original[0], str):
        return original[0], original[1], float(weight)
    return original[0], float(weight)


async def load_runtime_settings() -> None:
    settings = await db.get_settings()
    for key in ECONOMY:
        stored = settings.get(f"economy:{key}")
        if stored is not None and stored >= 1:
            ECONOMY[key] = int(stored)
    for dc_amount in STAR_DC_PACKAGES:
        stored = settings.get(f"stars:{dc_amount}")
        if stored is not None and stored >= 1:
            STAR_DC_PACKAGES[dc_amount] = int(stored)
    for case_id, case in CASES.items():
        updated_rewards = []
        for index, reward in enumerate(case["rewards"]):
            stored = settings.get(f"case_chance:{case_id}:{index}")
            updated_rewards.append(case_reward_tuple(reward, stored if stored is not None else case_reward_parts(reward)[2]))
        total_weight = sum(case_reward_parts(reward)[2] for reward in updated_rewards)
        if total_weight > 0:
            updated_rewards = [
                case_reward_tuple(reward, case_reward_parts(reward)[2] * 100.0 / total_weight)
                for reward in updated_rewards
            ]
        case["rewards"] = updated_rewards


async def save_case_chances(case_id: str) -> None:
    for index, reward in enumerate(CASES[case_id]["rewards"]):
        await db.set_setting(f"case_chance:{case_id}:{index}", case_reward_parts(reward)[2])

# =========================
# HELPERS
# =========================

def display_name(user) -> str:
    return user.first_name


def gift_roll_wins(chance: float) -> bool:
    return chance >= MAX_CHANCE or random.uniform(0, GIFT_WIN_ROLL_MAX) <= chance


async def send_log(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(LOG_CHAT_ID, text)
    except Exception as e:
        logger.warning("send_log failed: %s", e)

async def send_game_log(bot: Bot, text: str) -> None:
    if not GAME_LOG_CHAT_ID:
        return
    try:
        await bot.send_message(GAME_LOG_CHAT_ID, text)
    except Exception as e:
        logger.warning("send_game_log failed: %s", e)

async def publish_promo(bot: Bot, text: str) -> bool:
    try:
        await bot.send_message(PROMO_CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning("publish_promo failed: %s", e)
        return False


async def send_gift_safe(bot: Bot, user_id: int, user_name: str, gift_id: str, reason: str) -> None:
    try:
        star_balance = await bot.get_my_star_balance()
        cost_map = {
            EXCHANGE_GIFT_15:  15,
            EXCHANGE_GIFT_25:  25,
            EXCHANGE_GIFT_50:  50,
            EXCHANGE_GIFT_100: 100,
        }
        cost = 15
        await bot.send_gift(user_id=user_id, gift_id=gift_id)
        await send_log(bot, f"🎁 Подарок отправлен\n\n{user_name} ({user_id})\n📝 {reason}\n💫 Баланс: {star_balance.amount}⭐")
    except Exception as e:
        await db.add_pending_gift(user_id, user_name, gift_id, f"{reason} — ошибка: {e}")
        await bot.send_message(ADMIN_ID, f"❌ Ошибка отправки подарка\n\n👤 {user_name} ({user_id})\n📝 {reason}\n📛 {e}\nДобавлен в /pending")


# =========================
# CASINO TIMEOUT CHECKER
# =========================

async def casino_timeout_checker(bot: Bot) -> None:
    while True:
        await asyncio.sleep(30)
        async with game_action_lock:
            now = time.time()
            expired = [uid for uid, g in active_games.items() if g["expires"] < now]
            for uid in expired:
                game = active_games.pop(uid)
                bet  = game["bet"]
                chat_id = game.get("chat_id", MAIN_CHAT_ID)
                await db.add_coins(uid, bet)
                try:
                    await bot.send_message(
                        chat_id,
                        f"⏰ Время вышло! Ставка {bet} D-COINS возвращена на твой баланс.",
                    )
                except Exception:
                    pass


def release_duel(duel_id: str) -> dict | None:
    duel = active_duels.pop(duel_id, None)
    if not duel:
        return None
    for user_id in (duel["challenger_id"], duel.get("opponent_id")):
        if user_id is not None and duel_by_user.get(user_id) == duel_id:
            duel_by_user.pop(user_id, None)
    return duel


async def duel_timeout_checker(bot: Bot) -> None:
    while True:
        await asyncio.sleep(5)
        async with game_action_lock:
            now = time.time()
            expired_ids = [
                duel_id for duel_id, duel in active_duels.items()
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
                        text=(
                            "⌛ Дуэль отменена\n\n"
                            f"Игрок: {duel['challenger_name']}\n"
                            f"Ставка: {duel['bet']:,} DC\n"
                            "Никто не принял вызов за 5 минут."
                        ).replace(",", " "),
                    )
                except Exception as e:
                    logger.warning("Could not expire duel %s: %s", duel_id, e)


# =========================
# ROUTER
# =========================

router    = Router()
cooldowns: TTLCache = TTLCache(maxsize=50_000, ttl=COOLDOWN_SECONDS)
casino_bet_cooldowns: TTLCache = TTLCache(maxsize=50_000, ttl=CASINO_BET_COOLDOWN)
case_open_cooldowns: TTLCache = TTLCache(maxsize=50_000, ttl=CASE_OPEN_COOLDOWN)
duel_cooldowns: TTLCache = TTLCache(maxsize=50_000, ttl=DUEL_COOLDOWN)

# This block is embedded into the standalone bot file by the development workflow.
import json
import secrets
from contextlib import asynccontextmanager
from functools import wraps

# Serialize game actions that share in-memory state across awaited DB operations.
game_action_lock = asyncio.Lock()
school_prize_delivery_lock = asyncio.Lock()


def serialized_game(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with game_action_lock:
            return await function(*args, **kwargs)
    return wrapped

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

# Level -> list of rewards. The last level contains two ordinary prizes.
PATH_REWARDS = {
    1: [("coins", 500)], 2: [("coins", 500)], 3: [("school", 1)],
    4: [("coins", 750)], 5: [("coins", 2000)], 6: [("coins", 750)],
    7: [("school", 1)], 8: [("coins", 1000)], 9: [("coins", 1000)],
    10: [("coins", 3500)], 11: [("coins", 1000)], 12: [("school", 1)],
    13: [("coins", 1250)], 14: [("coins", 1250)], 15: [("coins", 3000)],
    16: [("student", 1)], 17: [("coins", 1500)], 18: [("coins", 1500)],
    19: [("school", 1)], 20: [("coins", 5500)], 21: [("coins", 1500)],
    22: [("coins", 1500)], 23: [("school", 1)], 24: [("coins", 2000)],
    25: [("coins", 5000)], 26: [("coins", 2000)], 27: [("student", 1)],
    28: [("coins", 2500)], 29: [("coins", 2500)], 30: [("coins", 5000)],
    31: [("coins", 2500)], 32: [("school", 1)], 33: [("coins", 3000)],
    34: [("coins", 3000)], 35: [("coins", 7000)], 36: [("student", 1)],
    37: [("coins", 3500)], 38: [("coins", 3500)], 39: [("school", 1)],
    40: [("gift", 15)], 41: [("coins", 4000)], 42: [("student", 1)],
    43: [("coins", 4000)], 44: [("school", 1)], 45: [("excellent", 1)],
    46: [("coins", 5000)], 47: [("coins", 5000)], 48: [("student", 1)],
    49: [("coins", 7000)], 50: [("gift", 25), ("excellent", 1)],
}


class SchoolEvent:
    """All progression, claims, boss damage and refunds use SQLite transactions."""

    def __init__(self, database):
        self.database = database

    @asynccontextmanager
    async def transaction(self):
        async with aiosqlite.connect(self.database.path, timeout=30) as conn:
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
                "CREATE TABLE IF NOT EXISTS school_seasons (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, ends REAL, stopped INTEGER DEFAULT 0, hp INTEGER, max_hp INTEGER, killed REAL, path_winner INTEGER)",
                "CREATE TABLE IF NOT EXISTS school_players (season INTEGER, uid INTEGER, name TEXT, knowledge INTEGER DEFAULT 0, damage INTEGER DEFAULT 0, reached REAL DEFAULT 0, last_message REAL DEFAULT 0, PRIMARY KEY(season,uid))",
                "CREATE TABLE IF NOT EXISTS school_quests (season INTEGER, uid INTEGER, day TEXT, quest TEXT, progress INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0, PRIMARY KEY(season,uid,day,quest))",
                "CREATE TABLE IF NOT EXISTS school_days (season INTEGER, uid INTEGER, day TEXT, streak INTEGER, claimed INTEGER DEFAULT 0, PRIMARY KEY(season,uid,day))",
                "CREATE TABLE IF NOT EXISTS school_claims (season INTEGER, uid INTEGER, prize TEXT, PRIMARY KEY(season,uid,prize))",
                "CREATE TABLE IF NOT EXISTS school_actions (season INTEGER, uid INTEGER, token TEXT, PRIMARY KEY(season,uid,token))",
                "CREATE TABLE IF NOT EXISTS school_prizes (id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER, uid INTEGER, reason TEXT, kind TEXT, amount INTEGER, done INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, UNIQUE(season,uid,reason,kind))",
                "CREATE TABLE IF NOT EXISTS school_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER, tag TEXT, text TEXT, sent INTEGER DEFAULT 0, UNIQUE(season,tag))",
            ):
                await c.execute(sql)
            async with c.execute("PRAGMA table_info(school_prizes)") as cur:
                prize_columns = {row[1] for row in await cur.fetchall()}
            if "attempts" not in prize_columns:
                await c.execute("ALTER TABLE school_prizes ADD COLUMN attempts INTEGER DEFAULT 0")

    async def current(self, c, now=None, active=True):
        row = await self.one(c, "SELECT * FROM school_seasons ORDER BY id DESC LIMIT 1")
        if active and row and (row["stopped"] or (now or time.time()) >= row["ends"]):
            return None
        return row

    async def start(self):
        async with self.transaction() as c:
            if await self.current(c):
                return False
            now = time.time()
            await c.execute("INSERT INTO school_seasons(started,ends,hp,max_hp) VALUES (?,?,5000000,5000000)", (now, now + 20 * 86400))
            return True

    async def stop(self, season):
        async with self.transaction() as c:
            await c.execute("UPDATE school_seasons SET stopped=1 WHERE id=?", (season,))

    async def coins(self, c, uid, amount):
        await c.execute("INSERT INTO coins(user_id,balance) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET balance=balance+?", (uid, COINS_START + amount, amount))

    async def reward(self, c, season, uid, reason, items):
        for kind, amount in items:
            if kind == "coins":
                await self.coins(c, uid, amount)
            elif kind in CASES:
                await c.execute("INSERT INTO case_keys(user_id,case_id,amount) VALUES (?,?,?) ON CONFLICT(user_id,case_id) DO UPDATE SET amount=amount+excluded.amount", (uid, kind, amount))
            else:
                # Real prizes are queued for an administrator; no duplicate external sends.
                await c.execute("INSERT OR IGNORE INTO school_prizes(season,uid,reason,kind,amount) VALUES (?,?,?,?,?)", (season, uid, reason, kind, amount))

    async def prepare(self, c, season, uid, name, now):
        day = datetime.fromtimestamp(now, pytz.timezone("Europe/Moscow")).date().isoformat()
        await c.execute("INSERT INTO school_players(season,uid,name) VALUES (?,?,?) ON CONFLICT(season,uid) DO UPDATE SET name=excluded.name", (season, uid, name))
        exists = await self.one(c, "SELECT 1 FROM school_quests WHERE season=? AND uid=? AND day=?", (season, uid, day))
        if not exists:
            chosen = secrets.SystemRandom().sample(list(QUESTS), 3)
            await c.executemany("INSERT INTO school_quests(season,uid,day,quest) VALUES (?,?,?,?)", [(season, uid, day, q) for q in chosen])
        return day

    async def progress(self, c, season, uid, name, day, now, metrics):
        player = await self.one(c, "SELECT * FROM school_players WHERE season=? AND uid=?", (season, uid))
        metrics = dict(metrics)
        if metrics.get("messages"):
            if now - player["last_message"] < 10:
                metrics.pop("messages")
            else:
                await c.execute("UPDATE school_players SET last_message=? WHERE season=? AND uid=?", (now, season, uid))
        async with c.execute("SELECT * FROM school_quests WHERE season=? AND uid=? AND day=?", (season, uid, day)) as cur:
            rows = await cur.fetchall()
        for row in rows:
            _, metric, target, _, _ = QUESTS[row["quest"]]
            value = max(0, int(metrics.get(metric, 0)))
            if value:
                await c.execute("UPDATE school_quests SET progress=MIN(?,progress+?) WHERE season=? AND uid=? AND day=? AND quest=?", (target, value, season, uid, day, row["quest"]))
        async with c.execute("SELECT quest,progress FROM school_quests WHERE season=? AND uid=? AND day=?", (season, uid, day)) as cur:
            complete = all(row["progress"] >= QUESTS[row["quest"]][2] for row in await cur.fetchall())
        if complete:
            yesterday = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
            previous = await self.one(c, "SELECT streak FROM school_days WHERE season=? AND uid=? AND day=?", (season, uid, yesterday))
            streak = previous[0] % 5 + 1 if previous else 1
            await c.execute("INSERT OR IGNORE INTO school_days(season,uid,day,streak) VALUES (?,?,?,?)", (season, uid, day, streak))

    async def record(self, uid, name, token, metrics=None, loss=0, now=None):
        now = time.time() if now is None else now
        async with self.transaction() as c:
            season = await self.current(c, now)
            if not season or uid <= 0:
                return 0, 0
            sid = season["id"]
            cur = await c.execute("INSERT OR IGNORE INTO school_actions(season,uid,token) VALUES (?,?,?)", (sid, uid, token))
            if cur.rowcount != 1:
                return 0, 0
            day = await self.prepare(c, sid, uid, name, now)
            await self.progress(c, sid, uid, name, day, now, metrics or {})
            damage = refund = 0
            if loss > 0 and season["hp"] > 0:
                damage = min(int(loss), season["hp"])
                refund = int(loss) - damage
                await c.execute("UPDATE school_players SET damage=damage+?, reached=? WHERE season=? AND uid=?", (damage, now, sid, uid))
                await c.execute("UPDATE school_seasons SET hp=hp-? WHERE id=?", (damage, sid))
                if refund:
                    await self.coins(c, uid, refund)
                if damage == season["hp"]:
                    await c.execute("UPDATE school_seasons SET killed=? WHERE id=?", (now, sid))
                    async with c.execute("SELECT * FROM school_players WHERE season=? AND damage>0 ORDER BY damage DESC,reached,uid", (sid,)) as cur:
                        players = await cur.fetchall()
                    for p in players:
                        rewards = []
                        if p["damage"] >= 10000:
                            rewards.append(("coins", 10000))
                        if p["damage"] >= 100000:
                            rewards.append(("excellent", 1))
                        await self.reward(c, sid, p["uid"], "Победа над боссом", rewards)
                    await self.reward(c, sid, uid, "Последний удар", [("coins", 50000)])
                    for place, p in enumerate(players[:3], 1):
                        await self.reward(c, sid, p["uid"], f"Топ-{place} по урону", [[("nft", 1)], [("premium", 1)], [("gift", 100)]][place - 1])
                    top = "\n".join(f"{i}. {p['name']} — {p['damage']:,}" for i, p in enumerate(players[:3], 1))
                    await c.execute("INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)", (sid, "boss", f"🏆 Архимед Знаний побеждён!\n\n{top}\n\nПоследний удар: {name}.\nDC и ключи начислены. NFT, Premium и подарок топ-3 выдаст администратор. Призовой путь продолжается!"))
            return damage, refund

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
                row = await self.one(c, "SELECT * FROM school_quests WHERE season=? AND uid=? AND day=? AND quest=?", (sid, uid, day, qid))
                if not row or row["progress"] < QUESTS[qid][2]:
                    return "Сначала выполни задание."
                if row["claimed"]:
                    return "Награда уже получена."
                await c.execute("UPDATE school_quests SET claimed=1 WHERE season=? AND uid=? AND day=? AND quest=?", (sid, uid, day, qid))
                _, _, _, dc, points = QUESTS[qid]
                await self.coins(c, uid, dc)
                await c.execute("UPDATE school_players SET knowledge=knowledge+? WHERE season=? AND uid=?", (points, sid, uid))
                p = await self.one(c, "SELECT knowledge FROM school_players WHERE season=? AND uid=?", (sid, uid))
                if p[0] >= 5000:
                    cur = await c.execute("UPDATE school_seasons SET path_winner=? WHERE id=? AND path_winner IS NULL", (uid, sid))
                    if cur.rowcount == 1:
                        await self.reward(c, sid, uid, "Первый на уровне 50", [("nft", 1)])
                        await c.execute("INSERT OR IGNORE INTO school_outbox(season,tag,text) VALUES (?,?,?)", (sid, "race", f"🏁 {name} первым достиг 50-го уровня!\n🏆 Отдельный NFT ждёт выдачи администратором. Остальные участники продолжают призовой путь."))
                return f"✅ +{dc:,} DC и +{points} 📖"
            if category == "day":
                if value != day:
                    return "Бонус относится к другому дню."
                row = await self.one(c, "SELECT * FROM school_days WHERE season=? AND uid=? AND day=?", (sid, uid, day))
                if not row:
                    return "Выполни все три задания."
                if row["claimed"]:
                    return "Бонус уже получен."
                await c.execute("UPDATE school_days SET claimed=1 WHERE season=? AND uid=? AND day=?", (sid, uid, day))
                rewards = [("coins", 3000)]
                if row["streak"] == 5:
                    rewards.append(("excellent", 1))
                await self.reward(c, sid, uid, "Серия квестов", rewards)
                return "✅ +3 000 DC" + (" и 🔑 Кейс отличника за 5 дней подряд!" if row["streak"] == 5 else "")
            if category in {"level", "levels"}:
                player = await self.one(c, "SELECT knowledge FROM school_players WHERE season=? AND uid=?", (sid, uid))
                if category == "levels":
                    max_level = min(50, player[0] // 100)
                    received = []
                    received_levels = []
                    for level in range(1, max_level + 1):
                        cur = await c.execute("INSERT OR IGNORE INTO school_claims(season,uid,prize) VALUES (?,?,?)", (sid, uid, f"level:{level}"))
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
                cur = await c.execute("INSERT OR IGNORE INTO school_claims(season,uid,prize) VALUES (?,?,?)", (sid, uid, f"level:{level}"))
                if cur.rowcount != 1:
                    return "Награда уже получена."
                await self.reward(c, sid, uid, f"Уровень {level}", PATH_REWARDS[level])
                return f"✅ Уровень {level}: {event_reward_text(PATH_REWARDS[level])}"
            return "Неизвестная награда."


school_event = SchoolEvent(db)


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


def event_navigation():
    return [
        [InlineKeyboardButton(text="📚 Босс", callback_data="school:boss"), InlineKeyboardButton(text="🏆 Топ по урону", callback_data="school:top")],
        [InlineKeyboardButton(text="⭐ Квесты", callback_data="school:quests"), InlineKeyboardButton(text="📖 Призовой путь", callback_data="school:path:0")],
        [InlineKeyboardButton(text="🏅 Топ-10 призового пути", callback_data="school:path_top")],
    ]


async def event_page(uid, name, page):
    buttons = []
    async with school_event.transaction() as c:
        season = await school_event.current(c, active=False)
        if not season:
            return "🏫 Школьный ивент ещё не запущен.", InlineKeyboardMarkup(inline_keyboard=event_navigation())
        sid = season["id"]
        now = time.time()
        active = not season["stopped"] and now < season["ends"]
        day = await school_event.prepare(c, sid, uid, name, now) if active else datetime.fromtimestamp(now, pytz.timezone("Europe/Moscow")).date().isoformat()
        p = await school_event.one(c, "SELECT * FROM school_players WHERE season=? AND uid=?", (sid, uid))
        knowledge = p["knowledge"] if p else 0
        footer = "" if active else "\n\n⏳ Ивент завершён."
        if page == "quests":
            async with c.execute("SELECT * FROM school_quests WHERE season=? AND uid=? AND day=? ORDER BY quest", (sid, uid, day)) as cur:
                quests = await cur.fetchall()
            lines = [f"⭐ Квесты · {day} (МСК)", f"📖 Очки знаний: {knowledge}", "Смена заданий в 00:00 МСК. Награды забирай сегодня.", ""]
            for row in quests:
                label, _, target, dc, points = QUESTS[row["quest"]]
                lines.append(f"{label}\n{row['progress']}/{target} · {dc:,} DC + {points} 📖")
                if active and row["progress"] >= target and not row["claimed"]:
                    buttons.append([InlineKeyboardButton(text=f"🎁 Забрать: {label}", callback_data=f"scq:{sid}:{uid}:{day}/{row['quest']}")])
                elif row["claimed"]:
                    lines.append("✅ Получено")
            today = await school_event.one(c, "SELECT * FROM school_days WHERE season=? AND uid=? AND day=?", (sid, uid, day))
            yesterday = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
            prev = await school_event.one(c, "SELECT streak FROM school_days WHERE season=? AND uid=? AND day=?", (sid, uid, yesterday))
            streak = today["streak"] if today else (prev[0] % 5 if prev else 0)
            lines.append(f"\n🔥 Серия: {streak}/5 дней\nЗа все 3 задания: 3 000 DC. За 5 дней: 🔑 Отличника.")
            if active and today and not today["claimed"]:
                buttons.append([InlineKeyboardButton(text="🎁 Бонус за все квесты", callback_data=f"scd:{sid}:{uid}:{day}")])
            buttons.append([InlineKeyboardButton(text="🔄 Обновить задания", callback_data="school:quests")])
            text = "\n".join(lines) + footer
        elif page == "path_top":
            async with c.execute(
                "SELECT uid,name,knowledge FROM school_players WHERE season=? AND knowledge>0 "
                "ORDER BY knowledge DESC,uid LIMIT 10",
                (sid,),
            ) as cur:
                leaders = await cur.fetchall()
            async with c.execute(
                "SELECT uid FROM school_players WHERE season=? AND knowledge>0 ORDER BY knowledge DESC,uid",
                (sid,),
            ) as cur:
                all_ranked = await cur.fetchall()
            rank = next((index for index, row in enumerate(all_ranked, 1) if row["uid"] == uid), None)
            lines = ["🏅 Топ-10 призового пути", ""]
            lines.extend(
                f"{index}. {row['name']} — {row['knowledge']:,} 📖 · уровень {min(50, row['knowledge'] // 100)}"
                for index, row in enumerate(leaders, 1)
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
            async with c.execute("SELECT prize FROM school_claims WHERE season=? AND uid=?", (sid, uid)) as cur:
                claimed = {r[0] for r in await cur.fetchall()}
            lines = [f"📖 Призовой путь · {knowledge}/5 000", f"Уровень: {min(50,knowledge // 100)}/50", "Каждые 100 📖 — новый уровень. Первый на 50-м получает NFT.", ""]
            for level in range(offset * 10 + 1, offset * 10 + 11):
                status = "✅" if f"level:{level}" in claimed else ("🎁" if knowledge >= level * 100 else "🔒")
                lines.append(f"{status} {level}. {event_reward_text(PATH_REWARDS[level])}")
                if active and status == "🎁":
                    buttons.append([InlineKeyboardButton(text=f"Забрать уровень {level}", callback_data=f"scl:{sid}:{uid}:{level}")])
            if active and any(knowledge >= level * 100 and f"level:{level}" not in claimed for level in PATH_REWARDS):
                buttons.insert(0, [InlineKeyboardButton(text="🎁 Забрать все доступные награды", callback_data=f"scla:{sid}:{uid}:{offset}")])
            buttons.append([InlineKeyboardButton(text=str(i * 10 + 1) + "–" + str(i * 10 + 10), callback_data=f"school:path:{i}") for i in range(5)])
            text = "\n".join(lines) + footer
        else:
            async with c.execute("SELECT * FROM school_players WHERE season=? AND damage>0 ORDER BY damage DESC,reached,uid", (sid,)) as cur:
                ranked = await cur.fetchall()
            rank = next((str(i) for i, r in enumerate(ranked, 1) if r["uid"] == uid), "—")
            if page == "top":
                text = "🏆 Топ по урону Архимеду\n\n" + ("\n".join(f"{i}. {r['name']} — {r['damage']:,}" for i, r in enumerate(ranked[:10], 1)) or "Урона пока нет.")
                text += f"\n\nТвоё место: {rank}\n🥇 NFT · 🥈 Premium · 🥉 подарок 100⭐" + footer
                buttons.append([InlineKeyboardButton(text="🔄 Обновить топ", callback_data="school:top")])
            else:
                hours = max(0, int((season["ends"] - now) / 3600))
                text = f"📚 Архимед Знаний\n❤️ {season['hp']:,} / {season['max_hp']:,} HP\n⚔️ Твой урон: {p['damage'] if p else 0:,}\n🏆 Твоё место: {rank}\n⏳ Осталось: {hours // 24} д. {hours % 24} ч."
                text += "\n\nПроигранные ставки наносят урон. Дуэли не учитываются. Очки знаний идут только в призовой путь."
                text += "\nЗа победу: от 10 000 урона — 10 000 DC; от 100 000 — также ключ Отличника. Последний удар: 50 000 DC."
                if season["hp"] == 0:
                    text += "\n\n🏆 Босс побеждён! Рейтинг зафиксирован."
                text += footer
                buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="school:boss")])
    buttons.extend(event_navigation())
    return text.replace(",", " "), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text.regexp(r"(?i)^/?(ивент|квесты|путь|event|quests|path)(?:@\w+)?$"))
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
    async with school_prize_delivery_lock:
        async with school_event.transaction() as c:
            async with c.execute(
                "SELECT id,amount FROM school_prizes "
                "WHERE season=? AND uid=? AND kind='gift' AND done=0 AND attempts<3 ORDER BY id",
                (sid, uid),
            ) as cur:
                prizes = await cur.fetchall()
        gift_sizes = {15: 5, 25: 10, 50: 15, 100: 20}
        for prize in prizes:
            try:
                gift_id = random.choice(GIFT_IDS[gift_sizes[prize["amount"]]])
                await bot.send_gift(user_id=uid, gift_id=gift_id)
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
    return sent, failed


@router.callback_query(F.data.startswith("school:"))
async def school_callback(callback: CallbackQuery):
    if await db.is_banned(callback.from_user.id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    await school_edit(callback, callback.data.split(":", 1)[1])
    await callback.answer()


@router.callback_query(F.data.startswith("scq:") | F.data.startswith("scd:") | F.data.startswith("scl:") | F.data.startswith("scla:"))
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
        result = await school_event.claim(int(sid), int(owner), display_name(callback.from_user), category, value)
        if kind in {"scl", "scla"}:
            sent, failed = await deliver_school_gifts(bot, int(sid), int(owner))
            if sent:
                result += f" Подарков отправлено: {sent}."
            if failed:
                result += f" Не удалось отправить: {failed}; оставлено в очереди админу."
    except (ValueError, KeyError):
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    await callback.answer(result[:190], show_alert=True)
    if kind == "scl":
        page = f"path:{(int(value)-1)//10}"
    elif kind == "scla":
        page = f"path:{max(0, min(4, int(value)))}"
    else:
        page = "quests"
    await school_edit(callback, page)


async def school_admin_page(callback, page=0):
    async with school_event.transaction() as c:
        season = await school_event.current(c, active=False)
        async with c.execute("SELECT * FROM school_prizes WHERE done=0 ORDER BY id LIMIT 10 OFFSET ?", (page * 10,)) as cur:
            prizes = await cur.fetchall()
        total = (await school_event.one(c, "SELECT COUNT(*) FROM school_prizes WHERE done=0"))[0]
    text = "🏫 Управление школьным ивентом\n"
    if season:
        text += f"Сезон #{season['id']} · HP {season['hp']:,}/{season['max_hp']:,}\n"
        text += "Окончание: " + datetime.fromtimestamp(season["ends"], pytz.timezone("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК") + "\n"
    text += f"\n🎁 Ожидают ручной выдачи: {total}\nСначала выдай приз, затем отметь заявку.\n"
    buttons = [[InlineKeyboardButton(text="▶️ Запустить на 20 дней", callback_data="sca:startask")]]
    if season:
        buttons.append([InlineKeyboardButton(text="⏹ Завершить ивент", callback_data=f"sca:stopask:{season['id']}")])
    for row in prizes:
        text += f"\n#{row['id']} · ID {row['uid']} · {row['reason']} · {event_reward_text([(row['kind'],row['amount'])])}"
        buttons.append([InlineKeyboardButton(text=f"✅ Приз #{row['id']} выдан", callback_data=f"sca:doneask:{row['id']}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sca:page:{page-1}"))
    if (page + 1) * 10 < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sca:page:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="sca:page:0"), InlineKeyboardButton(text="◀️ Админка", callback_data="admin_panel")])
    try:
        await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
    if action.endswith("ask"):
        confirm = callback.data.replace("ask", "", 1)
        descriptions = {"startask": "Запустить новый ивент на 20 дней с 5 000 000 HP?", "stopask": "Завершить ивент сейчас? Прогресс и получение наград остановятся. Награды за непобеждённого босса не выдаются.", "doneask": "Приз действительно выдан игроку?"}
        await callback.message.edit_text(descriptions[action], reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=confirm)],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="sca:page:0")],
        ]))
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
            await c.execute("UPDATE school_prizes SET done=1 WHERE id=? AND done=0", (int(parts[2]),))
        await callback.answer("Выдача отмечена")
    else:
        await callback.answer()
    await school_admin_page(callback, max(0, int(parts[2])) if action == "page" else 0)


async def school_notifications(bot):
    # Durable outbox retries notifications after a restart or temporary API failure.
    while True:
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
                    "SELECT DISTINCT season,uid FROM school_prizes "
                    "WHERE kind='gift' AND done=0 AND attempts<3 LIMIT 10"
                ) as cur:
                    gift_owners = await cur.fetchall()
            for owner in gift_owners:
                await deliver_school_gifts(bot, owner["season"], owner["uid"])
        except Exception:
            logger.exception("School event notification failed")
        await asyncio.sleep(5)


async def bonus_notification_worker(bot: Bot) -> None:
    while True:
        try:
            async with aiosqlite.connect(db.path) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT d.broadcast_id,d.user_id,b.amount,d.attempts "
                    "FROM bonus_deliveries d JOIN bonus_broadcasts b ON b.id=d.broadcast_id "
                    "WHERE d.status=0 ORDER BY d.broadcast_id,d.user_id LIMIT 20"
                ) as cur:
                    rows = await cur.fetchall()
            for row in rows:
                try:
                    await bot.send_message(
                        row["user_id"],
                        f"🎁 Администратор раздал всем игрокам {row['amount']:,} DC!\n"
                        "🪙 Монеты уже зачислены на твой баланс.".replace(",", " "),
                    )
                    status, error = 1, None
                except Exception as exc:
                    status = 2 if row["attempts"] >= 2 else 0
                    error = str(exc)[:300]
                async with aiosqlite.connect(db.path) as conn:
                    await conn.execute(
                        "UPDATE bonus_deliveries SET status=?,attempts=attempts+1,error=? "
                        "WHERE broadcast_id=? AND user_id=? AND status=0",
                        (status, error, row["broadcast_id"], row["user_id"]),
                    )
                    await conn.commit()
                await asyncio.sleep(0.05)
        except Exception:
            logger.exception("Bonus notification worker failed")
        await asyncio.sleep(2)


async def school_game(user, token, bet, won):
    damage, refund = await school_event.record(user.id, display_name(user), token,
        {"games": 1, "gamewin": int(won), "bets": bet}, loss=0 if won else bet)
    return f"\n📚 Урон боссу: {damage:,}. Возвращено: {refund:,} DC" if damage else ""

PLAIN_COMMANDS = {
    "start", "help", "say", "vip", "unvip", "viplist",
    "ban", "unban", "banlist", "addmsgs", "removemsgs", "addday", "removeday",
    "addcoins", "removecoins", "createpromo", "deletepromo", "createcasepromo",
    "promos", "balance", "popolnit", "sendgift",
    "pending", "deliver", "deletepending", "premiumorders", "premiumdone", "premiumrefund",
    "stats", "top", "winstop", "cointop",
    "coins", "promo", "transfer", "daytop", "bonus", "cases", "slots",
    "roulette", "dice", "mines", "duel", "exchange", "admin", "broadcast",
}

RUSSIAN_COMMANDS = {
    "старт": "start", "начать": "start", "помощь": "help",
    "кейсы": "cases", "баланс": "coins", "монеты": "coins",
    "обмен": "exchange", "бонус": "bonus", "стата": "stats",
    "статистика": "stats", "топ": "top", "победители": "winstop",
    "топкоинов": "cointop", "дневнойтоп": "daytop",
    "промо": "promo", "перевод": "transfer", "слоты": "slots",
    "рулетка": "roulette", "кубик": "dice", "мины": "mines",
    "удалитьзаявку": "deletepending",
    "админ": "admin", "админка": "admin", "дуэль": "duel", "дуель": "duel",
    "раздать": "broadcast",
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
        colors = {"красное": "red", "красный": "red", "черное": "black", "чёрное": "black", "черный": "black", "чёрный": "black"}
        parts[1] = colors.get(parts[1].lower(), parts[1])
    return command, parts[1:]

def is_plain_command(message: Message) -> bool:
    return parse_plain_command(message.text) is not None

def start_keyboard(is_admin: bool = False):
    buttons = [
        [InlineKeyboardButton(text="🏫 Школьный ивент", callback_data="school:boss")],
        [InlineKeyboardButton(text="⭐ Квесты", callback_data="school:quests"), InlineKeyboardButton(text="📖 Призовой путь", callback_data="school:path:0")],
        [InlineKeyboardButton(text="📦 Кейсы", callback_data="cases")],
        [InlineKeyboardButton(text="⭐ Купить D-COINS", callback_data="buy_dc_menu")],
        [InlineKeyboardButton(text="❓ Как играть", callback_data="help")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")],
    ])

async def is_channel_subscriber(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = getattr(member.status, "value", member.status)
        return status not in {"left", "kicked"}
    except Exception as e:
        logger.warning("Subscription check failed for %s: %s", user_id, e)
        return False

async def send_subscription_prompt(message: Message) -> None:
    await message.answer(
        "📢 Чтобы пользоваться ботом, подпишись на наш канал.",
        reply_markup=subscription_keyboard(),
    )

def exchange_keyboard(balance: int):
    buttons = [
        [InlineKeyboardButton(text=f"📈 {economy_price('chance_price'):,} DC → +1% шанса".replace(",", " "), callback_data="exch_chance")],
        [InlineKeyboardButton(text=f"🎁 {economy_price('gift_15'):,} DC → подарок 15⭐".replace(",", " "), callback_data="exch_gift_15")],
        [InlineKeyboardButton(text=f"🎁 {economy_price('gift_25'):,} DC → подарок 25⭐".replace(",", " "), callback_data="exch_gift_25")],
        [InlineKeyboardButton(text=f"🎁 {economy_price('gift_50'):,} DC → подарок 50⭐".replace(",", " "), callback_data="exch_gift_50")],
        [InlineKeyboardButton(text=f"🎁 {economy_price('gift_100'):,} DC → подарок 100⭐".replace(",", " "), callback_data="exch_gift_100")],
        [InlineKeyboardButton(text=f"💎 {economy_price('premium_1m'):,} DC → Premium на месяц".replace(",", " "), callback_data="exch_premium_1m")],
        [InlineKeyboardButton(text="⭐ Купить DC за звёзды", callback_data="buy_dc_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def buy_dc_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for dc_amount, star_amount in STAR_DC_PACKAGES.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"🪙 {dc_amount:,} DC — {star_amount}⭐".replace(",", " "),
                callback_data=f"buy_dc_{dc_amount}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ К обмену", callback_data="buy_dc_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def cases_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎒 ШКОЛЬНЫЙ — 3 000 DC", callback_data="case_view_school")],
        [InlineKeyboardButton(text="🎓 СТУДЕНЧЕСКИЙ — 12 000 DC", callback_data="case_view_student")],
        [InlineKeyboardButton(text="🏆 КЕЙС ОТЛИЧНИКА — только 🔑", callback_data="case_view_excellent")],
        [InlineKeyboardButton(text="🩸 BLOOD — 5 000 DC", callback_data="case_view_blood")],
        [InlineKeyboardButton(text="🐆 PANTERA — 10 000 DC", callback_data="case_view_pantera")],
        [InlineKeyboardButton(text="🕷 SPIDER MAN — 7 500 DC", callback_data="case_view_spider_man")],
    ])

def case_detail_keyboard(case_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Открыть кейс", callback_data=f"case_open_{case_id}")],
        [InlineKeyboardButton(text="⬅️ Все кейсы", callback_data="cases")],
    ])

# =========================
# PRIVATE — /start
# =========================

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    if await db.is_banned(message.from_user.id):
        await message.answer(BAN_MESSAGE)
        return
    await db.register_bot_user(message.from_user.id, display_name(message.from_user))
    if not await is_channel_subscriber(bot, message.from_user.id):
        await send_subscription_prompt(message)
        return
    await message.answer(
        "👋 Добро пожаловать!\n\nВыберите действие:",
        reply_markup=start_keyboard(message.from_user.id == ADMIN_ID)
    )

@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_channel_subscriber(bot, callback.from_user.id):
        await callback.answer("❌ Подписка пока не найдена.", show_alert=True)
        return
    await db.register_bot_user(callback.from_user.id, display_name(callback.from_user))
    await callback.message.edit_text(
        "👋 Добро пожаловать!\n\nВыберите действие:",
        reply_markup=start_keyboard(callback.from_user.id == ADMIN_ID),
    )
    await callback.answer("✅ Подписка подтверждена")

async def send_help(message: Message) -> None:
    await message.answer(
        "📖 Как играть\n\n"
        "1️⃣ Подпишись на канал и нажми «Проверить подписку».\n"
        "2️⃣ Пиши сообщения в основной группе — за них начисляются DC.\n"
        "3️⃣ Забирай ежедневный бонус: бонус.\n\n"
        "🎰 Игры — только в личке с ботом\n"
        "• слоты 50\n"
        "• рулетка красное 50\n"
        "• кубик 3 50\n"
        "• мины 2500\n"
        "В минах открывай клетки и забирай выигрыш до того, как попадёшь на бомбу.\n\n"
        "⚔️ Дуэли — только в основном чате\n"
        "• дуэль 1000 — создать вызов на 1 000 DC\n"
        "• дуэль 1000 @username — вызвать конкретного игрока\n"
        "Также можно ответить «дуэль 1000» на сообщение соперника.\n"
        "Соперник принимает дуэль кнопкой, победитель получает весь банк.\n\n"
        "💱 Полезное\n"
        "• баланс — твои DC\n"
        "• обмен — обмен DC на шанс, подарки или Premium\n"
        "• промо КОД — активировать промокод\n"
        "• перевод @username сумма — отправить DC игроку\n"
        "• стата — статистика в основном чате\n\n"
        "Команды пишутся без /"
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

# =========================
# PRIVATE — ADMIN COMMANDS
# =========================

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
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить.\n\n📛 {e}")

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
    async with aiosqlite.connect(db.path) as conn:
        async with conn.execute(
            "SELECT v.user_id, COALESCE(u.user_name, CAST(v.user_id AS TEXT)), COALESCE(u.msg_count, 0) "
            "FROM vip_users v LEFT JOIN user_stats u ON u.user_id = v.user_id AND u.chat_id = ?",
            (MAIN_CHAT_ID,)
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
        amount  = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    inv_name  = await db.get_user_name(user_id)
    new_count  = msg_count + amount
    new_chance = min(round(chance + STEP * amount, 3), MAX_CHANCE)
    await db.update_user(user_id, MAIN_CHAT_ID, inv_name, new_chance, new_count, last_bonus)
    await message.answer(
        f"✅ Добавлено {amount} сообщений\n"
        f"👤 {inv_name} ({user_id})\n"
        f"💬 Сообщений: {new_count}\n"
        f"📈 Шанс: {new_chance:.3f}%"
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
        amount  = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    inv_name  = await db.get_user_name(user_id)
    new_count  = max(0, msg_count - amount)
    new_chance = max(round(chance - STEP * amount, 3), START_CHANCE)
    await db.update_user(user_id, MAIN_CHAT_ID, inv_name, new_chance, new_count, last_bonus)
    await message.answer(
        f"✅ Убрано {amount} сообщений\n"
        f"👤 {inv_name} ({user_id})\n"
        f"💬 Сообщений: {new_count}\n"
        f"📈 Шанс: {new_chance:.3f}%"
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
        amount  = int(args[2])
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
        amount  = int(args[2])
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
        amount  = int(args[2])
    except ValueError:
        await message.answer("❌ Укажи числовой ID и количество.")
        return
    if amount <= 0:
        await message.answer("❌ Количество должно быть положительным.")
        return
    new_balance = await db.add_coins(user_id, amount)
    name = await db.get_user_name(user_id)
    await message.answer(f"✅ Добавлено {amount} DC\n👤 {name} ({user_id})\n🪙 Баланс: {new_balance} D-COINS")

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
        amount  = int(args[2])
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
        await message.answer(f"✅ Убрано {amount} DC\n👤 {name} ({user_id})\n🪙 Баланс: {balance} D-COINS")
    else:
        await message.answer(f"❌ Недостаточно монет\n👤 {name} ({user_id})\n🪙 Баланс: {balance} D-COINS")

@router.message(Command("createpromo"), F.chat.type == "private")
async def cmd_createpromo(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) not in (3, 4):
        await message.answer("Использование: /createpromo КОД награда_DC [лимит]\nЛимит не указывай для безлимитного промокода.")
        return
    code = args[1].upper()
    if not 3 <= len(code) <= 32 or not all(char.isalnum() or char in "_-" for char in code):
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
    await message.answer(f"✅ Промокод {code} создан.\n🎁 Награда: {reward} DC\n👥 Активаций: {limit_text}")
    if not await publish_promo(
        bot,
        f"🎁 Новый промокод!\n\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"🪙 Награда: {reward:,} DC\n"
        f"👥 Активаций: {limit_text}\n\n"
        f"Активировать в боте: промо {code}".replace(",", " "),
    ):
        await message.answer("⚠️ Промокод создан, но не отправлен в канал. Проверь, что бот — администратор канала.")

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
    args = message.text.split()
    if len(args) not in (3, 4, 5):
        await message.answer("Использование: /createcasepromo КОД КЕЙС [ключей] [лимит]\nПример: /createcasepromo BLOODFREE blood 1 100")
        return
    code = args[1].upper()
    case_id = args[2].lower()
    if not 3 <= len(code) <= 32 or not all(char.isalnum() or char in "_-" for char in code):
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
    await message.answer(f"✅ Промокод {code} создан.\n🎟 Кейс: {CASES[case_id]['title']} × {case_count}\n👥 Активаций: {limit_text}")
    if not await publish_promo(
        bot,
        f"🎁 Новый промокод!\n\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"🎟 Награда: {CASES[case_id]['title']} × {case_count}\n"
        f"👥 Активаций: {limit_text}\n\n"
        f"Активировать в боте: промо {code}",
    ):
        await message.answer("⚠️ Промокод создан, но не отправлен в канал. Проверь, что бот — администратор канала.")

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
    except Exception as e:
        await message.answer("❌ Не удалось получить баланс.")

@router.message(Command("popolnit"), F.chat.type == "private")
async def cmd_popolnit(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await bot.send_invoice(
        chat_id=message.from_user.id,
        title="💫 Пополнение бота",
        description=f"Пополнение баланса бота на {POPOLNIT_AMOUNT} звёзд",
        payload="admin_topup",
        currency="XTR",
        prices=[LabeledPrice(label="Звёзды", amount=POPOLNIT_AMOUNT)],
    )

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    if payment.invoice_payload == "admin_topup":
        await message.answer(f"✅ Оплата прошла!\n💫 Зачислено: {payment.total_amount} звёзд.")
        await send_log(bot, f"💫 Пополнение\n\n👤 Админ: {message.from_user.id}\n⭐ {payment.total_amount} звёзд")
        return

    if not payment.invoice_payload.startswith("buy_dc_"):
        return
    try:
        payload_parts = payment.invoice_payload.removeprefix("buy_dc_").split("_")
        dc_amount = int(payload_parts[0])
        invoice_star_amount = int(payload_parts[1]) if len(payload_parts) > 1 else None
    except (ValueError, IndexError):
        logger.warning("Unknown Stars payment payload: %s", payment.invoice_payload)
        return
    current_star_amount = STAR_DC_PACKAGES.get(dc_amount)
    star_amount = invoice_star_amount if invoice_star_amount is not None else current_star_amount
    if current_star_amount is None or star_amount is None or payment.total_amount != star_amount or payment.currency != "XTR":
        logger.warning("Invalid Stars payment: payload=%s amount=%s", payment.invoice_payload, payment.total_amount)
        return
    credited, new_balance = await db.credit_star_coin_purchase(
        payment.telegram_payment_charge_id,
        message.from_user.id,
        dc_amount,
        star_amount,
    )
    if not credited:
        await message.answer("ℹ️ Эта оплата уже была зачислена ранее.")
        return
    await message.answer(
        f"✅ Оплата прошла!\n"
        f"🪙 Зачислено: {dc_amount:,} DC\n"
        f"⭐ Оплачено: {star_amount}⭐\n"
        f"🪙 Баланс: {new_balance:,} DC".replace(",", " ")
    )
    await send_log(
        bot,
        f"⭐ Покупка DC\n\n👤 {display_name(message.from_user)} ({message.from_user.id})\n"
        f"⭐ {star_amount} → 🪙 {dc_amount:,} DC".replace(",", " "),
    )

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
        star_balance = await bot.get_my_star_balance()
        await bot.send_gift(user_id=user_id, gift_id=gift_id)
        await send_log(bot, f"🎁 Ручная выдача\n\n👤 {user_id}\n📦 {gift_id}\n💫 {star_balance.amount}⭐")
        await message.answer(f"✅ Подарок отправлен!\n👤 {user_id}\n📦 {gift_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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
    text = f"📋 Невыданные подарки ({len(gifts)}):\n💫 Баланс: {star_balance.amount}⭐\n\n"
    for g_id, uid, user_name, gift_id, reason, created_at in gifts:
        date = dt.datetime.fromtimestamp(created_at).strftime("%d.%m %H:%M")
        text += (
            f"#{g_id} | {user_name} ({uid})\n📦 {gift_id}\n📝 {reason} | {date}\n"
            f"👉 deliver {g_id} — выдать\n"
            f"🗑 deletepending {g_id} — удалить\n\n"
        )
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
    gift  = next((g for g in gifts if g[0] == gift_db_id), None)
    if not gift:
        await message.answer("❌ Подарок не найден.")
        return
    _, user_id, user_name, gift_id, reason, _ = gift
    try:
        star_balance = await bot.get_my_star_balance()
        await bot.send_gift(user_id=user_id, gift_id=gift_id)
        await db.remove_pending_gift(gift_db_id)
        await send_log(bot, f"🎁 Отложенный подарок выдан\n\n{user_name} ({user_id})\n💫 {star_balance.amount}⭐")
        await message.answer(f"✅ Подарок выдан!\n👤 {user_name} ({user_id})\n💫 Баланс: {star_balance.amount}⭐")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\n\nПополни баланс через /popolnit")

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
        text += (
            f"#{order_id} | {user_name} ({user_id})\n"
            f"🪙 {cost:,} DC | {date}\n"
            f"После выдачи: premiumdone {order_id}\n"
            f"Возврат: premiumrefund {order_id}\n\n"
        ).replace(",", " ")
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
    new_balance = await db.add_coins(user_id, cost)
    await db.remove_premium_order(order_id)
    await message.answer(f"↩️ Возвращено {cost:,} DC игроку {user_name} ({user_id}).".replace(",", " "))
    try:
        await bot.send_message(
            user_id,
            f"↩️ Premium пока недоступен — тебе вернули {cost:,} DC.\n🪙 Баланс: {new_balance:,} DC".replace(",", " "),
        )
    except Exception as e:
        logger.warning("Could not notify Premium refund recipient: %s", e)


@router.message(Command("broadcast"), F.chat.type == "private")
async def cmd_broadcast(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: раздать СУММА\nПример: раздать 10000")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.answer("❌ Сумма должна быть целым числом.")
        return
    if amount <= 0 or amount > 1_000_000_000:
        await message.answer("❌ Сумма должна быть от 1 до 1 000 000 000 DC.")
        return
    broadcast_id, recipients = await db.create_bonus_broadcast(amount)
    await message.answer(
        f"✅ Раздача #{broadcast_id} создана.\n"
        f"🪙 По {amount:,} DC начислено: {recipients} игрокам.\n"
        "📨 Уведомления отправляются автоматически.".replace(",", " ")
    )

# =========================
# ADMIN PANEL
# =========================

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
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
    ])

def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")]
    ])

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
    return (
        "🛠 Админ-панель\n\n"
        f"📋 Подарков в pending: {pending_count}\n"
        f"💎 Заявок Premium: {premium_count}\n"
        f"🎟 Активных промокодов: {promo_count}\n"
        f"🚫 Заблокировано: {ban_count}\n"
        f"⭐ Баланс бота: {stars_text}\n"
        f"🔄 Обновлено: {updated_at}"
    )

@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(await admin_dashboard_text(bot), reply_markup=admin_panel_keyboard())

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
            buttons.append([
                InlineKeyboardButton(text=f"✅ Выдать #{gift_id}", callback_data=f"admin_deliver_{gift_id}"),
                InlineKeyboardButton(text=f"🗑 Удалить #{gift_id}", callback_data=f"admin_dropgift_{gift_id}"),
            ])
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
        await bot.send_gift(user_id=user_id, gift_id=telegram_gift_id)
        await db.remove_pending_gift(gift_db_id)
        await send_log(bot, f"🎁 Подарок #{gift_db_id} выдан через админ-панель\n{user_name} ({user_id})")
        await render_admin_pending(callback, bot, f"✅ Подарок #{gift_db_id} выдан.")
        await callback.answer("✅ Выдано")
    except Exception as e:
        await callback.answer(f"❌ Не удалось выдать: {str(e)[:150]}", show_alert=True)

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
            buttons.append([
                InlineKeyboardButton(text=f"✅ Выдан #{order_id}", callback_data=f"admin_premiumdone_{order_id}"),
                InlineKeyboardButton(text=f"↩️ Возврат #{order_id}", callback_data=f"admin_premiumrefund_{order_id}"),
            ])
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
    order_id = int(callback.data.removeprefix("admin_premiumdone_"))
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
    order_id = int(callback.data.removeprefix("admin_premiumrefund_"))
    order = next((item for item in await db.get_premium_orders() if item[0] == order_id), None)
    if not order:
        await render_admin_premium(callback, "ℹ️ Заявка уже обработана.")
        await callback.answer()
        return
    _, user_id, user_name, cost, _ = order
    new_balance = await db.add_coins(user_id, cost)
    await db.remove_premium_order(order_id)
    try:
        await bot.send_message(user_id, f"↩️ Тебе вернули {cost:,} DC. Баланс: {new_balance:,} DC".replace(",", " "))
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
            prize = f"{CASES[case_id]['title']} × {case_count}" if reward_type == "case" else f"{reward:,} DC".replace(",", " ")
            text += f"• {code} — {prize} ({limit})\n"
            callback_data = f"admin_promodel:{code}"
            confirm_data = f"admin_promoconfirm:{code}"
            if len(confirm_data.encode("utf-8")) <= 64:
                buttons.append([InlineKeyboardButton(text=f"🗑 {code}", callback_data=callback_data)])
    else:
        text += "Промокодов нет."
    text += "\nСоздать: createpromo КОД DC ЛИМИТ\nКейс: createcasepromo КОД КЕЙС КЛЮЧИ ЛИМИТ"
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
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_promoconfirm:{code}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_promos")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promoconfirm:"))
async def admin_promo_delete_confirm_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    code = callback.data.removeprefix("admin_promoconfirm:")
    await db.delete_promo(code)
    # Повторно рисуем список без удалённого промокода.
    await admin_promos_callback(callback)

async def render_admin_users(callback: CallbackQuery) -> None:
    async with aiosqlite.connect(db.path) as conn:
        async with conn.execute(
            "SELECT v.user_id, COALESCE(u.user_name, CAST(v.user_id AS TEXT)) "
            "FROM vip_users v LEFT JOIN user_stats u ON u.user_id=v.user_id AND u.chat_id=? LIMIT 20",
            (MAIN_CHAT_ID,),
        ) as cur:
            vip_rows = await cur.fetchall()
    bans = await db.get_ban_list()
    buttons = []
    text = f"👥 Пользователи\n\n👑 VIP: {len(vip_rows)}\n"
    text += "\n".join(f"• {name} ({uid})" for uid, name in vip_rows) if vip_rows else "Нет VIP"
    for uid, name in vip_rows:
        buttons.append([InlineKeyboardButton(text=f"👑 Убрать VIP: {name}", callback_data=f"admin_unvip:{uid}")])
    text += f"\n\n🚫 Заблокировано: {len(bans)}\n"
    text += "\n".join(f"• {name} ({uid}) — {reason or 'без причины'}" for uid, name, reason in bans[:20]) if bans else "Список пуст"
    for uid, name, _ in bans[:20]:
        buttons.append([InlineKeyboardButton(text=f"🔓 Разбанить: {name}", callback_data=f"admin_unban:{uid}")])
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
    "chance_price": ("📈 +1% шанса", 1000),
    "gift_15": ("🎁 Подарок 15⭐", 10000),
    "gift_25": ("🎁 Подарок 25⭐", 10000),
    "gift_50": ("🎁 Подарок 50⭐", 10000),
    "gift_100": ("🎁 Подарок 100⭐", 10000),
    "premium_1m": ("💎 Premium 1 месяц", 50000),
}


def admin_economy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Цены обмена", callback_data="admin_econ_exchange")],
        [InlineKeyboardButton(text="⭐ Покупка DC", callback_data="admin_econ_stars")],
        [InlineKeyboardButton(text="🎲 Шансы кейсов", callback_data="admin_econ_cases")],
        [InlineKeyboardButton(text="◀️ Админ-панель", callback_data="admin_panel")],
    ])


async def render_admin_economy(callback: CallbackQuery) -> None:
    text = (
        "💰 Управление экономикой\n\n"
        "Все изменения применяются сразу и сохраняются после перезапуска бота.\n"
        "Выбери раздел:"
    )
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
        buttons.append([
            InlineKeyboardButton(text="➖", callback_data=f"admin_eprice:{key}:down"),
            InlineKeyboardButton(text=f"{value:,} DC".replace(",", " "), callback_data="admin_noop"),
            InlineKeyboardButton(text="➕", callback_data=f"admin_eprice:{key}:up"),
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Экономика", callback_data="admin_economy")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


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
        buttons.append([
            InlineKeyboardButton(text="➖10⭐", callback_data=f"admin_star:{dc_amount}:down"),
            InlineKeyboardButton(text=f"{dc_amount // 1000}K · {stars}⭐", callback_data="admin_noop"),
            InlineKeyboardButton(text="➕10⭐", callback_data=f"admin_star:{dc_amount}:up"),
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Экономика", callback_data="admin_economy")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


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
    lines = [f"🎲 {case['title']} — шансы", "", "Монеты меняются на 1%, подарки — на 0.05%:"]
    buttons = []
    for index, reward in enumerate(case["rewards"]):
        kind, _, chance = case_reward_parts(reward)
        label = case_prize_label(reward)
        lines.append(f"{label}: {chance:.2f}%")
        step_label = "0.05" if kind == "gift" else "1"
        buttons.append([
            InlineKeyboardButton(text=f"➖{step_label}", callback_data=f"admin_caseadj:{case_id}:{index}:down"),
            InlineKeyboardButton(text=f"{label} · {chance:.2f}%", callback_data="admin_noop"),
            InlineKeyboardButton(text=f"➕{step_label}", callback_data=f"admin_caseadj:{case_id}:{index}:up"),
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Все кейсы", callback_data="admin_econ_cases")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


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
    if abs(new_chance - old_chance) < 0.000001:
        await callback.answer("Достигнут предел изменения", show_alert=True)
        return
    other_total = sum(case_reward_parts(item)[2] for i, item in enumerate(rewards) if i != index)
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
        "🛠 Основные админ-команды\n\n"
        "addcoins ID СУММА / removecoins ID СУММА\n"
        "addmsgs ID КОЛ-ВО / removemsgs ID КОЛ-ВО\n"
        "vip ID / unvip ID / ban ID ПРИЧИНА / unban ID\n"
        "createpromo КОД DC ЛИМИТ\n"
        "createcasepromo КОД КЕЙС КЛЮЧИ ЛИМИТ\n"
        "promos / pending / premiumorders\n"
        "say ТЕКСТ — сообщение в основную группу\n"
        "раздать СУММА — начислить DC всем пользователям бота\n"
        "popolnit — пополнить баланс звёзд бота",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()

# =========================
# GROUP — /stats
# =========================

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
    balance, _ = await db.get_coins(user_id)
    await message.reply(
        f"📊 Статистика:\n\n"
        f"📈 Шанс: {chance:.3f}%\n"
        f"💬 Сообщений: {msg_count}\n"
        f"🏆 Побед: {wins}\n"
        f"🪙 D-COINS: {balance}"
    )

@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    top  = await db.get_top(message.chat.id)
    text = "🏆 Топ участников:\n\n"
    for i, (name, chance, count) in enumerate(top, start=1):
        text += f"{i}. {name} — {chance:.3f}% ({count} сообщ.)\n"
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
    balance, _ = await db.get_coins(message.from_user.id)
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
    status, reward, reward_type, case_id, case_count, uses, max_uses = await db.redeem_promo(code, message.from_user.id)
    if status == "success":
        limit_text = f"{uses}/{max_uses}" if max_uses is not None else f"{uses}/∞"
        if reward_type == "case":
            keys = await db.get_case_keys(message.from_user.id, case_id)
            await message.answer(
                f"✅ Промокод активирован!\n🎟 Получено: {CASES[case_id]['title']} × {case_count}\n"
                f"🔑 Ключей: {keys}\n👥 Активаций: {limit_text}"
            )
        else:
            balance, _ = await db.get_coins(message.from_user.id)
            await message.answer(f"✅ Промокод активирован!\n🎁 Получено: {reward} DC\n🪙 Баланс: {balance} DC\n👥 Активаций: {limit_text}")
    elif status == "already_used":
        await message.answer("❌ Ты уже активировал этот промокод.")
    elif status == "limit_reached":
        await message.answer("❌ Лимит активаций этого промокода исчерпан.")
    else:
        await message.answer("❌ Промокод не найден или уже отключён.")


# =========================
# /transfer — GROUP + PRIVATE
# =========================

@router.message(Command("transfer"))
async def cmd_transfer(message: Message, bot: Bot) -> None:
    sender_id = message.from_user.id

    if await db.is_banned(sender_id):
        await message.reply(BAN_MESSAGE)
        return

    # Запоминаем username отправителя, если он уже есть в user_stats.
    if message.chat.id == MAIN_CHAT_ID and message.from_user.username:
        await db.set_username(sender_id, message.from_user.username, MAIN_CHAT_ID, display_name(message.from_user))

    args = message.text.split()
    target_id = None
    target_name = None

    # Вариант 1: /transfer @username 100
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
                "❌ Пользователь с таким username не найден.\n"
                "Пользователь должен хотя бы один раз написать в основной группе, "
                "чтобы бот знал его ID."
            )
            return
        target_id, target_name = target

    # Вариант 2: ответ на сообщение — /transfer 100
    elif len(args) == 2 and message.reply_to_message and message.reply_to_message.from_user:
        try:
            amount = int(args[1])
        except ValueError:
            await message.reply("❌ Сумма должна быть целым числом.")
            return

        target_user = message.reply_to_message.from_user
        target_id = target_user.id
        target_name = display_name(target_user)

        if target_user.username:
            await db.set_username(target_id, target_user.username, MAIN_CHAT_ID, target_name)

    else:
        await message.reply(
            "❌ Использование:\n"
            "/transfer @username сумма\n"
            "или ответь на сообщение командой /transfer сумма"
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
        await message.reply(
            f"❌ Недостаточно D-COINS.\n🪙 Твой баланс: {balance} DC"
        )
        return

    sender_name = display_name(message.from_user)
    if not target_name:
        target_name = await db.get_user_name(target_id)

    await message.reply(
        f"✅ Перевод выполнен!\n\n"
        f"👤 Получатель: {target_name}\n"
        f"💸 Переведено: {amount} DC\n"
        f"🪙 Твой баланс: {sender_balance} DC"
    )

    try:
        await bot.send_message(
            target_id,
            f"💰 Тебе перевели {amount} D-COINS!\n\n"
            f"👤 От: {sender_name}\n"
            f"🪙 Твой баланс: {recipient_balance} DC"
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
    if message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    user_id = message.from_user.id
    name    = display_name(message.from_user)
    chance, msg_count, last_bonus = await db.get_user(user_id, message.chat.id)
    balance, last_coin_bonus = await db.get_coins(user_id)
    now = time.time()
    is_vip = await db.is_vip(user_id)

    chance_text = ""
    coin_text   = ""

    if now - last_bonus >= BONUS_COOLDOWN:
        bonus_amount = round(random.uniform(0.05, 0.20), 3)
        new_chance   = min(round(chance + bonus_amount, 3), MAX_CHANCE)
        await db.update_user(user_id, message.chat.id, name, new_chance, msg_count, now)
        chance_text = f"📈 Шанс: +{bonus_amount:.3f}% → {new_chance:.3f}%"
    else:
        left = BONUS_COOLDOWN - (now - last_bonus)
        h, m = int(left // 3600), int(left % 3600 // 60)
        chance_text = f"⏳ Шанс-бонус через: {h} ч. {m} мин."

    if now - last_coin_bonus >= COINS_BONUS_CD:
        coins_amount = COINS_VIP_BONUS if is_vip else COINS_BONUS
        new_balance  = await db.add_coins(user_id, coins_amount)
        await db.set_coin_bonus_time(user_id)
        await school_event.record(user_id, name, f"bonus:{message.chat.id}:{message.message_id}", {"bonus": 1})
        coin_text = f"🪙 D-COINS: +{coins_amount} → {new_balance} DC"
    else:
        left = COINS_BONUS_CD - (now - last_coin_bonus)
        h, m = int(left // 3600), int(left % 3600 // 60)
        coin_text = f"⏳ Бонус монет через: {h} ч. {m} мин."

    await message.reply(f"🎁 Ежедневный бонус:\n\n{chance_text}\n{coin_text}")

# =========================
# PRIVATE — CASES
# =========================

@router.message(Command("cases"), F.chat.type == "private")
async def cmd_cases(message: Message) -> None:
    if await db.is_banned(message.from_user.id):
        await message.answer(BAN_MESSAGE)
        return
    await send_cases_menu(message, message.from_user.id)

async def send_cases_menu(message: Message, user_id: int) -> None:
    await message.answer(
        "📦 Выбери кейс:",
        reply_markup=cases_keyboard(),
    )

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
    price_text = "🏆 Эксклюзив: только за ключ" if case.get("key_only") else f"💰 Цена: {case['price']:,} DC".replace(",", " ")
    await callback.message.edit_text(
        f"📦 Кейс {case['title']}\n\n"
        f"{price_text}\n"
        f"🔑 Твоих ключей: {keys}\n\n"
        f"🎁 Возможные награды:\n" + "\n".join(rewards),
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
        await callback.answer("🔑 Этот эксклюзивный кейс открывается только ключом из ивента, квеста или промокода.", show_alert=True)
        return
    if payment == "insufficient":
        case_open_cooldowns.pop(user_id, None)
        balance, _ = await db.get_coins(user_id)
        await callback.answer(f"❌ Нужно {case['price']} DC, у тебя {balance}", show_alert=True)
        return

    if isinstance(case["rewards"][0][0], str):
        if payment == "coins":
            await school_event.record(user_id, display_name(callback.from_user), f"case:{callback.id}", {"case": 1})
        kind, reward, _ = random.choices(case["rewards"], weights=[item[2] for item in case["rewards"]], k=1)[0]
    else:
        reward, _ = random.choices(case["rewards"], weights=[item[1] for item in case["rewards"]], k=1)[0]
        kind = "coins"
    if kind == "coins":
        new_balance = await db.add_coins(user_id, reward)
        prize_text = f"🎉 Выпало: {reward:,} DC".replace(",", " ")
    else:
        gift_key = {15: 5, 25: 10, 50: 15, 100: 20}[reward]
        gift_id = random.choice(GIFT_IDS[gift_key])
        try:
            await bot.send_gift(user_id=user_id, gift_id=gift_id)
            prize_text = f"🎁 Выпал подарок {reward}⭐\n✅ Подарок отправлен в личку!"
        except Exception as e:
            await db.add_pending_gift(user_id, await db.get_user_name(user_id), gift_id, f"кейс {case['title']}: {e}")
            prize_text = f"🎁 Выпал подарок {reward}⭐\n⏳ Добавлен в очередь выдачи."
        new_balance, _ = await db.get_coins(user_id)
    keys = await db.get_case_keys(user_id, case_id)
    payment_text = "🔑 Использован ключ кейса" if payment == "key" else f"💸 Списано: {case['price']:,} DC".replace(",", " ")
    result_text = (
        f"📦 {case['title']} открыт!\n\n"
        f"{prize_text}\n"
        f"{payment_text}\n"
        f"🪙 Баланс: {new_balance:,} DC\n"
        f"🔑 Ключей: {keys}"
    ).replace(",", " ")
    await callback.message.edit_text(
        result_text,
        reply_markup=case_detail_keyboard(case_id),
    )
    await send_game_log(
        bot,
        f"📦 Открыт кейс {case['title']}\n"
        f"👤 {display_name(callback.from_user)} ({user_id})\n"
        f"{payment_text}\n{prize_text}\n"
        f"🪙 Баланс: {new_balance:,} DC".replace(",", " "),
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

# =========================
# GROUP — CASINO
# =========================

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
    if bet < CASINO_MIN_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return

    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n🪙 Твой баланс: {balance} DC")
        return

    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        await message.reply("❌ Недостаточно D-COINS!")
        return
    balance_after, _ = await db.get_coins(user_id)

    SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "⭐", "💎"]
    s1 = random.choice(SYMBOLS)
    s2 = random.choice(SYMBOLS)
    s3 = random.choice(SYMBOLS)

    if s1 == s2 == s3:
        win = bet * 2
        await db.add_coins(user_id, win)
        await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, True)
        new_balance, _ = await db.get_coins(user_id)
        await message.reply(
            f"🎰 {s1} {s2} {s3}\n\n"
            f"✅ Ты выиграл!\n"
            f"💸 Ставка: {bet} DC\n"
            f"🏆 Выигрыш: {win} DC\n"
            f"🪙 Баланс: {new_balance} DC"
        )
        await send_game_log(bot, f"🎰 Слоты\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC\n✅ Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC")
    else:
        boss_note = await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, False)
        balance_after, _ = await db.get_coins(user_id)
        await message.reply(
            f"🎰 {s1} {s2} {s3}\n\n"
            f"❌ Не повезло!\n"
            f"💸 Ставка: {bet} DC\n"
            f"🪙 Баланс: {balance_after} DC{boss_note}"
        )
        await send_game_log(bot, f"🎰 Слоты\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC\n❌ Проигрыш\n🪙 Баланс: {balance_after} DC")


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
    if bet < CASINO_MIN_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return

    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n🪙 Твой баланс: {balance} DC")
        return

    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        await message.reply("❌ Недостаточно D-COINS!")
        return

    result_color = random.choice(["red"] * 18 + ["black"] * 18 + ["green"])
    emoji_map = {"red": "🔴", "black": "⚫", "green": "🟢"}
    result_emoji = emoji_map[result_color]
    chosen_emoji = "🔴" if color == "red" else "⚫"

    if result_color == color:
        win = bet * 2
        await db.add_coins(user_id, win)
        await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, True)
        new_balance, _ = await db.get_coins(user_id)
        await message.reply(
            f"🎡 Выпало: {result_emoji}\n\n"
            f"✅ Ты выиграл!\n"
            f"💸 Ставка: {bet} DC на {chosen_emoji}\n"
            f"🏆 Выигрыш: {win} DC\n"
            f"🪙 Баланс: {new_balance} DC"
        )
        await send_game_log(bot, f"🎡 Рулетка\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {chosen_emoji}\nВыпало: {result_emoji}\n✅ Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC")
    else:
        boss_note = await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, False)
        new_balance, _ = await db.get_coins(user_id)
        await message.reply(
            f"🎡 Выпало: {result_emoji}\n\n"
            f"❌ Не повезло!\n"
            f"💸 Ставка: {bet} DC на {chosen_emoji}\n"
            f"🪙 Баланс: {new_balance} DC{boss_note}"
        )
        await send_game_log(bot, f"🎡 Рулетка\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {chosen_emoji}\nВыпало: {result_emoji}\n❌ Проигрыш\n🪙 Баланс: {new_balance} DC")


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
        bet    = int(args[2])
    except ValueError:
        await message.reply("❌ Число и ставка должны быть числами.")
        return

    if number < 1 or number > 6:
        await message.reply("❌ Число должно быть от 1 до 6.")
        return
    if bet < CASINO_MIN_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return

    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Недостаточно D-COINS!\n🪙 Твой баланс: {balance} DC")
        return

    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    casino_bet_cooldowns[user_id] = True
    if not await db.remove_coins(user_id, bet):
        casino_bet_cooldowns.pop(user_id, None)
        await message.reply("❌ Недостаточно D-COINS!")
        return

    rolled = random.randint(1, 6)

    if rolled == number:
        win = bet * 2
        await db.add_coins(user_id, win)
        await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, True)
        new_balance, _ = await db.get_coins(user_id)
        await message.reply(
            f"🎲 Выпало: {rolled}\n\n"
            f"✅ Угадал!\n"
            f"💸 Ставка: {bet} DC на {number}\n"
            f"🏆 Выигрыш: {win} DC\n"
            f"🪙 Баланс: {new_balance} DC"
        )
        await send_game_log(bot, f"🎲 Кубик\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {number}\nВыпало: {rolled}\n✅ Выигрыш: {win} DC\n🪙 Баланс: {new_balance} DC")
    else:
        boss_note = await school_game(message.from_user, f"game:{message.chat.id}:{message.message_id}", bet, False)
        new_balance, _ = await db.get_coins(user_id)
        await message.reply(
            f"🎲 Выпало: {rolled}\n\n"
            f"❌ Не угадал! (ты выбрал {number})\n"
            f"💸 Ставка: {bet} DC\n"
            f"🪙 Баланс: {new_balance} DC{boss_note}"
        )
        await send_game_log(bot, f"🎲 Кубик\n👤 {display_name(message.from_user)} ({user_id})\n💸 Ставка: {bet} DC на {number}\nВыпало: {rolled}\n❌ Проигрыш\n🪙 Баланс: {new_balance} DC")


# =========================
# PRIVATE — MINES
# =========================

MINES_GRID_SIZE = 25
MINES_COUNT = 6

def mines_multiplier(safe_opened: int) -> float:
    """Коэффициент для поля 5×5 с шестью минами."""
    if safe_opened <= 0:
        return 0.0
    # Первые два коэффициента совпадают с привычной механикой игры.
    if safe_opened == 1:
        return 1.28
    if safe_opened == 2:
        return 1.65
    fair_multiplier = comb(MINES_GRID_SIZE, safe_opened) / comb(MINES_GRID_SIZE - MINES_COUNT, safe_opened)
    return round(fair_multiplier * 0.94, 2)

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
            buttons.append([InlineKeyboardButton(text=f"💸 Забрать {prize:,} DC".replace(",", " "), callback_data="mines_cashout")])
        else:
            buttons.append([InlineKeyboardButton(text="❌", callback_data="mines_cashout")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def mines_text(game: dict) -> str:
    safe_opened = len(game["opened"])
    text = (
        "💣 Ты начал игру «Минное поле»!\n"
        f"💰 Ставка: {game['bet']:,} DC\n"
        f"💣 Мин на поле: {MINES_COUNT}"
    ).replace(",", " ")
    if safe_opened:
        multiplier = mines_multiplier(safe_opened)
        prize = mines_prize(game["bet"], safe_opened)
        text += f"\n💵 Выигрыш: x{multiplier:.2f} | {prize:,} DC".replace(",", " ")
    return text

@serialized_game
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
    if bet < CASINO_MIN_BET:
        await message.reply(f"❌ Минимальная ставка: {CASINO_MIN_BET} DC")
        return
    if user_id in casino_bet_cooldowns:
        await message.reply(f"⏳ Следующая ставка будет доступна через {CASINO_BET_COOLDOWN} сек.")
        return
    if not await db.remove_coins(user_id, bet):
        balance, _ = await db.get_coins(user_id)
        await message.reply(f"❌ Недостаточно D-COINS!\n🪙 Твой баланс: {balance} DC")
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

@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^мины\s+\d+\s*$"))
async def cmd_mines_text(message: Message) -> None:
    await start_mines_game(message)

@router.callback_query(F.data.startswith("mines_cell_"))
@serialized_game
async def mines_open_cell(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game.get("game") != "mines":
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
        balance, _ = await db.get_coins(user_id)
        await callback.message.edit_text(
            "💣 Игра завершена!\n💵 Вы проиграли." + boss_note,
            reply_markup=mines_keyboard(game, reveal=True),
        )
        await send_game_log(
            bot,
            f"💣 Мины\n👤 {display_name(callback.from_user)} ({user_id})\n"
            f"💸 Ставка: {game['bet']} DC\n❌ Проигрыш\n🪙 Баланс: {balance} DC",
        )
        await callback.answer("💥 Мина!")
        return

    game["opened"].add(cell)
    await school_event.record(user_id, display_name(callback.from_user), f"safe:{game['token']}:{cell}", {"safe": 1})
    if len(game["opened"]) == MINES_GRID_SIZE - MINES_COUNT:
        prize = mines_prize(game["bet"], len(game["opened"]))
        active_games.pop(user_id, None)
        balance = await db.add_coins(user_id, prize)
        await school_game(callback.from_user, f"mines:{game['token']}", game["bet"], True)
        await callback.message.edit_text(
            f"🏆 Поле очищено!\n💵 Выигрыш: x{mines_multiplier(len(game['opened'])):.2f} | {prize:,} DC\n🪙 Баланс: {balance:,} DC".replace(",", " "),
            reply_markup=mines_keyboard(game, reveal=True),
        )
        await send_game_log(bot, f"💣 Мины\n👤 {display_name(callback.from_user)} ({user_id})\n💸 Ставка: {game['bet']} DC\n🏆 Поле очищено: +{prize} DC\n🪙 Баланс: {balance} DC")
    else:
        await callback.message.edit_text(mines_text(game), reply_markup=mines_keyboard(game))
    await callback.answer()

@router.callback_query(F.data == "mines_cashout")
@serialized_game
async def mines_cashout(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    game = active_games.get(user_id)
    if not game or game.get("game") != "mines":
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
    balance = await db.add_coins(user_id, prize)
    await school_game(callback.from_user, f"mines:{game['token']}", game["bet"], True)
    await callback.message.edit_text(
        f"✅ Вы забрали выигрыш!\n💵 Выигрыш: x{mines_multiplier(safe_opened):.2f} | {prize:,} DC\n🪙 Баланс: {balance:,} DC".replace(",", " "),
        reply_markup=mines_keyboard(game, reveal=True),
    )
    await send_game_log(
        bot,
        f"💣 Мины\n👤 {display_name(callback.from_user)} ({user_id})\n"
        f"💸 Ставка: {game['bet']} DC\n✅ Забрал: {prize} DC (x{mines_multiplier(safe_opened):.2f})\n"
        f"🪙 Баланс: {balance} DC",
    )
    await callback.answer("✅ Выигрыш зачислен")

@router.callback_query(F.data == "mines_done")
async def mines_done(callback: CallbackQuery) -> None:
    await callback.answer("Игра уже завершена.")


# =========================
# GROUP — /exchange
# =========================

@router.message(Command("exchange"))
async def cmd_exchange(message: Message) -> None:
    if message.chat.type != "private" and message.chat.id != MAIN_CHAT_ID:
        return
    if await db.is_banned(message.from_user.id):
        await message.reply(BAN_MESSAGE)
        return
    balance, _ = await db.get_coins(message.from_user.id)
    await message.reply(
        f"💱 Обмен D-COINS\n\n"
        f"🪙 Твой баланс: {balance} DC\n\n"
        f"Выбери что хочешь получить:",
        reply_markup=exchange_keyboard(balance)
    )

@router.callback_query(F.data == "buy_dc_menu")
async def buy_dc_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⭐ Покупка D-COINS за звёзды\n\n"
        "Актуальные цены указаны на кнопках.\n"
        "Выбери пакет:",
        reply_markup=buy_dc_keyboard(),
    )
    await callback.answer()

@router.callback_query(F.data == "buy_dc_back")
async def buy_dc_back(callback: CallbackQuery) -> None:
    balance, _ = await db.get_coins(callback.from_user.id)
    await callback.message.edit_text(
        f"💱 Обмен D-COINS\n\n🪙 Твой баланс: {balance:,} DC\n\nВыбери что хочешь получить:".replace(",", " "),
        reply_markup=exchange_keyboard(balance),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("buy_dc_"))
async def buy_dc_package(callback: CallbackQuery, bot: Bot) -> None:
    try:
        dc_amount = int(callback.data.removeprefix("buy_dc_"))
    except (AttributeError, ValueError):
        await callback.answer("❌ Пакет не найден.", show_alert=True)
        return
    star_amount = STAR_DC_PACKAGES.get(dc_amount)
    if star_amount is None:
        await callback.answer("❌ Пакет не найден.", show_alert=True)
        return
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"{dc_amount:,} D-COINS".replace(",", " "),
            description=f"Покупка {dc_amount:,} D-COINS за {star_amount}⭐".replace(",", " "),
            payload=f"buy_dc_{dc_amount}_{star_amount}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{dc_amount:,} DC".replace(",", " "), amount=star_amount)],
        )
    except Exception as e:
        logger.warning("Could not create Stars invoice: %s", e)
        await callback.answer("❌ Не удалось создать счёт. Попробуй позже.", show_alert=True)
        return
    await callback.answer("⭐ Счёт отправлен в личные сообщения")

@router.callback_query(F.data == "exch_chance")
async def exch_chance(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    cost = economy_price("chance_price")
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    balance, _ = await db.get_coins(user_id)
    if balance < cost:
        await callback.answer(f"❌ Нужно {cost} DC, у тебя {balance}", show_alert=True)
        return
    if not await db.remove_coins(user_id, cost):
        balance, _ = await db.get_coins(user_id)
        await callback.answer(f"❌ Нужно {cost} DC, у тебя {balance}", show_alert=True)
        return
    chance, msg_count, last_bonus = await db.get_user(user_id, MAIN_CHAT_ID)
    new_chance = min(round(chance + 1.0, 3), MAX_CHANCE)
    name = await db.get_user_name(user_id)
    await db.update_user(user_id, MAIN_CHAT_ID, name, new_chance, msg_count, last_bonus)
    new_balance, _ = await db.get_coins(user_id)
    await callback.message.edit_text(
        f"✅ Обменял {cost} DC на +1% шанса\n"
        f"🪙 Баланс: {new_balance} DC\n"
        f"📈 Новый шанс: {new_chance:.3f}%",
        reply_markup=exchange_keyboard(new_balance)
    )
    await callback.answer()

async def process_exchange_gift(callback: CallbackQuery, cost: int, gift_key: int, reward_label: str, bot: Bot) -> None:
    user_id = callback.from_user.id
    if await db.is_banned(user_id):
        await callback.answer(BAN_MESSAGE, show_alert=True)
        return
    balance, _ = await db.get_coins(user_id)
    if balance < cost:
        await callback.answer(f"❌ Нужно {cost} DC, у тебя {balance}", show_alert=True)
        return
    gift_ids = GIFT_IDS.get(gift_key, [])
    gift_id  = random.choice(gift_ids) if gift_ids else None
    if not gift_id:
        logger.error("No gift IDs configured for exchange gift key %s", gift_key)
        await callback.answer("❌ Этот подарок временно недоступен.", show_alert=True)
        return

    if not await db.remove_coins(user_id, cost):
        balance, _ = await db.get_coins(user_id)
        await callback.answer(f"❌ Нужно {cost} DC, у тебя {balance}", show_alert=True)
        return

    name = await db.get_user_name(user_id)
    new_balance, _ = await db.get_coins(user_id)
    pending_reason = f"обмен {cost} DC → {reward_label}"
    try:
        star_balance = await bot.get_my_star_balance()
        stars_needed = int(reward_label.replace("⭐", "").strip())
    except Exception as e:
        logger.warning("Could not check star balance: %s", e)
        await db.add_pending_gift(user_id, name, gift_id, f"{pending_reason} — не удалось проверить баланс")
        await callback.message.edit_text(
            f"✅ Обменял {cost} DC на подарок {reward_label}\n"
            f"🪙 Баланс: {new_balance} DC\n"
            f"⏳ Подарок будет отправлен позже.",
            reply_markup=exchange_keyboard(new_balance),
        )
        await callback.answer()
        return

    if star_balance.amount < stars_needed:
        await db.add_pending_gift(user_id, name, gift_id, pending_reason)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Недостаточно звёзд!\n\n👤 {name} ({user_id})\n💫 {star_balance.amount}⭐\nДобавлен в /pending",
            )
        except Exception as e:
            logger.warning("Could not notify admin about pending gift: %s", e)
        await callback.message.edit_text(
            f"✅ Обменял {cost} DC на подарок {reward_label}\n"
            f"🪙 Баланс: {new_balance} DC\n"
            f"⏳ Подарок будет отправлен как только пополним баланс.",
            reply_markup=exchange_keyboard(new_balance),
        )
        await callback.answer()
        return

    try:
        await bot.send_gift(user_id=user_id, gift_id=gift_id)
    except Exception as e:
        logger.warning("Exchange gift failed: %s", e)
        await db.add_pending_gift(user_id, name, gift_id, f"{pending_reason} — ошибка: {e}")
        await callback.message.edit_text(
            f"✅ Обменял {cost} DC на подарок {reward_label}\n"
            f"🪙 Баланс: {new_balance} DC\n"
            f"⏳ Подарок будет отправлен позже.",
            reply_markup=exchange_keyboard(new_balance),
        )
        await callback.answer()
        return

    try:
        await send_log(bot, f"🎁 Обмен монет\n\n{name} ({user_id})\n{cost} DC → {reward_label}")
    except Exception as e:
        logger.warning("Could not log exchange gift: %s", e)
    await callback.message.edit_text(
        f"✅ Обменял {cost} DC на подарок {reward_label}\n"
        f"🪙 Баланс: {new_balance} DC\n"
        f"🎁 Подарок отправлен в личку!",
        reply_markup=exchange_keyboard(new_balance),
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
            f"❌ Нужно {cost:,} DC, у тебя {balance:,}".replace(",", " "),
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

    new_balance, _ = await db.get_coins(user_id)
    await callback.message.edit_text(
        f"✅ Заявка #{order_id} на Premium на месяц создана\n"
        f"🪙 Списано: {cost:,} DC\n"
        f"🪙 Баланс: {new_balance:,} DC\n\n"
        "💎 Premium будет выдан вручную в ближайшее время.".replace(",", " "),
        reply_markup=exchange_keyboard(new_balance),
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💎 Новая заявка Premium на месяц\n\n"
            f"#{order_id} | {name} ({user_id})\n"
            f"🪙 {cost:,} DC\n\n"
            f"После выдачи: premiumdone {order_id}".replace(",", " "),
        )
        await send_log(bot, f"💎 Обмен на Premium\n\n#{order_id} | {name} ({user_id})\n{cost:,} DC".replace(",", " "))
    except Exception as e:
        logger.warning("Could not notify about Premium order: %s", e)
    await callback.answer("✅ Заявка создана")


# =========================
# GROUP — DUELS
# =========================

def duel_keyboard(duel_id: str, bet: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⚔️ Принять за {bet:,} DC".replace(",", " "),
            callback_data=f"duel_accept:{duel_id}",
        )],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"duel_cancel:{duel_id}")],
    ])


@router.message(Command("duel"))
@serialized_game
async def cmd_duel(message: Message) -> None:
    if message.chat.id != MAIN_CHAT_ID or message.chat.type not in {"group", "supergroup"}:
        return
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        await message.reply("❌ Дуэль нельзя создать от имени канала.")
        return
    user_id = message.from_user.id
    if await db.is_banned(user_id):
        await message.reply(BAN_MESSAGE)
        return
    args = (message.text or "").split()
    if len(args) not in {2, 3}:
        await message.reply(
            (
                "Использование: дуэль СТАВКА\n"
                "Или: дуэль СТАВКА @username\n"
                "Также можно ответить этой командой на сообщение игрока.\n"
                f"Минимум {DUEL_MIN_BET} DC, максимум {DUEL_MAX_BET:,} DC"
            ).replace(",", " ")
        )
        return
    try:
        bet = int(args[1].replace(" ", ""))
    except ValueError:
        await message.reply("❌ Ставка должна быть целым числом.")
        return
    if bet < DUEL_MIN_BET or bet > DUEL_MAX_BET:
        await message.reply(f"❌ Ставка должна быть от {DUEL_MIN_BET} до {DUEL_MAX_BET:,} DC.".replace(",", " "))
        return
    if user_id in duel_by_user:
        await message.reply("⚔️ У тебя уже есть активная дуэль.")
        return
    if user_id in duel_cooldowns:
        await message.reply(f"⏳ Дуэль доступна раз в {DUEL_COOLDOWN} секунд.")
        return
    balance, _ = await db.get_coins(user_id)
    if balance < bet:
        await message.reply(f"❌ Для дуэли нужно {bet:,} DC, у тебя {balance:,} DC.".replace(",", " "))
        return

    target_id = None
    target_name = None
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot and not reply.sender_chat:
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
            await message.reply("❌ Игрок не найден. Пусть он сначала напишет сообщение в основной чат или вызови его ответом на сообщение.")
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
        if target_id else "Кто примет вызов?"
    )
    sent = await message.reply(
        (
            "⚔️ Вызов на дуэль!\n\n"
            f"Игрок: {challenger_name}\n"
            f"Ставка каждого: {bet:,} DC\n"
            f"Банк победителя: {bet * 2:,} DC\n"
            "Шанс победы: 50/50\n\n"
            f"{target_text}"
        ).replace(",", " "),
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
@serialized_game
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
@serialized_game
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
            await callback.answer(f"❌ Для принятия нужно {bet:,} DC, у тебя {opponent_balance:,} DC.".replace(",", " "), show_alert=True)
        return

    duel_cooldowns[challenger_id] = True
    duel_cooldowns[opponent_id] = True
    release_duel(duel_id)
    for participant, participant_name in ((challenger_id, duel["challenger_name"]), (opponent_id, opponent_name)):
        await school_event.record(participant, participant_name, f"duel:{duel_id}",
            {"duels": 1, "duelwin": int(participant == winner_id), "games": 1,
             "gamewin": int(participant == winner_id), "bets": bet})
    winner_name = duel["challenger_name"] if winner_id == challenger_id else opponent_name
    loser_name = opponent_name if winner_id == challenger_id else duel["challenger_name"]
    winner_balance = challenger_balance if winner_id == challenger_id else opponent_balance
    await callback.message.edit_text(
        (
            "⚔️ Дуэль завершена!\n\n"
            f"{duel['challenger_name']} VS {opponent_name}\n"
            f"Ставка каждого: {bet:,} DC\n"
            f"🏆 Победитель: {winner_name}\n"
            f"💰 Выигрыш: {bet * 2:,} DC\n"
            f"🪙 Баланс победителя: {winner_balance:,} DC\n\n"
            f"{loser_name} проиграл ставку."
        ).replace(",", " ")
    )
    await send_game_log(
        bot,
        (
            "⚔️ Дуэль\n"
            f"{duel['challenger_name']} ({challenger_id}) VS {opponent_name} ({opponent_id})\n"
            f"💸 Ставка: {bet:,} DC с каждого\n"
            f"🏆 Победитель: {winner_name} ({winner_id})\n"
            f"💰 Банк: {bet * 2:,} DC"
        ).replace(",", " "),
    )
    await callback.answer(f"🏆 Победил {winner_name}!")

# Обычные слова вместо команд со слешем. Этот обработчик расположен до
# group_handler, поэтому команды не засчитываются как обычные сообщения.
PRIVATE_PLAIN_COMMANDS = {
    "start", "say", "vip", "unvip", "viplist", "ban",
    "unban", "banlist", "addmsgs", "removemsgs", "addday", "removeday",
    "addcoins", "removecoins", "createpromo", "deletepromo", "createcasepromo",
    "promos", "balance", "popolnit", "sendgift",
    "pending", "deliver", "deletepending", "premiumorders", "premiumdone", "premiumrefund",
    "promo", "cases", "slots", "roulette", "dice", "mines", "admin", "broadcast",
}
GROUP_PLAIN_COMMANDS = {"stats", "top", "winstop", "cointop", "daytop", "bonus", "duel"}
BOT_ARGUMENT_COMMANDS = {
    "start", "say", "balance", "popolnit", "sendgift",
    "createpromo", "createcasepromo",
    "pending", "deliver", "premiumdone", "premiumrefund", "transfer", "slots", "roulette", "dice", "admin",
}
PLAIN_COMMAND_HANDLERS = {
    "start": cmd_start, "help": cmd_help,
    "say": cmd_say, "vip": cmd_vip, "unvip": cmd_unvip, "viplist": cmd_viplist,
    "ban": cmd_ban, "unban": cmd_unban, "banlist": cmd_banlist,
    "addmsgs": cmd_addmsgs, "removemsgs": cmd_removemsgs,
    "addday": cmd_addday, "removeday": cmd_removeday,
    "addcoins": cmd_addcoins, "removecoins": cmd_removecoins,
    "createpromo": cmd_createpromo, "deletepromo": cmd_deletepromo,
    "createcasepromo": cmd_createcasepromo, "promos": cmd_promos,
    "balance": cmd_balance, "popolnit": cmd_popolnit, "sendgift": cmd_sendgift,
    "pending": cmd_pending, "deliver": cmd_deliver, "deletepending": cmd_deletepending,
    "premiumorders": cmd_premiumorders,
    "premiumdone": cmd_premiumdone, "premiumrefund": cmd_premiumrefund,
    "stats": cmd_stats, "top": cmd_top, "winstop": cmd_winstop,
    "cointop": cmd_cointop, "coins": cmd_coins,
    "promo": cmd_promo, "transfer": cmd_transfer, "daytop": cmd_daytop,
    "bonus": cmd_bonus, "cases": cmd_cases, "slots": cmd_slots,
    "roulette": cmd_roulette, "dice": cmd_dice, "mines": cmd_mines,
    "duel": cmd_duel, "exchange": cmd_exchange, "admin": cmd_admin,
    "broadcast": cmd_broadcast,
}

@router.message(is_plain_command)
async def plain_command_handler(message: Message, bot: Bot) -> None:
    parsed = parse_plain_command(message.text)
    if not parsed:
        return
    command, args = parsed
    if command in PRIVATE_PLAIN_COMMANDS and message.chat.type != "private":
        return
    if command in GROUP_PLAIN_COMMANDS and message.chat.id != MAIN_CHAT_ID:
        return

    command_message = message.model_copy(update={"text": "/" + command + (" " + " ".join(args) if args else "")})
    handler = PLAIN_COMMAND_HANDLERS[command]
    if command in BOT_ARGUMENT_COMMANDS:
        await handler(command_message, bot)
    else:
        await handler(command_message)

# =========================
# MAIN GROUP HANDLER
# =========================

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_handler(message: Message, bot: Bot) -> None:
    if message.chat.id != MAIN_CHAT_ID:
        if message.chat.id not in {LOG_CHAT_ID, GAME_LOG_CHAT_ID}:
            try:
                await bot.leave_chat(message.chat.id)
            except Exception as e:
                logger.warning("leave_chat failed: %s", e)
        return

    msg_text = message.text or message.caption
    if not msg_text or msg_text.startswith("/"):
        return

    # Сообщение, отправленное от имени канала, должно учитываться за канал.
    # Проверяем sender_chat первым: Telegram может одновременно передать from_user.
    if message.sender_chat:
        user_id = message.sender_chat.id
        name = message.sender_chat.title or str(message.sender_chat.id)
    elif message.from_user and not message.from_user.is_bot:
        user_id = message.from_user.id
        name = display_name(message.from_user)
    else:
        return

    if message.from_user and message.from_user.username:
        await db.set_username(user_id, message.from_user.username, message.chat.id, name)

    cache_key = (user_id, message.chat.id)

    if await db.is_banned(user_id):
        return

    if cache_key in cooldowns:
        return
    cooldowns[cache_key] = True

    await db.increment_daily(user_id, message.chat.id, name)

    chance, msg_count, last_bonus = await db.get_user(user_id, message.chat.id)
    is_vip = await db.is_vip(user_id)

    # Монеты за сообщение
    if not message.sender_chat and message.from_user and not message.from_user.is_bot:
        await school_event.record(user_id, name, f"message:{message.chat.id}:{message.message_id}", {"messages": 1})
    coins_earned = COINS_VIP_PER_MSG if is_vip else COINS_PER_MSG
    await db.add_coins(user_id, coins_earned)

    # WIN SYSTEM
    msg_step = 1
    chance_step = 0.003 if is_vip else 0.002  # VIP x1.5

    if msg_count < 150:
        is_win = False
    else:
        is_win = gift_roll_wins(chance)

    if is_win:
        await db.add_win(user_id, message.chat.id, name, chance)
        await message.reply(
            f"🏆 Поздравляем, {name}!\n\n"
            f"📈 Шанс был: {chance:.3f}%\n"
            f"🎁 Подарок уже отправлен тебе в личку!"
        )
        await send_log(bot, f"🏆 Победитель\n\n{name} ({user_id})\nШанс: {chance:.3f}%")
        await bot.send_message(ADMIN_ID, f"🏆 Новый победитель\n\n{name} ({user_id})\nШанс: {chance:.3f}%")

        try:
            star_balance = await bot.get_my_star_balance()
            if star_balance.amount < 15:
                await db.add_pending_gift(user_id, name, WIN_GIFT_IDS[0], "победа")
                await send_log(bot, f"⚠️ Недостаточно звёзд!\n\nБаланс: {star_balance.amount}⭐\n{name} ({user_id})\nДобавлен в /pending")
                await bot.send_message(ADMIN_ID,
                    f"⚠️ Недостаточно звёзд!\n\n💫 {star_balance.amount}⭐\n👤 {name} ({user_id})\nДобавлен в /pending"
                )
            else:
                try:
                    await bot.send_gift(user_id=user_id, gift_id=random.choice(WIN_GIFT_IDS))
                    await send_log(bot, f"🎁 Подарок отправлен\n\n{name} ({user_id})\n💫 {star_balance.amount - 15}⭐")
                    await bot.send_message(ADMIN_ID, f"✅ Подарок отправлен!\n\n👤 {name} ({user_id})\n💫 {star_balance.amount - 15}⭐")
                except Exception as e:
                    error_text = str(e)
                    await db.add_pending_gift(user_id, name, WIN_GIFT_IDS[0], f"ошибка: {error_text}")
                    await send_log(bot, f"❌ Ошибка подарка\n\n{name} ({user_id})\n{error_text}")
                    await bot.send_message(ADMIN_ID, f"❌ Ошибка подарка\n\n👤 {name} ({user_id})\n📛 {error_text}\nДобавлен в /pending")
        except Exception as e:
            logger.warning("get_my_star_balance failed: %s", e)
            await db.add_pending_gift(user_id, name, WIN_GIFT_IDS[0], "не удалось проверить баланс")
            await bot.send_message(ADMIN_ID, f"⚠️ Не удалось проверить баланс\n\n👤 {name} ({user_id})\nДобавлен в /pending")

        await db.update_user(user_id, message.chat.id, name, START_CHANCE, 0, last_bonus)
    else:
        new_chance = min(round(chance + chance_step, 3), MAX_CHANCE)
        await db.update_user(user_id, message.chat.id, name, new_chance, msg_count + 1, last_bonus)

# =========================
# DAILY RESET TASK
# =========================

async def daily_reset_task(bot: Bot) -> None:
    tz = pytz.timezone("Europe/Moscow")
    while True:
        now = datetime.now(tz)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds())
        await db.clear_old_daily()
        await send_log(bot, "🗑 Дневная статистика сброшена (00:00 МСК)")

# =========================
# MAIN
# =========================

async def main() -> None:
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env")
    await db.init()
    await school_event.init()
    await load_runtime_settings()
    bot = Bot(token=TOKEN)
    dp  = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(daily_reset_task(bot))
    asyncio.create_task(casino_timeout_checker(bot))
    asyncio.create_task(duel_timeout_checker(bot))
    asyncio.create_task(school_notifications(bot))
    asyncio.create_task(bonus_notification_worker(bot))
    logger.info("Бот запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(main())
