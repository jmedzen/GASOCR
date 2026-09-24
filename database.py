import aiosqlite
import time
import datetime
import hashlib
import secrets
import hmac
import os
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
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

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
                model TEXT DEFAULT 'gemini-3.5-flash-lite',
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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS file_hashes (
                file_hash TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                created_at REAL NOT NULL
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
        if "pdf_total_pages" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN pdf_total_pages INTEGER DEFAULT 0")
        if "bg_render_status" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN bg_render_status TEXT DEFAULT 'idle'")
        if "file_hash" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN file_hash TEXT DEFAULT ''")
        if "rendered_pages" not in cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN rendered_pages INTEGER DEFAULT 0")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                page_num INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                status TEXT DEFAULT 'pending', -- pending, processing, completed, failed, rendered
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

        await db.commit()

    # 伺服器啟動時自動修復中斷與殘留任務
    await recover_stale_tasks_on_startup()

    # 檢查環境變數是否提供 ADMIN_PASSWORD，若有且尚未初始化則自動完成設定
    env_admin_pwd = os.environ.get("ADMIN_PASSWORD")
    if env_admin_pwd and not await is_admin_initialized():
        await init_admin_password(env_admin_pwd.strip())
        print("🔐 [GASOCR Security] 已依據環境變數 ADMIN_PASSWORD 自動初始化管理員密碼")

    # 自動建立 uploads 目錄現有檔案的 Hash 索引
    await index_existing_uploads()

async def recover_stale_tasks_on_startup() -> Dict[str, int]:
    """
    伺服器重啟或崩潰時的任務自動修復 (Stale Task Recovery)：
    1. 修復掛在 processing / rendering 的 tasks：
       - 若該任務的所有頁面皆已完成 (total_pages > 0 且 completed >= total)，自動標記為 completed
       - 否則標記為 paused (已暫停，可由使用者按「續傳」無縫接續)
       - 若 bg_render_status 為 rendering，重置為 paused
    2. 修復掛在 processing 的 task_pages：
       - 自動標記為 paused，並註記 error_message = '伺服器重新啟動中斷，已轉為暫停狀態以供續傳'
    回傳修復統計字典：{"tasks_paused": int, "tasks_completed": int, "pages_recovered": int}
    """
    now = time.time()
    stats = {
        "tasks_paused": 0,
        "tasks_completed": 0,
        "pages_recovered": 0
    }
    
    async with get_db() as db:
        # 1. 處理殘留為 processing 的 task_pages
        cursor = await db.execute("""
            UPDATE task_pages 
            SET status = 'paused', 
                error_message = '伺服器重新啟動中斷，已轉為暫停狀態以供續傳', 
                updated_at = ?
            WHERE status = 'processing'
        """, (now,))
        stats["pages_recovered"] = cursor.rowcount
        
        # 2. 檢索掛在 processing 或 rendering 的 tasks
        cursor = await db.execute("""
            SELECT id, total_pages, processed_pages, status, bg_render_status
            FROM tasks
            WHERE status IN ('processing', 'rendering')
        """)
        stale_tasks = await cursor.fetchall()
        
        for task in stale_tasks:
            t_id = task["id"]
            # 統計該任務實際頁面完成情況
            page_cur = await db.execute("""
                SELECT 
                    COUNT(*) as total_count,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_count
                FROM task_pages
                WHERE task_id = ?
            """, (t_id,))
            page_stat = await page_cur.fetchone()
            total_count = page_stat["total_count"] if page_stat else 0
            completed_count = page_stat["completed_count"] if page_stat else 0
            
            # 若所有頁面實際上已全數完成
            if total_count > 0 and completed_count >= total_count:
                await db.execute("""
                    UPDATE tasks 
                    SET status = 'completed', processed_pages = total_pages, updated_at = ?
                    WHERE id = ?
                """, (now, t_id))
                stats["tasks_completed"] += 1
            else:
                new_bg = 'paused' if task["bg_render_status"] == 'rendering' else task["bg_render_status"]
                await db.execute("""
                    UPDATE tasks 
                    SET status = 'paused', bg_render_status = ?, updated_at = ?
                    WHERE id = ?
                """, (new_bg, now, t_id))
                stats["tasks_paused"] += 1
                
        await db.commit()
        
    return stats

def compute_file_sha256(filepath: Any) -> str:
    """計算檔案的 SHA-256 哈希值"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

async def save_file_hash(file_hash: str, filename: str, filepath: str, file_size: int = 0):
    """儲存或更新檔案 Hash 索引"""
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO file_hashes (file_hash, filename, filepath, file_size, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (file_hash, filename, str(filepath), file_size, now))
        await db.commit()

async def get_file_by_hash(file_hash: str) -> Optional[Dict[str, Any]]:
    """根據 Hash 查詢是否已有快取檔案，並驗證實體檔案是否依然存在"""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM file_hashes WHERE file_hash = ?", (file_hash,))
        row = await cursor.fetchone()
        if row:
            d = dict(row)
            if Path(d["filepath"]).exists():
                return d
        return None

async def delete_file_hash_by_path(filepath: str):
    """清理失效檔案路徑的 Hash 記錄"""
    async with get_db() as db:
        await db.execute("DELETE FROM file_hashes WHERE filepath = ?", (str(filepath),))
        await db.commit()

async def index_existing_uploads():
    """伺服器啟動時，自動為 uploads 目錄內的現有檔案建立 Hash 索引"""
    from config import UPLOADS_DIR
    upload_path = Path(UPLOADS_DIR)
    if not upload_path.exists():
        return
    for f in upload_path.glob("*.pdf"):
        try:
            h = compute_file_sha256(f)
            await save_file_hash(h, f.name, str(f), f.stat().st_size)
        except Exception as e:
            print(f"Error indexing upload {f}: {e}")

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

async def get_accounts(include_secrets: bool = False) -> List[Dict[str, Any]]:
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
            elif d.get("oauth_client_id"):
                raw = d["oauth_client_id"]
                if len(raw) > 8:
                    d["masked_key"] = raw[:4] + "...." + raw[-4:]
                else:
                    d["masked_key"] = "OAuth Token"

            if not include_secrets:
                d.pop("api_key", None)
                d.pop("oauth_client_secret", None)
                d.pop("oauth_refresh_token", None)
                if d.get("oauth_client_id"):
                    raw = d["oauth_client_id"]
                    d["oauth_client_id"] = (raw[:4] + "...." + raw[-4:]) if len(raw) > 8 else "****"

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
    paid_account_id: Optional[int] = None,
    pdf_total_pages: int = 0,
    file_hash: str = ""
):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, model, lang_pref, direction_pref, column_pref, custom_prompt, start_page, end_page, is_paid, paid_account_id, pdf_total_pages, file_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, filename, filepath, model, lang, direction, column, custom_prompt, start_page, end_page, is_paid, paid_account_id, pdf_total_pages, file_hash, now, now))
        await db.commit()

async def delete_task(task_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM task_pages WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()

async def delete_tasks(task_ids: List[str]):
    """批次從資料庫中刪除多筆任務與對應的頁面記錄"""
    if not task_ids:
        return
    async with get_db() as db:
        placeholders = ",".join("?" for _ in task_ids)
        await db.execute(f"DELETE FROM task_pages WHERE task_id IN ({placeholders})", task_ids)
        await db.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
        await db.commit()

async def update_task_model(task_id: str, model: str):
    now = time.time()
    async with get_db() as db:
        await db.execute("UPDATE tasks SET model = ?, updated_at = ? WHERE id = ?", (model, now, task_id))
        await db.commit()

async def update_task_status(
    task_id: str, 
    status: Optional[str] = None, 
    total_pages: Optional[int] = None, 
    processed_pages: Optional[int] = None, 
    model: Optional[str] = None,
    pdf_total_pages: Optional[int] = None,
    bg_render_status: Optional[str] = None,
    rendered_pages: Optional[int] = None
):
    now = time.time()
    async with get_db() as db:
        updates = ["updated_at = ?"]
        params = [now]
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if total_pages is not None:
            updates.append("total_pages = ?")
            params.append(total_pages)
        if processed_pages is not None:
            updates.append("processed_pages = ?")
            params.append(processed_pages)
        if rendered_pages is not None:
            updates.append("rendered_pages = ?")
            params.append(rendered_pages)
        if model:
            updates.append("model = ?")
            params.append(model)
        if pdf_total_pages is not None:
            updates.append("pdf_total_pages = ?")
            params.append(pdf_total_pages)
        if bg_render_status is not None:
            updates.append("bg_render_status = ?")
            params.append(bg_render_status)
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
            initial_status = page.get("status", "pending")
            await db.execute("""
                INSERT OR IGNORE INTO task_pages (task_id, page_num, image_path, status, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (page["task_id"], page["page_num"], page["image_path"], initial_status, now))
        await db.commit()

async def update_page_image_and_status(task_id: str, page_num: int, image_path: str, status: str = "pending"):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            UPDATE task_pages 
            SET image_path = ?, status = ?, updated_at = ?
            WHERE task_id = ? AND page_num = ?
        """, (image_path, status, now, task_id, page_num))
        await db.commit()

async def add_background_rendered_pages(pages_data: List[Dict[str, Any]]):
    now = time.time()
    async with get_db() as db:
        for page in pages_data:
            await db.execute("""
                INSERT OR IGNORE INTO task_pages (task_id, page_num, image_path, status, updated_at)
                VALUES (?, ?, ?, 'rendered', ?)
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

# --- Password & System Settings ---

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return pwd_hash, salt

def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return hmac.compare_digest(pwd_hash, stored_hash)

async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with get_db() as db:
        cursor = await db.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

async def set_setting(key: str, value: str):
    now = time.time()
    async with get_db() as db:
        await db.execute("""
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (key, value, now))
        await db.commit()

async def is_admin_initialized() -> bool:
    admin_hash = await get_setting("admin_password_hash")
    return bool(admin_hash)

async def init_admin_password(password: str) -> bool:
    """初始化管理員密碼（僅在未初始化時有效）"""
    if await is_admin_initialized():
        return False
    pwd_hash, salt = hash_password(password)
    await set_setting("admin_password_hash", pwd_hash)
    await set_setting("admin_salt", salt)
    if not await get_setting("session_secret"):
        await set_setting("session_secret", secrets.token_hex(32))
    return True

async def verify_admin_password(password: str) -> bool:
    stored_hash = await get_setting("admin_password_hash")
    salt = await get_setting("admin_salt")
    if not stored_hash or not salt:
        return False
    return verify_password(password, stored_hash, salt)

async def change_admin_password(old_password: str, new_password: str) -> tuple[bool, str]:
    if not await verify_admin_password(old_password):
        return False, "原管理員密碼輸入錯誤"
    if len(new_password) < 4:
        return False, "新密碼長度至少需 4 個字元"
    pwd_hash, salt = hash_password(new_password)
    await set_setting("admin_password_hash", pwd_hash)
    await set_setting("admin_salt", salt)
    return True, "管理員密碼修改成功"

async def is_access_gate_enabled() -> bool:
    val = await get_setting("access_gate_enabled", "0")
    return val == "1"

async def has_access_password() -> bool:
    val = await get_setting("access_password_hash")
    return bool(val)

async def set_access_gate(enabled: bool, password: Optional[str] = None) -> tuple[bool, str]:
    if password is not None and len(password.strip()) > 0:
        pwd_hash, salt = hash_password(password.strip())
        await set_setting("access_password_hash", pwd_hash)
        await set_setting("access_salt", salt)
        await set_setting("access_gate_enabled", "1" if enabled else "0")
        return True, "通關密碼與保護設定已更新"
    elif enabled:
        if not await has_access_password():
            return False, "啟用通關密碼保護前，必須先設定一組通關密碼"
        await set_setting("access_gate_enabled", "1")
        return True, "通關密碼保護已啟用"
    else:
        await set_setting("access_gate_enabled", "0")
        return True, "通關密碼保護已停用"

async def verify_access_password(password: str) -> bool:
    # 若符合管理員密碼，也視為合法通關
    if await verify_admin_password(password):
        return True
    stored_hash = await get_setting("access_password_hash")
    salt = await get_setting("access_salt")
    if not stored_hash or not salt:
        return False
    return verify_password(password, stored_hash, salt)

async def get_session_secret() -> str:
    secret = await get_setting("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        await set_setting("session_secret", secret)
    return secret

