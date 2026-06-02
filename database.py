import sqlite3
import json
import os

DB_FILE = os.path.join(os.path.dirname(__file__), "sniper.db")


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                vinted_user TEXT,
                cookies TEXT
            );

            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                keyword TEXT,
                max_price REAL,
                action TEXT DEFAULT 'notify',
                offer_price REAL,
                guild_id TEXT,
                channel_id TEXT,
                UNIQUE(discord_id, keyword)
            );
        """)
        # Migration: Spalten hinzufügen falls sie noch nicht existieren
        for col in ("guild_id TEXT", "channel_id TEXT"):
            try:
                conn.execute(f"ALTER TABLE searches ADD COLUMN {col}")
            except Exception:
                pass
        for col in ("delivery_type TEXT DEFAULT 'home'", "pickup_name TEXT"):
            try:
                conn.execute(f"ALTER TABLE payment_info ADD COLUMN {col}")
            except Exception:
                pass
        conn.executescript("""

            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT,
                discord_id TEXT,
                PRIMARY KEY (item_id, discord_id)
            );

            CREATE TABLE IF NOT EXISTS payment_info (
                discord_id TEXT PRIMARY KEY,
                full_name TEXT,
                street TEXT,
                city TEXT,
                postal_code TEXT,
                country TEXT DEFAULT 'DE',
                delivery_type TEXT DEFAULT 'home',
                pickup_name TEXT
            );

            CREATE TABLE IF NOT EXISTS blocked_sellers (
                discord_id TEXT,
                seller_id TEXT,
                PRIMARY KEY (discord_id, seller_id)
            );
        """)


# ── Users ──────────────────────────────────────────────────────────────────────

def save_user(discord_id: str, vinted_user: str, cookies: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (discord_id, vinted_user, cookies) VALUES (?, ?, ?)",
            (str(discord_id), vinted_user, json.dumps(cookies)),
        )


def get_user(discord_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
    if row:
        return {
            "discord_id": row["discord_id"],
            "vinted_user": row["vinted_user"],
            "cookies": json.loads(row["cookies"]),
        }
    return None


def delete_user(discord_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE discord_id = ?", (str(discord_id),))
        conn.execute("DELETE FROM searches WHERE discord_id = ?", (str(discord_id),))
        conn.execute("DELETE FROM payment_info WHERE discord_id = ?", (str(discord_id),))


# ── Searches ──────────────────────────────────────────────────────────────────

def add_search(discord_id: str, keyword: str, max_price: float, action: str = "notify", offer_price: float = None, guild_id: str = None, channel_id: str = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO searches (discord_id, keyword, max_price, action, offer_price, guild_id, channel_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(discord_id), keyword, max_price, action, offer_price, guild_id, channel_id),
        )


def remove_search(discord_id: str, keyword: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM searches WHERE discord_id = ? AND LOWER(keyword) = LOWER(?)",
            (str(discord_id), keyword),
        )


def get_searches(discord_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM searches WHERE discord_id = ?", (str(discord_id),)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_searches() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM searches").fetchall()
    return [dict(r) for r in rows]


# ── Seen Items ────────────────────────────────────────────────────────────────

def is_seen(item_id: str, discord_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ? AND discord_id = ?",
            (str(item_id), str(discord_id)),
        ).fetchone()
    return row is not None


def mark_seen(item_id: str, discord_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, discord_id) VALUES (?, ?)",
            (str(item_id), str(discord_id)),
        )


# ── Blocked Sellers ───────────────────────────────────────────────────────────

def block_seller(discord_id: str, seller_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blocked_sellers (discord_id, seller_id) VALUES (?, ?)",
            (str(discord_id), str(seller_id)),
        )


def is_blocked(discord_id: str, seller_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_sellers WHERE discord_id = ? AND seller_id = ?",
            (str(discord_id), str(seller_id)),
        ).fetchone()
    return row is not None


# ── Payment Info ──────────────────────────────────────────────────────────────

def save_payment_info(discord_id: str, full_name: str, street: str, city: str, postal_code: str,
                      country: str = "DE", delivery_type: str = "home", pickup_name: str = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO payment_info "
            "(discord_id, full_name, street, city, postal_code, country, delivery_type, pickup_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(discord_id), full_name, street, city, postal_code, country, delivery_type, pickup_name),
        )


def get_payment_info(discord_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM payment_info WHERE discord_id = ?", (str(discord_id),)
        ).fetchone()
    return dict(row) if row else None
