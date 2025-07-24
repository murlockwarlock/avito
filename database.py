import sqlite3
import logging
from datetime import datetime, timezone

DB_FILE = 'avito_manager.sqlite'
logger = logging.getLogger(__name__)


def init_database():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        client_id TEXT NOT NULL,
                        client_secret TEXT NOT NULL,
                        profile_id TEXT NOT NULL,
                        notification_chat_id INTEGER NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        ai_mode INTEGER NOT NULL DEFAULT 0,
                        ai_reply_delay INTEGER,
                        ai_provider TEXT DEFAULT 'openai',
                        prompt_id_limited INTEGER,
                        prompt_id_full INTEGER,
                        default_category_id INTEGER,
                        auto_reply_template_id INTEGER,
                        FOREIGN KEY (prompt_id_limited) REFERENCES prompts (id) ON DELETE SET NULL,
                        FOREIGN KEY (prompt_id_full) REFERENCES prompts (id) ON DELETE SET NULL,
                        FOREIGN KEY (default_category_id) REFERENCES response_categories (id) ON DELETE SET NULL,
                        FOREIGN KEY (auto_reply_template_id) REFERENCES canned_responses (id) ON DELETE SET NULL
                    )
                ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS response_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS canned_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_name TEXT NOT NULL UNIQUE,
                response_text TEXT NOT NULL,
                category_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES response_categories (id) ON DELETE SET NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                account_id INTEGER NOT NULL,
                avito_chat_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                reply_type TEXT,
                message_text TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                prompt_text TEXT NOT NULL
            )
        ''')
        conn.commit()
        logger.info("База данных успешно инициализирована.")


def add_account(data):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO accounts (name, client_id, client_secret, profile_id, notification_chat_id) VALUES (?, ?, ?, ?, ?)",
            (data['name'], data['client_id'], data['client_secret'], data['profile_id'], data['chat_id'])
        )
        conn.commit()


def get_accounts(active_only=False):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        base_query = """
            SELECT
                a.*,
                p_lim.name as prompt_name_limited,
                p_full.name as prompt_name_full
            FROM accounts a
            LEFT JOIN prompts p_lim ON a.prompt_id_limited = p_lim.id
            LEFT JOIN prompts p_full ON a.prompt_id_full = p_full.id
        """
        if active_only:
            query = f"{base_query} WHERE a.is_active = 1 ORDER BY a.id DESC"
        else:
            query = f"{base_query} ORDER BY a.id DESC"
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]


def get_account_by_id(account_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                a.*,
                p_lim.name as prompt_name_limited,
                p_lim.prompt_text as prompt_text_limited,
                p_full.name as prompt_name_full,
                p_full.prompt_text as prompt_text_full,
                rc.name as default_category_name,
                cr.short_name as auto_reply_template_name
            FROM accounts a
            LEFT JOIN prompts p_lim ON a.prompt_id_limited = p_lim.id
            LEFT JOIN prompts p_full ON a.prompt_id_full = p_full.id
            LEFT JOIN response_categories rc ON a.default_category_id = rc.id
            LEFT JOIN canned_responses cr ON a.auto_reply_template_id = cr.id
            WHERE a.id = ?
        """, (account_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_account(account_id, field, value):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if value is None:
            cursor.execute(f"UPDATE accounts SET {field} = NULL WHERE id = ?", (account_id,))
        else:
            cursor.execute(f"UPDATE accounts SET {field} = ? WHERE id = ?", (value, account_id))
        conn.commit()


def delete_account(account_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()


def add_canned_response(short_name, text, category_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO canned_responses (short_name, response_text, category_id) VALUES (?, ?, ?)",
                       (short_name, text, category_id))
        conn.commit()

def update_canned_response(response_id, field, value):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE canned_responses SET {field} = ? WHERE id = ?", (value, response_id))
        conn.commit()

def get_canned_responses():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cr.id, cr.short_name, cr.response_text, rc.name as category_name
            FROM canned_responses cr
            LEFT JOIN response_categories rc ON cr.category_id = rc.id
            ORDER BY rc.name, cr.short_name
        """)
        return [dict(row) for row in cursor.fetchall()]


def get_canned_responses_by_category(category_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM canned_responses WHERE category_id = ? ORDER BY short_name", (category_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_canned_response_by_id(response_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM canned_responses WHERE id = ?", (response_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_canned_response(response_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("DELETE FROM canned_responses WHERE id = ?", (response_id,))
        conn.commit()


def add_category(name):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO response_categories (name) VALUES (?)", (name,))
        conn.commit()


def get_categories():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM response_categories ORDER BY name")
        return [dict(row) for row in cursor.fetchall()]


def delete_category(category_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM response_categories WHERE id = ?", (category_id,))
        conn.commit()


def log_message(account_id, avito_chat_id, direction, reply_type, message_text):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO statistics (account_id, avito_chat_id, direction, reply_type, message_text, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, avito_chat_id, direction, reply_type, message_text, timestamp)
        )
        conn.commit()


def get_stats_for_period(period: str):
    if period == 'day':
        period_filter = "timestamp >= datetime('now', '-1 day', 'localtime')"
    elif period == 'week':
        period_filter = "timestamp >= datetime('now', '-7 days', 'localtime')"
    else:
        period_filter = "timestamp >= datetime('now', '-30 days', 'localtime')"

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = f"""
            SELECT s.*, a.name as account_name FROM statistics s
            LEFT JOIN accounts a ON s.account_id = a.id
            WHERE {period_filter} ORDER BY s.timestamp DESC
        """
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]

def get_prompts():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM prompts ORDER BY name")
        return [dict(row) for row in cursor.fetchall()]

def add_prompt(name, text):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO prompts (name, prompt_text) VALUES (?, ?)", (name, text))
        conn.commit()

def update_prompt(prompt_id, field, value):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE prompts SET {field} = ? WHERE id = ?", (value, prompt_id))
        conn.commit()

def delete_prompt(prompt_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
        conn.commit()

def get_account_by_profile_id(profile_id):
 with sqlite3.connect(DB_FILE) as conn:
     conn.row_factory = sqlite3.Row
     cursor = conn.cursor()
     cursor.execute("SELECT * FROM accounts WHERE profile_id = ?", (profile_id,))
     row = cursor.fetchone()
     return dict(row) if row else None