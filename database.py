import aiosqlite
import time
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from config import DB_PATH

from contextlib import asynccontextmanager

@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db

async def init_db():
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                auth_type TEXT NOT NULL DEFAULT 'api_key', -- 'api_key' or 'oauth'
                api_key TEXT,
                oauth_client_id TEXT,
                oauth_client_secret TEXT,
                oauth_refresh_token TEXT,
                oauth_access_token TEXT,
                oauth_token_expiry REAL DEFAULT 0.0,
                is_active INTEGER DEFAULT 1,
                cooldown_until REAL DEFAULT 0.0,
                rpm_limit INTEGER DEFAULT 15,
                last_used_at REAL DEFAULT 0.0,
                requests_today INTEGER DEFAULT 0,
                last_reset_date TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                original_filepath TEXT NOT NULL,
                total_pages INTEGER DEFAULT 0,
                processed_pages INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending', -- pending, rendering, processing, completed, paused, failed
                model TEXT DEFAULT 'gemini-2.5-flash',
                lang_pref TEXT DEFAULT 'traditional',
                direction_pref TEXT DEFAULT 'auto',
                column_pref TEXT DEFAULT 'auto',
                custom_prompt TEXT DEFAULT '',
                start_page INTEGER DEFAULT 1,
                end_page INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # 資料表遷移檢查
        cursor = await db.execute("PRAGMA table_info(accounts)")
        acc_cols = [row["name"] for row in await cursor.fetchall()]
        if "is_paid" not in acc_cols:
            await db.execute("ALTER TABLE accounts ADD COLUMN is_paid INTEGER DEFAULT 0")

        cursor = await db.execute("PRAGMA table_info(tasks)")
        cols = [row["name"] for row in await cursor.fetchall()]
        if "start_page" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN start_page INTEGER DEFAULT 1")
        if "end_page" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN end_page INTEGER DEFAULT 0")
        if "is_paid" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN is_paid INTEGER DEFAULT 0")
        if "paid_account_id" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN paid_account_id INTEGER DEFAULT NULL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                page_num INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                status TEXT DEFAULT 'pending', -- pending, processing, completed, failed
                ocr_text TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                used_account_id INTEGER,
                duration_seconds REAL DEFAULT 0.0,
                used_model TEXT DEFAULT '',
                updated_at REAL NOT NULL,
                UNIQUE(task_id, page_num),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        """)

        cursor = await db.execute("PRAGMA table_info(task_pages)")
        page_cols = [row["name"] for row in await cursor.fetchall()]
        if "used_model" not in page_cols:
            await db.execute("ALTER TABLE task_pages ADD COLUMN used_model TEXT DEFAULT ''")

        # 啟動時自動重置上次非正常中斷而殘留為 processing 的頁面
        now = time.time()
        await db.execute("""
            UPDATE task_pages 
            SET status = 'pending', error_message = '前次轉譯中斷，已重設等待重辨', updated_at = ?
            WHERE status = 'processing'
        """, (now,))

        await db.commit()

async def reset_orphan_processing_pages() -> int:
    """伺服器啟動時將所有處於 processing 的中斷頁面重設為 pending，避免卡死"""
    now = time.time()
    async with get_db() as db:
        cursor = await db.execute("""
            UPDATE task_pages 
            SET status = 'pending', error_message = '前次轉譯中斷，已重設等待重辨', updated_at = ?
            WHERE status = 'processing'
        """, (now,))
        await db.commit()
        return cursor.rowcount

# --- Accounts CRUD ---

async def add_api_key_account(name: str, api_key: str, rpm_limit: int = 15, is_paid: int = 0) -> int:
    today_str = datetime.date.today().isoformat()
    now = time.time()
    # 付費金鑰享有更高的預設 RPM（例如 1000）
    actual_rpm = 1000 if is_paid else rpm_limit
    async with get_db() as db:
        cursor = await db.execute("""
            INSERT INTO accounts (name, auth_type, api_key, rpm_limit, is_paid, last_reset_date, created_at)
            VALUES (?, 'api_key', ?, ?, ?, ?, ?)
        """, (name, api_key.strip(), actual_rpm, 1 if is_paid else 0, today_str, now))
        await db.commit()
        return cursor.lastrowid

async def add_oauth_account(name: str, client_id: str, client_secret: str, refresh_token: str) -> int:
    today_str = datetime.date.today().isoformat()
    now = time.time()
    async with get_db() as db:
        cursor = await db.execute("""
            INSERT INTO accounts (name, auth_type, oauth_client_id, oauth_client_secret, oauth_refresh_token, last_reset_date, created_at)
            VALUES (?, 'oauth', ?, ?, ?, ?, ?)
        """, (name, client_id.strip(), client_secret.strip(), refresh_token.strip(), today_str, now))
        await db.commit()
        return cursor.lastrowid

async def get_accounts() -> List[Dict[str, Any]]:
    today_str = datetime.date.today().isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM accounts ORDER BY id ASC")
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            # Reset daily requests count if day changed
            if d.get("last_reset_date") != today_str:
                d["requests_today"] = 0
                await db.execute("UPDATE accounts SET requests_today = 0, last_reset_date = ? WHERE id = ?", (today_str, d["id"]))
            
            # Mask sensitive info for UI
            if d.get("api_key"):
                raw = d["api_key"]
                if len(raw) > 8:
                    d["masked_key"] = raw[:4] + "...." + raw[-4:]
                else:
                    d["masked_key"] = "****"
            result.append(d)
        await db.commit()
        return result

async def get_account_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def toggle_account_active(account_id: int, is_active: bool):
    async with get_db() as db:
        await db.execute("UPDATE accounts SET is_active = ? WHERE id = ?", (1 if is_active else 0, account_id))
        await db.commit()

async def delete_account(account_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        await db.commit()

async def update_account_usage(account_id: int, set_cooldown_seconds: float = 0.0):
    now = time.time()
    today_str = datetime.date.today().isoformat()
    async with get_db() as db:
        if set_cooldown_seconds > 0:
            cooldown_until = now + set_cooldown_seconds
            await db.execute("""
                UPDATE accounts 
                SET last_used_at = ?, 
                    requests_today = requests_today + 1,
                    cooldown_until = ?
                WHERE id = ?
            """, (now, cooldown_until, account_id))
        else:
            await db.execute("""
                UPDATE accounts 
                SET last_used_at = ?, 
                    requests_today = requests_today + 1
                WHERE id = ?
            """, (now, account_id))
        await db.commit()

async def set_account_cooldown(account_id: int, cooldown_seconds: float):
    now = time.time()
    cooldown_until = now + cooldown_seconds
    async with get_db() as db:
        await db.execute("UPDATE accounts SET cooldown_until = ? WHERE id = ?", (cooldown_until, account_id))
        await db.commit()

# --- Tasks CRUD ---

async def create_task(
    task_id: str, 
    filename: str, 
    filepath: str, 
    model: str, 
    lang: str, 
    direction: str, 
    column: str, 
    custom_prompt: str = "",
    start_page: int = 1,
    end_page: int = 0,
    is_paid: int = 0,
    paid_account_id: Optional[int] = None
):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, model, lang_pref, direction_pref, column_pref, custom_prompt, start_page, end_page, is_paid, paid_account_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, filename, filepath, model, lang, direction, column, custom_prompt, start_page, end_page, is_paid, paid_account_id, now, now))
        await db.commit()

async def delete_task(task_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM task_pages WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()

async def update_task_model(task_id: str, model: str):
    now = time.time()
    async with get_db() as db:
        await db.execute("UPDATE tasks SET model = ?, updated_at = ? WHERE id = ?", (model, now, task_id))
        await db.commit()

async def update_task_status(task_id: str, status: str, total_pages: Optional[int] = None, processed_pages: Optional[int] = None, model: Optional[str] = None):
    now = time.time()
    async with get_db() as db:
        updates = ["status = ?", "updated_at = ?"]
        params = [status, now]
        if total_pages is not None:
            updates.append("total_pages = ?")
            params.append(total_pages)
        if processed_pages is not None:
            updates.append("processed_pages = ?")
            params.append(processed_pages)
        if model:
            updates.append("model = ?")
            params.append(model)
        params.append(task_id)
        
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"
        await db.execute(query, tuple(params))
        await db.commit()

async def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def list_tasks() -> List[Dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

# --- Task Pages CRUD ---

async def create_task_pages(pages_data: List[Dict[str, Any]]):
    now = time.time()
    async with get_db() as db:
        for page in pages_data:
            await db.execute("""
                INSERT OR IGNORE INTO task_pages (task_id, page_num, image_path, status, updated_at)
                VALUES (?, ?, ?, 'pending', ?)
            """, (page["task_id"], page["page_num"], page["image_path"], now))
        await db.commit()

async def get_task_pages(task_id: str) -> List[Dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM task_pages WHERE task_id = ? ORDER BY page_num ASC", (task_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def update_page_result(task_id: str, page_num: int, status: str, ocr_text: str = "", error_message: str = "", account_id: Optional[int] = None, duration: float = 0.0, used_model: str = ""):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            UPDATE task_pages 
            SET status = ?, ocr_text = ?, error_message = ?, used_account_id = ?, duration_seconds = ?, used_model = ?, updated_at = ?
            WHERE task_id = ? AND page_num = ?
        """, (status, ocr_text, error_message, account_id, duration, used_model, now, task_id, page_num))
        
        # Calculate processed pages
        cursor = await db.execute("SELECT COUNT(*) as count FROM task_pages WHERE task_id = ? AND status = 'completed'", (task_id,))
        row = await cursor.fetchone()
        completed_count = row["count"] if row else 0
        
        await db.execute("UPDATE tasks SET processed_pages = ?, updated_at = ? WHERE id = ?", (completed_count, now, task_id))
        await db.commit()

async def save_page_ocr_text(task_id: str, page_num: int, new_text: str):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            UPDATE task_pages 
            SET ocr_text = ?, updated_at = ?
            WHERE task_id = ? AND page_num = ?
        """, (new_text, now, task_id, page_num))
        await db.commit()

async def update_page_status(task_id: str, page_num: int, status: str, error_message: str = ""):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            UPDATE task_pages 
            SET status = ?, error_message = ?, updated_at = ?
            WHERE task_id = ? AND page_num = ?
        """, (status, error_message, now, task_id, page_num))
        await db.commit()
