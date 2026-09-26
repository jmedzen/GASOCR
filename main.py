import os
import time
import uuid
import asyncio
import json
import hashlib
import hmac
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set
import concurrent.futures
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx

import database
import config
from scheduler import scheduler, free_ocr_task_manager
from pdf_engine import render_pdf_to_images, render_pdf_to_images_async, render_single_page, get_pdf_page_count, render_remaining_pdf_pages
import gemini_ocr
from gemini_ocr import build_ocr_prompt, call_gemini_ocr, refresh_oauth_token_if_needed
import exporters
import web_rpa
from rate_limiter import auth_rate_limiter, get_client_ip

# PDF 渲染專屬執行緒池與並發信號量
# - PDF_RENDER_EXECUTOR 管 CPU（執行緒數）
# - RENDER_SEMAPHORE 管記憶體（同時持有全頁點陣圖的文件數），兩者刻意分開
PDF_RENDER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=config.MAX_RENDER_WORKERS,
    thread_name_prefix="pdf_render_"
)
RENDER_SEMAPHORE = asyncio.Semaphore(config.MAX_CONCURRENT_RENDERS)
print(f"🚀 [Config] PDF 渲染初始化：CPU 執行緒 {config.MAX_RENDER_WORKERS} / "
      f"同時切圖檔案數 {config.MAX_CONCURRENT_RENDERS} (CPU: {os.cpu_count()})")

# 應用程式是否正在關閉。
# ⚠️ 用途：阻止關閉期間再啟動新的 PDFium 渲染。
#    PDFium 在直譯器收尾階段仍在渲染會造成 SIGSEGV（見下方 lifespan 的說明）。
SHUTTING_DOWN = False

# ══════════════════════════════════════════════════════════════
#  背景任務管理
#
#  ⚠️ asyncio.create_task() 的回傳值若沒有被保留強引用，任務可能在執行中途
#     被垃圾回收（Python 官方文件明載的陷阱），造成「任務隨機沒開始／沒跑完」。
#     所有 fire-and-forget 的背景任務都必須走 spawn_background()。
# ══════════════════════════════════════════════════════════════
_background_tasks: Set[asyncio.Task] = set()


def spawn_background(coro) -> asyncio.Task:
    """建立背景任務並保留強引用，避免被 GC 回收。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def shutdown_render_executor(wait: bool = True) -> None:
    """
    安全關閉 PDF 渲染執行緒池。

    ⚠️ 為什麼一定要 wait=True：
      ThreadPoolExecutor 會註冊 atexit 處理器，在直譯器收尾時 join 它的執行緒。
      若此時 PDFium 仍在渲染，C++ 端會存取已被釋放的記憶體 → SIGSEGV
      （實測 faulthandler 追蹤：pdf_render__ 執行緒在 page.render()，
        主執行緒在 concurrent.futures.thread._python_exit 的 join）。
      這就是使用者回報的「python 當機」。
    cancel_futures 先丟棄尚未開始的工作，只等待正在跑的那一頁（通常 <2 秒）。
    可安全重複呼叫。
    """
    try:
        PDF_RENDER_EXECUTOR.shutdown(wait=wait, cancel_futures=True)
    except Exception:
        pass


def _free_disk_bytes(path: Path) -> int:
    """取得指定路徑所在磁碟的可用位元組數；失敗時回傳 -1（代表未知，不阻擋流程）。"""
    try:
        return shutil.disk_usage(str(path)).free
    except Exception:
        return -1


def _safe_filename(name: str) -> str:
    """去除路徑分隔與危險字元，避免上傳檔名造成路徑穿越。"""
    base = os.path.basename(name or "").replace("\\", "/").split("/")[-1]
    base = base.replace("\x00", "").strip()
    cleaned = "".join(c for c in base if c not in '<>:"|?*' and ord(c) >= 32)
    return cleaned or "upload.pdf"


async def _stream_upload_to_disk(
    upload: UploadFile, dest: Path, chunk_size: int = 1024 * 1024
) -> Tuple[str, int]:
    """
    以串流方式把上傳檔寫入磁碟，同時增量計算 SHA-256。

    為什麼不用 file.read()／write_bytes()：
      先前做法會把整個檔案讀進記憶體（使用者的 PDF 單檔可達 250MB），
      5 個檔案時記憶體壓力極大，且同步寫入會長時間卡住事件迴圈
      （導致其他請求包含上傳本身逾時 → 「有時候沒辦法上傳」）。
    回傳 (sha256_hex, 位元組數)。
    """
    limit_bytes = config.MAX_UPLOAD_SIZE_MB * 1024 * 1024 if config.MAX_UPLOAD_SIZE_MB > 0 else 0
    reserve = config.MIN_FREE_DISK_MB * 1024 * 1024
    hasher = hashlib.sha256()
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 先檢查：連一個位元組都還沒寫之前，磁碟是否已經太滿
    free = _free_disk_bytes(config.DATA_DIR)
    if free >= 0 and free < reserve:
        raise HTTPException(
            status_code=507,
            detail=f"伺服器磁碟空間不足（剩餘 {free / 1048576:.0f} MB，"
                   f"需保留至少 {config.MIN_FREE_DISK_MB} MB）。"
                   "請清理 data/renders 或 data/uploads 後再試。"
        )

    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if limit_bytes and total > limit_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"檔案「{upload.filename}」超過單檔上限 "
                               f"{config.MAX_UPLOAD_SIZE_MB} MB"
                    )
                # 每 16MB 檢查一次剩餘空間，避免寫到磁碟爆掉（先前會直接 OSError 中斷整批）
                if total % (16 * 1024 * 1024) < chunk_size:
                    free = _free_disk_bytes(config.DATA_DIR)
                    if free >= 0 and free < reserve:
                        raise HTTPException(
                            status_code=507,
                            detail=f"寫入「{upload.filename}」時磁碟空間不足"
                                   f"（剩餘 {free / 1048576:.0f} MB）。"
                                   "已中止本次上傳，請清理磁碟後再試。"
                        )
                hasher.update(chunk)
                fh.write(chunk)
    except BaseException:
        # 任何失敗都不要留下半截檔案
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return hasher.hexdigest(), total


# 模型快取清單（預設先載入內建清單）
CACHED_MODELS: List[Dict[str, Any]] = list(config.AVAILABLE_MODELS)

def get_combined_models() -> List[Dict[str, Any]]:
    """整合 API 抓取的模型清單與網頁自動化 [Web] 模型"""
    web_models = web_rpa.get_supported_web_models()
    return web_models + list(CACHED_MODELS)

async def update_cached_models(force: bool = False) -> Tuple[bool, str]:
    """向 Google AI Studio 自動抓取並更新可用模型清單"""
    global CACHED_MODELS
    try:
        # 需 include_secrets=True 才能獲取 api_key 或 oauth token 進行 Google API 查詢
        accounts = await database.get_accounts(include_secrets=True)
        active_accounts = [acc for acc in accounts if acc.get("is_active") == 1]
        if not active_accounts:
            return False, "尚未設定啟用的 Google 帳號或 API Key，顯示預設模型清單"
        
        last_error = ""
        # 嘗試使用可用的啟用帳號向 Google 抓取最新清單
        for acc in active_accounts:
            success, models, msg = await gemini_ocr.fetch_google_models(acc)
            if success and models:
                CACHED_MODELS = models
                acc_name = acc.get("name", "未命名金鑰")
                print(f"✅ 成功從 Google AI Studio 自動抓取 {len(models)} 個 Gemini 模型 (使用帳號: {acc_name})")
                return True, f"成功更新 {len(models)} 個最新模型 (來自帳號: {acc_name})"
            else:
                last_error = msg
        return False, f"嘗試向 Google AI Studio 請求模型清單未果 ({last_error})，維持現有清單"
    except Exception as e:
        return False, f"更新模型清單異常: {str(e)}"

# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化資料庫
    await database.init_db()

    # 伺服器啟動時自動修復中斷與未完成任務 (Stale Task Recovery)
    try:
        recovery_stats = await database.recover_stale_tasks_on_startup()
        if any(recovery_stats.values()):
            print(f"🔄 [Stale Task Recovery] 伺服器啟動修復：暫停了 {recovery_stats['tasks_paused']} 個中斷任務、完成 {recovery_stats['tasks_completed']} 個已辨識任務、恢復 {recovery_stats['pages_recovered']} 個進行中頁面為可續傳狀態")
    except Exception as e:
        print(f"⚠️ [Stale Task Recovery] 任務修復執行例外: {e}")

    # 清理上次異常中斷（例如當機）留下的半截上傳暫存檔，避免持續佔用磁碟。
    # 磁碟只剩個位數 GB 時，這些孤兒檔會讓後續上傳直接被拒。
    try:
        stale = list(config.UPLOADS_DIR.glob(".incoming_*.part"))
        for f in stale:
            try:
                f.unlink()
            except Exception:
                pass
        if stale:
            print(f"🧹 [Startup] 清理 {len(stale)} 個未完成的上傳暫存檔")
    except Exception:
        pass

    # API 上線後自動背景抓取最新模型清單
    spawn_background(update_cached_models())

    # 啟動全自動流水線排程調度引擎
    await pipeline_dispatcher.start()

    try:
        yield
    finally:
        global SHUTTING_DOWN
        SHUTTING_DOWN = True

        # 停止流水線排程調度引擎
        try:
            await pipeline_dispatcher.stop()
        except Exception:
            pass

        # ⚠️ 這裡必須 wait=True，這是修掉「python 當機」的關鍵。
        #
        # ThreadPoolExecutor 會註冊 atexit 處理器，在直譯器收尾時 join 它的執行緒。
        # 若此時 PDFium 還在渲染，C++ 端會存取已被釋放的記憶體 → SIGSEGV。
        # faulthandler 實測追蹤（修正前）：
        #   thread pdf_render__1 : pypdfium2 page.py:481 render ← 正在渲染
        #   main thread          : concurrent/futures/thread.py:31 _python_exit ← 正在 join
        #   → Fatal Python error: Segmentation fault
        # 先前的 wait=False 只是不再收新工作，完全沒有等待進行中的渲染，因此必崩。
        # cancel_futures 先丟棄「還沒開始」的工作，只等「正在跑」的那一頁（通常 <2 秒）。
        try:
            await asyncio.to_thread(shutdown_render_executor, True)
            print("🧹 [Shutdown] PDF 渲染執行緒已安全結束")
        except Exception as e:
            print(f"⚠️ [Shutdown] 關閉渲染執行緒時發生問題: {e}")

        # 關閉時脫離 CDP 連線。
        # 注意：close_cdp_session() 只關閉 Playwright 的連線，
        # 不會終止使用者手動啟動的 Chrome。
        try:
            await web_rpa.close_cdp_session()
        except Exception:
            pass

app = FastAPI(title="Google AI Studio Gemini OCR", lifespan=lifespan)

# Templates & Static
TEMPLATES_DIR = config.BASE_DIR / "templates"
STATIC_DIR = config.BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/renders", StaticFiles(directory=str(config.RENDERS_DIR)), name="renders")

# --- Authentication & Session Security ---

def create_session_token(role: str, secret: str, ttl_seconds: int = 86400 * 7) -> str:
    exp = int(time.time()) + ttl_seconds
    payload = f"{role}:{exp}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

def verify_session_token(token: str, secret: str, required_role: Optional[str] = None) -> bool:
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        role, exp_str, sig = parts
        if int(exp_str) < time.time():
            return False
        expected_sig = hmac.new(secret.encode("utf-8"), f"{role}:{exp_str}".encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, sig):
            return False
        if required_role == "admin" and role != "admin":
            return False
        return True
    except Exception:
        return False

async def extract_auth_info(request: Request) -> Tuple[bool, bool]:
    """回傳 (is_admin, is_access)"""
    secret = await database.get_session_secret()
    
    # 提取 admin token (Header 或 Cookie 或 Bearer)
    admin_header = request.headers.get("X-Admin-Token")
    admin_cookie = request.cookies.get("gasocr_admin_token")
    auth_header = request.headers.get("Authorization")
    bearer_token = None
    if auth_header and auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:].strip()
        
    admin_token = admin_header or admin_cookie or (bearer_token if bearer_token and bearer_token.startswith("admin:") else None)
    is_admin = False
    if admin_token and verify_session_token(admin_token, secret, required_role="admin"):
        is_admin = True
        
    # 提取 access token
    access_header = request.headers.get("X-Access-Token")
    access_cookie = request.cookies.get("gasocr_access_token")
    access_token = access_header or access_cookie or bearer_token
    
    is_access = False
    if is_admin:
        is_access = True
    elif access_token and verify_session_token(access_token, secret):
        is_access = True
        
    return is_admin, is_access


async def require_admin(request: Request):
    """FastAPI Dependency: 要求當前請求必須具備管理員權限"""
    is_admin, _ = await extract_auth_info(request)
    if not is_admin:
        raise HTTPException(status_code=401, detail="需要管理員權限")


async def require_access_or_admin(request: Request):
    """FastAPI Dependency: 要求當前請求必須具備通關密碼授權或管理員權限"""
    is_admin, is_access = await extract_auth_info(request)
    gate_enabled = await database.is_access_gate_enabled()
    if is_admin or is_access:
        return True
    if gate_enabled:
        raise HTTPException(status_code=401, detail="已啟用通關密碼保護，請先輸入通關密碼解鎖")
    raise HTTPException(status_code=401, detail="需要通關密碼或管理員權限")



def set_auth_cookie(
    response: Response,
    key: str,
    value: str,
    request: Request,
    max_age: int = 86400 * 7
):
    """統一設定安全 Session Cookie (強制 HttpOnly, Lax, 依協議自動啟用 Secure)"""
    is_secure = (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(
        key=key,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/"
    )


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """全域安全回應標頭注入中間件"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    
    # 1. 靜態資源、首頁與 API 文件一律放行
    if path == "/" or path.startswith(("/static", "/docs", "/openapi.json", "/redoc", "/favicon.ico")):
        return await call_next(request)
        
    # 2. 檢查初次安裝狀態：若未初始化管理員密碼，僅放行初始化與狀態端點
    admin_initialized = await database.is_admin_initialized()
    if not admin_initialized:
        if path in ["/api/version", "/api/auth/status", "/api/admin/init"]:
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "系統尚未初始化，請先設定管理員密碼"})

    # 3. 公開認證與系統狀態端點一律放行
    if path in [
        "/api/version",
        "/api/auth/status",
        "/api/auth/verify-access",
        "/api/auth/logout-access",
        "/api/admin/init",
        "/api/admin/login",
        "/api/admin/logout",
    ]:
        return await call_next(request)

    # 4. 檢查管理員端點權限
    if path.startswith("/api/admin/"):
        is_admin, _ = await extract_auth_info(request)
        if not is_admin:
            return JSONResponse(status_code=401, content={"detail": "需要管理員權限"})
        return await call_next(request)

    # 5. 檢查通關密碼保護 (Access Gate)
    gate_enabled = await database.is_access_gate_enabled()
    if gate_enabled:
        _, is_access = await extract_auth_info(request)
        if not is_access:
            return JSONResponse(status_code=401, content={"detail": "已啟用通關密碼保護，請先輸入通關密碼解鎖"})

    return await call_next(request)

# --- Background OCR Worker ---

active_page_tasks: Dict[str, asyncio.Task] = {}
active_pipeline_tasks: Dict[str, asyncio.Task] = {}
active_bg_render_tasks: Dict[str, asyncio.Task] = {}

def cancel_task_active_jobs(tid: str):
    """即時取消指定任務所有在途的背景任務（包含流水線、切圖與單頁 OCR 請求）"""
    pipe = active_pipeline_tasks.pop(tid, None)
    if pipe and not pipe.done():
        pipe.cancel()
    bg = active_bg_render_tasks.pop(tid, None)
    if bg and not bg.done():
        bg.cancel()
    for key in list(active_page_tasks.keys()):
        if key.startswith(f"{tid}_"):
            pt = active_page_tasks.pop(key, None)
            if pt and not pt.done():
                pt.cancel()

# --- 全自動流水線排程調度引擎 (Pipeline Conveyor Belt Engine) ---

class PipelineDispatcher:
    """
    全域流水線排程調度引擎 (Pipeline Conveyor Belt Engine)
    
    職責：
    1. 管理「等待切圖 (pending_render)」隊列：
       - 當切圖執行緒有空位時，依 FIFO 順序自動拉起等待切圖任務。
    2. 管理「等待OCR (pending_ocr)」隊列：
       - 當免費 OCR 槽位或付費通道有空位時，依 FIFO 順序自動拉起等待OCR任務。
    3. 全自動流水線接棒 (Auto-Drain)：
       - 前一個任務切圖完畢時，立即推進該任務進入 OCR 佇列，並拉起下一個等待切圖任務。
       - 前一個任務 OCR 完畢（或暫停/失敗）時，立即釋放槽位並拉起下一個等待OCR任務。
    4. 崩潰/重啟/異常自動修復 (Self-Healing)：
       - 即使伺服器重啟、連線中斷或協程意外終止，調度器常駐心跳每 1.5 秒巡檢資料庫，
         自動接續所有未完成任務，杜絕任務卡在「等待中」動彈不得。
    """
    def __init__(self):
        self._notify_event = asyncio.Event()
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def trigger(self):
        """立即通知調度器推進佇列"""
        self._notify_event.set()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._loop_task = spawn_background(self._run_loop())
        print("🚀 [Pipeline Dispatcher] 全自動流水線排程調度引擎已啟動")

    async def stop(self):
        self._running = False
        self._notify_event.set()
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        print("🧹 [Pipeline Dispatcher] 流水線排程調度引擎已安全停止")

    async def _run_loop(self):
        while self._running:
            try:
                await self.dispatch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ [Pipeline Dispatcher] 調度循環異常: {e}")
            
            try:
                await asyncio.wait_for(self._notify_event.wait(), timeout=1.5)
                self._notify_event.clear()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def dispatch(self):
        if SHUTTING_DOWN:
            return

        async with self._lock:
            # 1. 清理已結束的協程參照
            for tid in list(active_pipeline_tasks.keys()):
                t = active_pipeline_tasks.get(tid)
                if t and t.done():
                    active_pipeline_tasks.pop(tid, None)

            # 2. 自動校準 free_ocr_task_manager 的 active_tasks，清理殘留幽靈槽位
            running_tids = set(active_pipeline_tasks.keys())
            await free_ocr_task_manager.clean_inactive_slots(running_tids)

            # 3. 查詢資料庫中等待處理的任務 (FIFO 順序，相容舊版 pending)
            async with database.get_db() as db:
                cursor = await db.execute("""
                    SELECT id, status, is_paid, created_at, total_pages, rendered_pages, bg_render_status
                    FROM tasks
                    WHERE status IN ('pending_render', 'pending_ocr', 'pending')
                    ORDER BY created_at ASC
                """)
                rows = await cursor.fetchall()
            waiting_tasks = [database._normalize_task_status(dict(r)) for r in rows]

            if not waiting_tasks:
                return

            # 4. 推進等待切圖任務 (pending_render)
            available_render_slots = max(0, RENDER_SEMAPHORE._value)
            if available_render_slots > 0:
                pending_renders = [t for t in waiting_tasks if t["status"] == "pending_render"]
                for pt in pending_renders:
                    tid = pt["id"]
                    if tid in active_pipeline_tasks and not active_pipeline_tasks[tid].done():
                        continue
                    if available_render_slots <= 0:
                        break
                    print(f"🚀 [Pipeline Dispatcher] 自動啟動切圖任務: {tid}")
                    spawn_background(process_task_pipeline(tid))
                    available_render_slots -= 1

            # 5. 推進等待OCR任務 (pending_ocr)
            max_free_slots = await free_ocr_task_manager.get_max_concurrent()
            current_active_free = len(free_ocr_task_manager.active_tasks)
            available_ocr_slots = max(0, max_free_slots - current_active_free)

            pending_ocrs = [t for t in waiting_tasks if t["status"] == "pending_ocr"]
            for ot in pending_ocrs:
                tid = ot["id"]
                if tid in active_pipeline_tasks and not active_pipeline_tasks[tid].done():
                    continue
                is_paid = bool(ot.get("is_paid"))
                if is_paid:
                    print(f"💎 [Pipeline Dispatcher] 自動啟動付費 OCR 任務: {tid}")
                    spawn_background(process_task_pipeline(tid))
                elif available_ocr_slots > 0:
                    print(f"🚀 [Pipeline Dispatcher] 自動啟動免費 OCR 任務: {tid} (可用槽位: {available_ocr_slots})")
                    spawn_background(process_task_pipeline(tid))
                    available_ocr_slots -= 1

pipeline_dispatcher = PipelineDispatcher()

async def background_render_task_pages(task_id: str, pdf_path: Path, target_pages: set):
    """在背景完成剩餘未渲染頁面的 300 DPI 高清渲染，受 RENDER_SEMAPHORE 控管總 CPU 並發"""
    active_bg_render_tasks[task_id] = asyncio.current_task()
    try:
        async with RENDER_SEMAPHORE:
            if SHUTTING_DOWN:
                return
            check_task = await database.get_task(task_id)
            if not check_task or check_task.get("status") in ["paused", "failed"]:
                return

            await database.update_task_status(task_id, bg_render_status="rendering")
            print(f"🎨 [Background Render] 任務 {task_id} 開始背景渲染其餘頁面...")
            
            # 使用專屬切圖執行緒池 PDF_RENDER_EXECUTOR，單檔單線程循序切圖
            loop = asyncio.get_running_loop()
            remaining_rendered = await loop.run_in_executor(
                PDF_RENDER_EXECUTOR,
                render_remaining_pdf_pages, pdf_path, task_id, target_pages
            )
            
            if remaining_rendered:
                pages_data = [
                    {"task_id": task_id, "page_num": p_num, "image_path": str(img_path)}
                    for p_num, img_path in remaining_rendered
                ]
                await database.add_background_rendered_pages(pages_data)
                
            await database.update_task_status(task_id, bg_render_status="completed")
            print(f"✅ [Background Render] 任務 {task_id} 其餘 {len(remaining_rendered)} 頁高清渲染全部完成！")
    except asyncio.CancelledError:
        print(f"⏸️ [Background Render] 任務 {task_id} 背景高清切圖已及時響應暫停/取消")
        await database.update_task_status(task_id, bg_render_status="paused")
        return
    except Exception as e:
        print(f"❌ [Background Render] 任務 {task_id} 背景渲染異常: {e}")
        await database.update_task_status(task_id, bg_render_status="failed")
    finally:
        active_bg_render_tasks.pop(task_id, None)
        pipeline_dispatcher.trigger()

async def run_page_ocr(
    task_id: str, 
    page_num: int, 
    image_path: Path, 
    model: str, 
    prompt: str, 
    max_retries: int = 3,
    specific_account_id: Optional[int] = None
):
    """執行單頁 OCR 並配合排程器冷卻切換，支援 503 自動重試 5 次與任務暫停"""
    task_key = f"{task_id}_{page_num}"
    active_page_tasks[task_key] = asyncio.current_task()
    
    clean_model = model.replace("[PAID]", "").strip()
    is_paid_call = "[PAID]" in model or specific_account_id is not None
    used_model_display = f"{clean_model} 💎" if is_paid_call else clean_model
    
    try:
        await database.update_page_status(task_id, page_num, "processing")

        # 0. 切圖可能還沒渲染完成（例如剛上傳就按「重新辨識本頁」，或背景渲染尚未排到）。
        #    先前會直接以 FileNotFoundError 失敗，使用者只看到隨機錯誤。
        #    這裡改成即時補渲染該頁，讓重試在任何時機都能成功。
        if not image_path.exists():
            page_task = await database.get_task(task_id)
            pdf_path = Path(page_task["original_filepath"]) if page_task else None
            if pdf_path and pdf_path.exists():
                try:
                    await asyncio.to_thread(
                        render_single_page, pdf_path, page_num, image_path
                    )
                    print(f"🖼️ [Page OCR] 任務 {task_id} 第 {page_num} 頁切圖不存在，已即時補渲染")
                except Exception as e:
                    await database.update_page_result(
                        task_id, page_num, "failed",
                        error_message=f"頁面切圖不存在且即時補渲染失敗: {e}"
                    )
                    return
            else:
                await database.update_page_result(
                    task_id, page_num, "failed",
                    error_message=f"頁面切圖不存在，且找不到原始 PDF: {image_path}"
                )
                return

        # 1. 若為網頁自動化模型，調用本機網頁自動化 RPA
        if web_rpa.is_web_model(clean_model):
            start_time = time.time()
            print(f"🌐 [Page OCR Web RPA] 任務 {task_id} 第 {page_num} 頁透過網頁自動化調用: {clean_model}")
            success, text_or_err, status_code = await web_rpa.run_web_ocr(
                image_path=image_path,
                model_name=clean_model,
                prompt=prompt
            )
            duration = round(time.time() - start_time, 2)
            if success:
                await database.update_page_result(
                    task_id, page_num, "completed",
                    ocr_text=text_or_err,
                    account_id=None,
                    duration=duration,
                    used_model=used_model_display
                )
                return
            else:
                await database.update_page_result(
                    task_id, page_num, "failed",
                    error_message=f"[Web RPA] {text_or_err}",
                    account_id=None,
                    duration=duration,
                    used_model=used_model_display
                )
                return

        task_info = await database.get_task(task_id) or {}
        target_account_id = specific_account_id
        is_paid_task = bool(is_paid_call or task_info.get("is_paid"))
        if target_account_id is None and task_info.get("is_paid"):
            target_account_id = task_info.get("paid_account_id")

        retries = 0
        retries_503 = 0
        max_retries_503 = 5

        while retries < max_retries:
            # 2. 向智慧排程器索取可用帳號（若為付費任務，鎖定指定付費金鑰或從付費池輪詢）
            account = await scheduler.get_next_available_account(
                specific_account_id=target_account_id,
                require_paid=is_paid_task
            )
            if not account:
                # 等待冷卻中的帳號解除
                account = await scheduler.wait_for_any_account(
                    max_wait_seconds=60.0,
                    specific_account_id=target_account_id,
                    require_paid=is_paid_task
                )
                
            if not account:
                # 智慧降級備援：若指定付費通道但無可用金鑰（冷卻或已耗盡），自動降級至免費池預設模型
                if is_paid_task or target_account_id is not None:
                    fallback_model = config.DEFAULT_MODEL.replace("[PAID]", "").strip()
                    prev_label = clean_model + (" 💎" if is_paid_call else "")
                    print(f"⚠️ [Page OCR Fallback] 任務 {task_id} 第 {page_num} 頁：{prev_label} 無可用金鑰或冷卻超時，智慧降級至免費 API ({fallback_model}) 接續執行")
                    await database.update_page_status(
                        task_id, page_num, "processing",
                        error_message=f"[智慧降級備援] 付費 API 配額耗盡/冷卻中，已自動切換至免費 API ({fallback_model}) 接續轉譯..."
                    )
                    target_account_id = None
                    is_paid_call = False
                    is_paid_task = False
                    clean_model = fallback_model
                    used_model_display = f"{clean_model} [降級備援]"
                    
                    # 重新向免費金鑰池索取可用帳號
                    account = await scheduler.get_next_available_account(specific_account_id=None, require_paid=False)
                    if not account:
                        account = await scheduler.wait_for_any_account(max_wait_seconds=60.0, specific_account_id=None, require_paid=False)

            if not account:
                if (not is_paid_task) and await scheduler.is_all_free_quota_exhausted():
                    reset_info = scheduler.get_free_quota_reset_info()
                    prompt_msg = (
                        f"[免費配額耗盡] 所有免費金鑰池皆已連續 3 次回傳額度上限 (429 Quota Exhausted)。"
                        f"預計於 {reset_info['reset_time_display']} (美西 00:00 PT) 重置 (尚餘約 {reset_info['remaining_formatted']})。"
                        f"任務已自動安全暫停，配額重置或新增可用金鑰後即可無縫接續！"
                    )
                    await database.update_page_status(task_id, page_num, "paused", error_message=prompt_msg)
                    await database.update_task_status(task_id, status="paused", bg_render_status="paused")
                    await database.pause_task_in_progress_pages(task_id)
                    return

                await database.update_page_result(
                    task_id, page_num, "failed",
                    error_message="無可用的 Google 帳號或 API Key。請至「帳號管理」新增或檢查帳號狀態。"
                )
                return
                
            start_time = time.time()
            print(f"🚀 [Page OCR] 任務 {task_id} 第 {page_num} 頁使用模型: {clean_model} (帳號: {account['name']})")
            success, text_or_err, status_code = await call_gemini_ocr(account, image_path, clean_model, prompt)
            duration = round(time.time() - start_time, 2)
            
            if success:
                await scheduler.report_success(account["id"])
                await database.update_page_result(
                    task_id, page_num, "completed",
                    ocr_text=text_or_err,
                    account_id=account["id"],
                    duration=duration,
                    used_model=used_model_display
                )
                return
            elif status_code == 503 or "503" in str(text_or_err) or "UNAVAILABLE" in str(text_or_err).upper() or "OVERLOADED" in str(text_or_err).upper():
                # 503 伺服器忙碌 / 模型超載，自動重試 5 次
                retries_503 += 1
                delay = 2.0 * retries_503
                print(f"⚠️ [503 Service Unavailable] 任務 {task_id} 第 {page_num} 頁遭遇 503，第 {retries_503}/5 次自動重試，等待 {delay:.1f} 秒...")
                await database.update_page_status(
                    task_id, page_num, "processing", 
                    error_message=f"[503 伺服器忙碌] 正在進行第 {retries_503}/5 次自動重試 (等待 {delay:.1f}s)..."
                )
                await asyncio.sleep(delay)
                if retries_503 >= max_retries_503:
                    await database.update_page_result(
                        task_id, page_num, "failed",
                        error_message=f"[503 重試 5 次皆忙碌] {text_or_err}",
                        account_id=account["id"],
                        duration=duration,
                        used_model=used_model_display
                    )
                    return
                continue
            elif status_code == 429:
                # 觸發 429 限額，登記該帳號冷卻
                await scheduler.report_rate_limited(account["id"])

                # 追蹤免費金鑰連續配額耗盡次數
                is_free_account = (account.get("is_paid", 0) == 0)
                all_free_exhausted = False
                if is_free_account:
                    all_free_exhausted = await scheduler.report_quota_exhausted(account["id"])

                # 若所有免費金鑰池皆已連續 3 次額度耗盡，且任務為免費通道，自動暫停任務並提示重置時間
                if all_free_exhausted and not is_paid_task:
                    reset_info = scheduler.get_free_quota_reset_info()
                    prompt_msg = (
                        f"[免費配額耗盡] 所有免費金鑰池皆已連續 3 次回傳額度上限 (429 Quota Exhausted)。"
                        f"預計於 {reset_info['reset_time_display']} (美西 00:00 PT) 重置 (尚餘約 {reset_info['remaining_formatted']})。"
                        f"任務已自動安全暫停，配額重置或新增可用金鑰後即可無縫接續！"
                    )
                    print(f"🚨 [全域免費配額耗盡] 任務 {task_id} 第 {page_num} 頁觸發免費池全數耗盡，自動優雅暫停任務")
                    await database.update_page_status(task_id, page_num, "paused", error_message=prompt_msg)
                    await database.update_task_status(task_id, status="paused", bg_render_status="paused")
                    await database.pause_task_in_progress_pages(task_id)
                    return
                
                # 智慧降級備援：若為付費 API 帳號或指定付費通道觸發 429，或高級模型（非預設模型）重試多次遭遇 429
                fallback_model = config.DEFAULT_MODEL.replace("[PAID]", "").strip()
                should_fallback = False
                fallback_reason = ""
                
                if is_paid_call or target_account_id is not None or is_paid_task:
                    # 若為輪詢付費池 (未鎖定單一金鑰)，先看付費池是否還有其他可用金鑰
                    other_paid = None
                    if target_account_id is None and is_paid_task:
                        other_paid = await scheduler.get_next_available_account(specific_account_id=None, require_paid=True)
                    if not other_paid:
                        should_fallback = True
                        fallback_reason = f"付費 API ({account['name']}) 配額耗盡 (429)"
                    else:
                        print(f"🔄 [Page OCR] 付費金鑰 {account['name']} 觸發 429，切換至金鑰池另一組可用付費金鑰: {other_paid['name']}")
                elif clean_model != fallback_model and retries >= 1:
                    should_fallback = True
                    fallback_reason = f"高級模型 ({clean_model}) 頻繁觸發 429 限額"
                    
                if should_fallback:
                    print(f"⚠️ [Page OCR Fallback] 任務 {task_id} 第 {page_num} 頁：{fallback_reason}，智慧降級至免費 API ({fallback_model})")
                    await database.update_page_status(
                        task_id, page_num, "processing",
                        error_message=f"[智慧降級備援] {fallback_reason}，已轉為免費 API ({fallback_model}) 續跑..."
                    )
                    target_account_id = None
                    is_paid_call = False
                    is_paid_task = False
                    clean_model = fallback_model
                    used_model_display = f"{clean_model} [降級備援]"
                    retries += 1
                    await asyncio.sleep(1.0)
                    continue
                retries += 1
                if retries >= max_retries:
                    await database.update_page_result(
                        task_id, page_num, "failed",
                        error_message=f"[{account['name']}] 配額耗盡或速率受限 (429 Too Many Requests)，已重試 {retries} 次仍受限",
                        account_id=account["id"],
                        duration=duration,
                        used_model=used_model_display
                    )
                    return
                await asyncio.sleep(2.0)
            else:
                # 其他錯誤
                retries += 1
                if retries >= max_retries:
                    await database.update_page_result(
                        task_id, page_num, "failed",
                        error_message=f"[{account['name']}] {text_or_err}",
                        account_id=account["id"],
                        duration=duration,
                        used_model=used_model_display
                    )
                    return
                await asyncio.sleep(2.0)

        # 保底防呆：若 while 迴圈正常結束但仍未 return，檢查該頁面狀態，若仍為 processing 則標記為 failed
        cur_pages = await database.get_task_pages(task_id)
        this_page = next((x for x in cur_pages if x["page_num"] == page_num), None)
        if this_page and this_page["status"] == "processing":
            await database.update_page_result(
                task_id, page_num, "failed",
                error_message=this_page.get("error_message") or "多次重試後未能取得辨識結果",
                used_model=used_model_display
            )
    except asyncio.CancelledError:
        print(f"⏸️ [Page OCR] 任務 {task_id} 第 {page_num} 頁辨識已被使用者暫停")
        await database.update_page_status(task_id, page_num, "paused", error_message="已手動暫停")
        raise
    except Exception as e:
        print(f"❌ [Page OCR] 任務 {task_id} 第 {page_num} 頁例外: {e}")
        await database.update_page_status(task_id, page_num, "failed", error_message=str(e))
    finally:
        active_page_tasks.pop(task_key, None)

def is_task_rendering_completed(task: dict) -> bool:
    """
    檢查任務之切圖渲染是否已全數完成：
    1. total_pages 必須大於 0
    2. rendered_pages 必須 >= total_pages
    3. 目標頁面之 PNG 圖片實體檔案必須全數存在
    """
    if not task:
        return False
    total = task.get("total_pages", 0) or 0
    rendered = task.get("rendered_pages", 0) or 0
    if total <= 0 or rendered < total:
        return False
        
    task_id = task.get("id")
    if not task_id:
        return False
        
    task_render_dir = config.RENDERS_DIR / task_id
    if not task_render_dir.exists():
        return False
        
    s_page = task.get("start_page", 1) or 1
    e_page = task.get("end_page", 0) or 0
    if e_page <= 0:
        e_page = task.get("pdf_total_pages", 0) or total
        
    for p_num in range(s_page, e_page + 1):
        if not (task_render_dir / f"page_{p_num:04d}.png").exists():
            return False
            
    return True

async def process_task_pipeline(task_id: str):
    """整個任務的流水線處理（支援初次執行與切圖/OCR階段隨時暫停與接續）"""
    current_coro_task = asyncio.current_task()
    active_pipeline_tasks[task_id] = current_coro_task
    
    try:
        task = await database.get_task(task_id)
        if not task:
            return
            
        pdf_path = Path(task["original_filepath"])
        # 同步 PDF 解析丟到執行緒，避免大檔解析時卡住整個事件迴圈
        pdf_total_pages = (
            await asyncio.to_thread(get_pdf_page_count, pdf_path)
            if pdf_path.exists() else 0
        )
        s_page = task.get("start_page", 1) or 1
        e_page = task.get("end_page", 0) or 0
        if e_page <= 0 or e_page > pdf_total_pages:
            e_page = pdf_total_pages
        target_page_nums = set(range(s_page, e_page + 1))
        target_list = list(range(s_page, e_page + 1))
        total_target = len(target_list)
        task_render_dir = config.RENDERS_DIR / task_id

        # 開工前先確認磁碟空間
        _free = _free_disk_bytes(config.DATA_DIR)
        if _free >= 0 and _free < config.MIN_FREE_DISK_MB * 1024 * 1024:
            print(f"❌ [Task {task_id}] 磁碟空間不足（剩餘 {_free/1048576:.0f} MB），中止切圖")
            await database.update_task_status(task_id, "failed")
            return

        # 關閉中就不再啟動新的 PDFium 渲染
        if SHUTTING_DOWN:
            print(f"⏹️ [Task {task_id}] 應用程式正在關閉，不啟動新的切圖")
            return

        pages = await database.get_task_pages(task_id)
        if not pages:
            # 初次建立 task_pages 預備記錄，排隊等待切圖期間頁面狀態皆為 pending
            init_pages = [
                {
                    "task_id": task_id,
                    "page_num": p_num,
                    "image_path": str(task_render_dir / f"page_{p_num:04d}.png"),
                    "status": "pending"
                }
                for p_num in target_list
            ]
            await database.create_task_pages(init_pages)
            await database.update_task_status(
                task_id, 
                status="pending_render", 
                total_pages=total_target, 
                processed_pages=0,
                rendered_pages=0,
                pdf_total_pages=pdf_total_pages
            )
            pages = await database.get_task_pages(task_id)

        # 1. 檢查哪些目標頁面尚未切圖（圖片檔案不存在或尚未完成切圖）
        pages_to_slice = [
            p_num for p_num in target_list 
            if not (task_render_dir / f"page_{p_num:04d}.png").exists()
        ]
        
        # 2. 若有未切圖的頁面，進入受控並發進行切圖渲染
        if pages_to_slice:
            # 佇列排隊階段：確保狀態為 pending_render (等待切圖)
            await database.update_task_status(task_id, status="pending_render", bg_render_status="pending")
            async with RENDER_SEMAPHORE:
                # 取得執行槽位後，檢查在佇列等待期間任務是否已被使用者暫停或刪除
                check_task = await database.get_task(task_id)
                if not check_task or check_task.get("status") in ["paused", "failed"]:
                    return

                # 正式啟動切圖，狀態更新為 rendering
                await database.update_task_status(task_id, status="rendering", bg_render_status="rendering")
                if pages_to_slice:
                    await database.update_page_status(task_id, pages_to_slice[0], "rendering")

                current_rendered = total_target - len(pages_to_slice)
                async def on_single_page_done(p_num: int, total_count: int, img_path: Path):
                    nonlocal current_rendered
                    current_rendered += 1
                    await database.update_page_image_and_status(task_id, p_num, str(img_path), "pending")
                    await database.update_task_status(task_id, rendered_pages=current_rendered)

                    # 檢查任務是否已被暫停或取消
                    t_check = await database.get_task(task_id)
                    if not t_check or t_check.get("status") in ["paused", "failed"]:
                        raise asyncio.CancelledError()

                await render_pdf_to_images_async(
                    pdf_path, 
                    task_id, 
                    start_page=s_page, 
                    end_page=e_page,
                    pages_to_render=pages_to_slice,
                    on_page_rendered=on_single_page_done,
                    executor=PDF_RENDER_EXECUTOR
                )
            
            # 切圖完成釋放切圖槽位，立即通知調度器拉起下一個等待切圖任務
            pipeline_dispatcher.trigger()

            # 切圖完成後，檢查在切圖渲染期間，使用者是否已按了暫停或刪除
            check_task = await database.get_task(task_id)
            if not check_task or check_task.get("status") in ["paused", "failed"]:
                return

        # 3. 準備進入 OCR 階段（若為免費任務，受 free_ocr_task_manager 並發槽位控管）
        is_paid_task = bool(task.get("is_paid"))
        
        # 若為免費任務，在等待取得免費 OCR 槽位排隊期間，狀態設為 pending_ocr (等待OCR)
        if not is_paid_task:
            await database.update_task_status(
                task_id, 
                status="pending_ocr", 
                total_pages=total_target, 
                rendered_pages=total_target,
                pdf_total_pages=pdf_total_pages
            )
            # 通知調度器該任務已就緒等待 OCR
            pipeline_dispatcher.trigger()

        async with free_ocr_task_manager.limit(task_id, is_paid=is_paid_task):
            # 取得執行槽位後，檢查在等待期間任務是否已被使用者暫停或刪除
            check_task = await database.get_task(task_id)
            if not check_task or check_task.get("status") in ["paused", "failed"]:
                return

            # 正式啟動 OCR 轉譯階段，切換為 processing 狀態準備執行 OCR
            await database.update_task_status(
                task_id, "processing", 
                total_pages=total_target, 
                rendered_pages=total_target,
                pdf_total_pages=pdf_total_pages
            )
            pages = await database.get_task_pages(task_id)
            
            # 4. 準備 Prompt (重新拉取 task 以取得最新可能被變更之模型與設定)
            task = await database.get_task(task_id) or task
            prompt = build_ocr_prompt(
                lang=task.get("lang_pref", "traditional"),
                direction=task.get("direction_pref", "auto"),
                column=task.get("column_pref", "auto"),
                custom_prompt=task.get("custom_prompt", "")
            )
            
            # 5. 逐頁進行 OCR 辨識（跳過已完成的頁面與非本次目標頁面）
            for p in pages:
                if p["page_num"] not in target_page_nums:
                    continue
                if p["status"] == "completed" and (p.get("ocr_text") or "").strip():
                    continue
                    
                # 檢查任務是否已被刪除或暫停
                current_task = await database.get_task(task_id)
                if not current_task or current_task.get("status") in ["paused", "failed"]:
                    return
                    
                model_to_use = current_task.get("model") or task.get("model", config.DEFAULT_MODEL)
                await run_page_ocr(task_id, p["page_num"], Path(p["image_path"]), model_to_use, prompt)
                
            # 5.5 全書最後一頁初次 OCR 完畢後，重新檢查一次沒有結果的頁面並補辨 (End-of-Book Sweep)
            current_task = await database.get_task(task_id)
            if not current_task or current_task.get("status") in ["paused", "failed"]:
                return

            latest_pages = await database.get_task_pages(task_id)
            unresolved_pages = [
                lp for lp in latest_pages 
                if lp["page_num"] in target_page_nums 
                and (lp["status"] != "completed" or not (lp.get("ocr_text") or "").strip())
            ]

            if unresolved_pages:
                missing_nums = [p["page_num"] for p in unresolved_pages]
                print(f"🔄 [Task {task_id}] 全書最後一頁辨識完畢，發現 {len(unresolved_pages)} 頁無結果 (頁碼: {missing_nums})，啟動全書結尾自動補檢複查...")
                # 給予短暫冷卻緩衝 (2.5s)，讓 429 速率限制或 503 伺服器忙碌解凍
                await asyncio.sleep(2.5)

                for idx, p in enumerate(unresolved_pages, 1):
                    current_task = await database.get_task(task_id)
                    if not current_task or current_task.get("status") in ["paused", "failed"]:
                        return
                    p_num = p["page_num"]
                    print(f"🔄 [Task {task_id} 補檢複查 {idx}/{len(unresolved_pages)}] 重新辨識第 {p_num} 頁...")
                    await database.update_page_status(
                        task_id, p_num, "processing", 
                        error_message=f"正在進行全書結尾自動補檢複查 ({idx}/{len(unresolved_pages)} 頁)..."
                    )
                    model_to_use = current_task.get("model") or task.get("model", config.DEFAULT_MODEL)
                    await run_page_ocr(
                        task_id=task_id, 
                        page_num=p_num, 
                        image_path=Path(p["image_path"]), 
                        model=model_to_use, 
                        prompt=prompt
                    )

            # 6. 全部目標頁面處理完成結算
            latest_task = await database.get_task(task_id)
            if latest_task and latest_task["status"] not in ["paused", "failed"]:
                latest_pages = await database.get_task_pages(task_id)
                target_pages_status = [lp for lp in latest_pages if lp["page_num"] in target_page_nums]
                all_completed = all(lp["status"] == "completed" and (lp.get("ocr_text") or "").strip() for lp in target_pages_status)
                if all_completed:
                    await database.update_task_status(task_id, "completed")
                    print(f"🎉 [Task {task_id}] 全書目標頁面全數辨識完成 (100%)！")
                    pipeline_dispatcher.trigger()
                    
                    # 7. 進度條完成之後，若還有頁面沒有高清解析，在背景跑完所有剩餘頁面渲染
                    if pdf_path.exists() and pdf_total_pages > len(target_page_nums):
                        print(f"🚀 [Background Render] 觸發其餘未渲染頁面之背景高清處理 (總頁數: {pdf_total_pages}, 本次辨識: {len(target_page_nums)})")
                        spawn_background(background_render_task_pages(task_id, pdf_path, target_page_nums))
                else:
                    # 複查後仍有頁面未成功（例如該帳號當日配額已完全耗盡）
                    for lp in target_pages_status:
                        if lp["status"] != "completed" or not (lp.get("ocr_text") or "").strip():
                            if lp["status"] != "failed":
                                err_msg = lp.get("error_message") or "全書自動補檢後仍無結果（API配額耗盡或忙碌）"
                                await database.update_page_result(task_id, lp["page_num"], "failed", error_message=err_msg)
                    
                    completed_count = sum(1 for lp in target_pages_status if lp["status"] == "completed")
                    print(f"⚠️ [Task {task_id}] 全書轉譯及補檢完畢，部分頁面未完成 (成功: {completed_count}/{total_target})")
                    if completed_count == 0:
                        await database.update_task_status(task_id, "failed")
                    else:
                        await database.update_task_status(task_id, "paused")
                    pipeline_dispatcher.trigger()
        
    except asyncio.CancelledError:
        print(f"⏸️ [Task {task_id}] 切圖/轉譯流水線已即時響應暫停指令")
        await database.update_task_status(task_id, status="paused", bg_render_status="paused")
        await database.pause_task_in_progress_pages(task_id)
        return
    except Exception as e:
        if await database.get_task(task_id):
            print(f"Task {task_id} pipeline error: {e}")
            await database.update_task_status(task_id, "failed")
    finally:
        active_pipeline_tasks.pop(task_id, None)
        pipeline_dispatcher.trigger()

# --- Web UI Routes ---

@app.get("/api/version")
async def get_version():
    """取得當前應用程式版本與建置號"""
    return {
        "app_name": config.APP_NAME,
        "version": config.APP_VERSION,
        "build": config.BUILD_NUMBER,
    }

# --- Auth & Admin Models ---

class AdminInitRequest(BaseModel):
    password: str

class AdminLoginRequest(BaseModel):
    password: str

class AdminChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class AccessGateSettingRequest(BaseModel):
    enabled: bool
    password: Optional[str] = None

class AccessVerifyRequest(BaseModel):
    password: str

# --- Auth & Admin API Routes ---

@app.get("/api/auth/status")
async def get_auth_status(request: Request):
    """檢查目前登入狀態、通關密碼保護狀態與管理員初始化狀態"""
    is_admin, is_access = await extract_auth_info(request)
    gate_enabled = await database.is_access_gate_enabled()
    admin_init = await database.is_admin_initialized()
    return {
        "admin_initialized": admin_init,
        "access_gate_enabled": gate_enabled,
        "has_access_password": await database.has_access_password(),
        "is_admin": is_admin,
        "is_authenticated_user": (not gate_enabled) or is_access,
    }

@app.post("/api/admin/init")
async def admin_init(req: AdminInitRequest, request: Request):
    """初次安裝初始化管理員密碼"""
    if await database.is_admin_initialized():
        raise HTTPException(status_code=400, detail="系統已完成管理員初始化，無法重複設定")
    pwd = req.password.strip()
    if len(pwd) < 4:
        raise HTTPException(status_code=400, detail="管理員密碼長度至少需 4 個字元")
        
    success = await database.init_admin_password(pwd)
    if not success:
        raise HTTPException(status_code=400, detail="管理員密碼初始化失敗")
        
    secret = await database.get_session_secret()
    token = create_session_token("admin", secret)
    resp = JSONResponse(content={"success": True, "token": token, "message": "管理員密碼初始化成功！"})
    set_auth_cookie(resp, "gasocr_admin_token", token, request)
    set_auth_cookie(resp, "gasocr_access_token", token, request)
    return resp

@app.post("/api/admin/login")
async def admin_login(req: AdminLoginRequest, request: Request):
    """管理員登入（含 IP 速率限制與防暴力破解）"""
    ip = get_client_ip(request)
    allowed, retry_after = await auth_rate_limiter.check_rate_limit(ip, "admin_login")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"登入嘗試次數過多，為保障安全請於 {retry_after} 秒後再試",
            headers={"Retry-After": str(retry_after)}
        )
    await auth_rate_limiter.record_attempt(ip, "admin_login")

    if not await database.is_admin_initialized():
        raise HTTPException(status_code=400, detail="系統尚未初始化管理員密碼，請先完成初始化設定")
    if not await database.verify_admin_password(req.password.strip()):
        await auth_rate_limiter.record_failure(ip, "admin_login")
        raise HTTPException(status_code=401, detail="管理員密碼錯誤")
        
    await auth_rate_limiter.record_success(ip, "admin_login")
    secret = await database.get_session_secret()
    token = create_session_token("admin", secret)
    resp = JSONResponse(content={"success": True, "token": token, "message": "管理員登入成功"})
    set_auth_cookie(resp, "gasocr_admin_token", token, request)
    set_auth_cookie(resp, "gasocr_access_token", token, request)
    return resp

@app.post("/api/admin/logout")
async def admin_logout():
    """管理員登出"""
    resp = JSONResponse(content={"success": True, "message": "管理員已登出"})
    resp.delete_cookie(key="gasocr_admin_token", path="/")
    return resp

@app.post("/api/admin/change-password")
async def admin_change_password(req: AdminChangePasswordRequest, request: Request):
    """修改管理員密碼"""
    is_admin, _ = await extract_auth_info(request)
    if not is_admin:
        raise HTTPException(status_code=401, detail="需要管理員權限")
        
    success, msg = await database.change_admin_password(req.old_password.strip(), req.new_password.strip())
    if not success:
        raise HTTPException(status_code=400, detail=msg)
        
    secret = await database.get_session_secret()
    token = create_session_token("admin", secret)
    resp = JSONResponse(content={"success": True, "token": token, "message": msg})
    set_auth_cookie(resp, "gasocr_admin_token", token, request)
    return resp

@app.get("/api/admin/settings")
async def get_admin_settings(request: Request):
    """取得管理員後台設定與系統概況"""
    is_admin, _ = await extract_auth_info(request)
    if not is_admin:
        raise HTTPException(status_code=401, detail="需要管理員權限")
        
    accounts = await database.get_accounts()
    tasks = await database.list_tasks()
    return {
        "access_gate_enabled": await database.is_access_gate_enabled(),
        "has_access_password": await database.has_access_password(),
        "stats": {
            "total_accounts": len(accounts),
            "active_accounts": sum(1 for a in accounts if a.get("is_active") == 1),
            "total_tasks": len(tasks),
            "completed_tasks": sum(1 for t in tasks if t.get("status") == "completed"),
        }
    }

@app.post("/api/admin/settings/access-gate")
async def update_access_gate(req: AccessGateSettingRequest, request: Request):
    """設定通關密碼保護開關與通關密碼"""
    is_admin, _ = await extract_auth_info(request)
    if not is_admin:
        raise HTTPException(status_code=401, detail="需要管理員權限")
        
    success, msg = await database.set_access_gate(req.enabled, req.password)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
        
    return {
        "success": True,
        "message": msg,
        "access_gate_enabled": await database.is_access_gate_enabled(),
        "has_access_password": await database.has_access_password(),
    }

@app.post("/api/auth/verify-access")
async def verify_access(req: AccessVerifyRequest, request: Request):
    """訪客輸入通關密碼解鎖本套件（含 IP 速率限制與防暴力破解）"""
    ip = get_client_ip(request)
    allowed, retry_after = await auth_rate_limiter.check_rate_limit(ip, "verify_access")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"嘗試次數過多，為保障安全請於 {retry_after} 秒後再試",
            headers={"Retry-After": str(retry_after)}
        )
    await auth_rate_limiter.record_attempt(ip, "verify_access")

    pwd = req.password.strip()
    is_admin = await database.verify_admin_password(pwd)
    is_valid_access = is_admin or await database.verify_access_password(pwd)
    
    if not is_valid_access:
        await auth_rate_limiter.record_failure(ip, "verify_access")
        raise HTTPException(status_code=401, detail="通關密碼錯誤，請重新輸入")
        
    await auth_rate_limiter.record_success(ip, "verify_access")
    secret = await database.get_session_secret()
    role = "admin" if is_admin else "access"
    token = create_session_token(role, secret)
    
    resp = JSONResponse(content={
        "success": True, 
        "token": token, 
        "is_admin": is_admin, 
        "message": "解鎖成功！" + (" (管理員身分)" if is_admin else "")
    })
    set_auth_cookie(resp, "gasocr_access_token", token, request)
    if is_admin:
        set_auth_cookie(resp, "gasocr_admin_token", token, request)
    return resp

@app.post("/api/auth/logout-access")
async def access_logout():
    """訪客重新鎖定通關"""
    resp = JSONResponse(content={"success": True, "message": "已成功鎖定"})
    resp.delete_cookie(key="gasocr_access_token", path="/")
    resp.delete_cookie(key="gasocr_admin_token", path="/")
    return resp

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 1. 檢查初次安裝初始化狀態：未設定管理員密碼時直接呈現初始化精靈
    admin_initialized = await database.is_admin_initialized()
    if not admin_initialized:
        return templates.TemplateResponse(
            request=request,
            name="gate_lock.html",
            context={
                "mode": "setup",
                "app_name": config.APP_NAME,
                "app_version": config.APP_VERSION,
                "build_number": config.BUILD_NUMBER,
            }
        )

    # 2. 檢查全站通關密碼保護 (Access Gate)：已啟用但未解鎖時物理隔離主系統
    gate_enabled = await database.is_access_gate_enabled()
    is_admin, is_access = await extract_auth_info(request)

    if gate_enabled and not is_access:
        return templates.TemplateResponse(
            request=request,
            name="gate_lock.html",
            context={
                "mode": "lock",
                "app_name": config.APP_NAME,
                "app_version": config.APP_VERSION,
                "build_number": config.BUILD_NUMBER,
            }
        )

    # 3. 已解鎖或未啟用全站保護：正常渲染系統主頁面
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": config.APP_NAME,
            "app_version": config.APP_VERSION,
            "build_number": config.BUILD_NUMBER,
            "default_model": config.DEFAULT_MODEL,
            "models": get_combined_models(),
            "languages": config.LANGUAGE_OPTIONS,
            "directions": config.DIRECTION_OPTIONS,
            "columns": config.COLUMN_OPTIONS,
            "is_admin": is_admin,
            "access_gate_enabled": gate_enabled,
        }
    )

@app.get("/api/models")
async def get_models(refresh: bool = False):
    """取得或手動強制刷新 Google AI Studio 可用模型清單（包含 [Web] 網頁自動化模型）"""
    updated = False
    message = ""
    if refresh:
        updated, message = await update_cached_models(force=True)
    combined = get_combined_models()
    return {
        "models": combined,
        "count": len(combined),
        "updated": updated,
        "message": message
    }

# --- Accounts API ---

class ApiKeyAccountCreate(BaseModel):
    name: str
    api_key: str
    rpm_limit: int = 15
    is_paid: bool = False

class OAuthAccountCreate(BaseModel):
    name: str
    client_id: str
    client_secret: str
    refresh_token: str

@app.get("/api/accounts")
async def list_accounts():
    accounts = await database.get_accounts(include_secrets=False)
    return {"accounts": accounts}

@app.post("/api/accounts/api-key", dependencies=[Depends(require_access_or_admin)])
async def create_api_key_account(payload: ApiKeyAccountCreate):
    acc_id = await database.add_api_key_account(
        payload.name, payload.api_key, payload.rpm_limit, is_paid=1 if payload.is_paid else 0
    )
    # 新增金鑰後自動觸發更新模型清單與並發槽位
    spawn_background(update_cached_models())
    await free_ocr_task_manager.notify_account_change()
    pipeline_dispatcher.trigger()
    return {"status": "ok", "account_id": acc_id}

@app.post("/api/accounts/oauth", dependencies=[Depends(require_access_or_admin)])
async def create_oauth_account(payload: OAuthAccountCreate):
    acc_id = await database.add_oauth_account(payload.name, payload.client_id, payload.client_secret, payload.refresh_token)
    # 新增 OAuth 後自動觸發更新模型清單與並發槽位
    spawn_background(update_cached_models())
    await free_ocr_task_manager.notify_account_change()
    pipeline_dispatcher.trigger()
    return {"status": "ok", "account_id": acc_id}

@app.post("/api/accounts/{account_id}/toggle", dependencies=[Depends(require_access_or_admin)])
async def toggle_account(account_id: int):
    account = await database.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    new_state = not bool(account["is_active"])
    await database.toggle_account_active(account_id, new_state)
    await free_ocr_task_manager.notify_account_change()
    pipeline_dispatcher.trigger()
    return {"status": "ok", "is_active": new_state}

@app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_access_or_admin)])
async def delete_account(account_id: int):
    await database.delete_account(account_id)
    await free_ocr_task_manager.notify_account_change()
    pipeline_dispatcher.trigger()
    return {"status": "ok"}

@app.post("/api/accounts/{account_id}/test", dependencies=[Depends(require_access_or_admin)])
async def test_account(account_id: int, model: Optional[str] = None):
    account = await database.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    # 使用系統預設模型 (定義於 config.py 的 DEFAULT_MODEL) 進行驗證
    target_model = (model or config.DEFAULT_MODEL).replace("[PAID]", "").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {}
    
    if account["auth_type"] == "api_key":
        params["key"] = account["api_key"]
    else:
        try:
            token = await gemini_ocr.refresh_oauth_token_if_needed(account)
            headers["Authorization"] = f"Bearer {token}"
        except Exception as e:
            return {"status": "error", "message": f"OAuth Token 刷新失敗: {str(e)}"}
            
    payload = {"contents": [{"parts": [{"text": "Hello, respond with 'OK'"}]}]}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, params=params, json=payload)
            if resp.status_code == 200:
                return {
                    "status": "success", 
                    "model": target_model,
                    "message": f"連線成功！金鑰驗證通過（測試模型: {target_model}）。"
                }
            elif resp.status_code == 429:
                return {
                    "status": "warning", 
                    "model": target_model,
                    "message": f"此金鑰目前達到 Google 配額限制 (429 - {target_model})。"
                }
            else:
                return {
                    "status": "error", 
                    "model": target_model,
                    "message": f"驗證失敗 ({resp.status_code} - {target_model}): {resp.text}"
                }
    except Exception as e:
        return {"status": "error", "model": target_model, "message": f"連線異常: {str(e)}"}

# --- Web RPA Automation API ---

class WebRpaConfigPayload(BaseModel):
    enabled: Optional[bool] = None
    target_service: Optional[str] = None
    browser_mode: Optional[str] = None      # "cdp" | "launch"
    cdp_port: Optional[int] = None
    cdp_user_data_dir: Optional[str] = None
    headless: Optional[bool] = None
    timeout_seconds: Optional[int] = None
    chrome_path: Optional[str] = None

@app.get("/api/web-rpa/config")
async def get_web_rpa_config():
    """取得網頁自動化設定、支援模型清單、及登入快取狀態"""
    cfg = web_rpa.get_web_config()
    models = web_rpa.get_supported_web_models()
    login_info = web_rpa.get_login_status_info()
    return {"status": "ok", "config": cfg, "models": models, "login_info": login_info}

@app.post("/api/web-rpa/config", dependencies=[Depends(require_admin)])
async def update_web_rpa_config(payload: WebRpaConfigPayload):
    """更新網頁自動化設定（管理員專屬）"""
    if payload.chrome_path is not None:
        if not web_rpa.validate_chrome_path(payload.chrome_path):
            raise HTTPException(status_code=400, detail="不合法的 Chrome 執行檔路徑或非標準瀏覽器程式")
    cfg = web_rpa.get_web_config()
    for field in (
        "enabled", "target_service", "browser_mode", "cdp_port",
        "cdp_user_data_dir", "headless", "timeout_seconds", "chrome_path",
    ):
        value = getattr(payload, field, None)
        if value is not None:
            cfg[field] = value
    # 切換瀏覽器模式時，先把既有的 CDP 連線乾淨地脫離
    if payload.browser_mode is not None:
        try:
            await web_rpa.close_cdp_session()
        except Exception:
            pass
    web_rpa.save_web_config(cfg)
    return {"status": "ok", "config": cfg}

@app.post("/api/web-rpa/launch-login", dependencies=[Depends(require_admin)])
async def launch_web_login(service: Optional[str] = None):
    """以 Playwright 可見視窗開啟登入頁面（使用獨立 RPA Profile，解決 Keychain 加密問題）"""
    result = await web_rpa.launch_login_browser(target_service=service)
    if result.get("success"):
        return {"status": "ok", "message": result["message"]}
    else:
        raise HTTPException(status_code=500, detail=result.get("message", "開啟登入視窗失敗"))

@app.post("/api/web-rpa/save-state", dependencies=[Depends(require_admin)])
async def save_web_rpa_state():
    """從當前開啟的登入視窗提取 cookies 並保存為 storage_state.json，然後關閉登入視窗"""
    result = await web_rpa.save_storage_state()
    return result

@app.get("/api/web-rpa/status")
async def check_web_status():
    """取得登入快取狀態（輕量，不啟動瀏覽器）"""
    info = web_rpa.get_login_status_info()
    # 將 cached + key_cookie_count 轉為 logged_in 欄位供前端判斷
    info["logged_in"] = info.get("cached", False) and info.get("key_cookie_count", 0) > 0
    return info

@app.post("/api/web-rpa/verify", dependencies=[Depends(require_admin)])
async def verify_web_login():
    """用 headless 瀏覽器實際訪問 AI Studio 驗證 session 是否仍有效"""
    status_info = await web_rpa.check_login_status()
    return status_info

@app.post("/api/web-rpa/sync-cookies", dependencies=[Depends(require_admin)])
async def sync_web_rpa_cookies():
    """回傳當前登入快取狀態（相容舊版 UI 呼叫）"""
    info = web_rpa.get_login_status_info()
    info["logged_in"] = info.get("cached", False) and info.get("key_cookie_count", 0) > 0
    return info

@app.post("/api/web-rpa/test", dependencies=[Depends(require_admin)])
async def test_web_rpa(model: Optional[str] = None):
    """測試網頁端 OCR 連線運作（建立一張帶文字的測試圖）"""
    from PIL import Image, ImageDraw
    test_img = config.DATA_DIR / "test_dummy.png"
    img = Image.new("RGB", (400, 100), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 30), "OCR Test: Hello World 123", fill=(0, 0, 0))
    img.save(test_img)
    target_m = model or "[Web] gemini-3.7-flash"
    success, text, code = await web_rpa.run_web_ocr(
        image_path=test_img,
        model_name=target_m,
        prompt="請辨識圖中的所有文字，原樣輸出，不要加任何說明",
        timeout=90
    )
    if test_img.exists():
        test_img.unlink(missing_ok=True)
    return {"status": "ok" if success else "error", "message": text, "code": code}

# --- Web RPA: CDP 模式（接入使用者自己開的 Chrome）---

@app.get("/api/web-rpa/chrome-command")
async def get_chrome_command():
    """回傳使用者應該執行的 Chrome 啟動指令（供 UI 顯示／複製）"""
    return {
        "status": "ok",
        "chrome_path": web_rpa.get_web_config().get("chrome_path"),
        "cdp_port": web_rpa.get_cdp_port(),
        "cdp_endpoint": web_rpa.get_cdp_endpoint(),
        "profile_dir": str(web_rpa.get_cdp_profile_dir()),
        "command": web_rpa.get_chrome_launch_command(),
        "script": "./start_chrome_cdp.sh",
    }

@app.get("/api/web-rpa/cdp-status")
async def get_cdp_status():
    """
    檢查使用者是否已開好「可被接入的 Chrome」。
    只讀探測 http://127.0.0.1:<port>/json/version，不會啟動任何瀏覽器。
    """
    info = await web_rpa.check_cdp_available()
    info["browser_mode"] = web_rpa.get_web_config().get("browser_mode", "cdp")
    return info

@app.post("/api/web-rpa/cdp-connect", dependencies=[Depends(require_admin)])
async def connect_cdp():
    """
    實際接入使用者的 Chrome，並開一個 GASOCR 專用分頁驗證 AI Studio 登入狀態。
    注意：此端點不會關閉使用者的瀏覽器。
    """
    return await web_rpa.check_cdp_login()

@app.post("/api/web-rpa/cdp-disconnect", dependencies=[Depends(require_admin)])
async def disconnect_cdp():
    """主動脫離 CDP 連線（您的 Chrome 會保持開啟）"""
    return await web_rpa.close_cdp_session()

# --- Task & OCR API ---

class FileHashCheckRequest(BaseModel):
    hash: str
    filename: Optional[str] = ""
    size: Optional[int] = 0

@app.post("/api/files/check-hash")
async def check_file_hash(payload: FileHashCheckRequest):
    """檢查伺服器端是否已存在完全相同的檔案快取（基於 SHA-256）"""
    cached = await database.get_file_by_hash(payload.hash)
    if cached:
        return {
            "exists": True,
            "hash": payload.hash,
            "filename": cached["filename"],
            "filepath": cached["filepath"],
            "size": cached.get("file_size", 0)
        }
    return {"exists": False, "hash": payload.hash}

@app.get("/api/tasks")
async def get_all_tasks():
    tasks = await database.list_tasks()
    return {"tasks": tasks}

@app.post("/api/upload/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    chunk: UploadFile = File(...)
):
    """
    大檔案分塊切片上傳端點 (Chunked Upload)：
    - 徹底突破 Cloudflare 100MB 限制與 Nginx Body Size 限制
    - 逐塊以 append 模式寫入暫存檔 .incoming_chunk_{upload_id}.part
    - 最後一塊上傳完成時：
      1. 驗證完整 PDF 與磁碟剩餘空間
      2. 計算 SHA-256 特徵碼
      3. 若資料庫已有相同快取，直接重用快取路徑
      4. 否則移至正式 uploads 目錄並建立快取索引
      5. 回傳 file_hash、filepath 與 filename，供前端直接建立任務
    """
    clean_upload_id = "".join(c for c in upload_id if c.isalnum() or c in "_-")
    if not clean_upload_id:
        raise HTTPException(status_code=400, detail="無效的 upload_id")
        
    safe_name = _safe_filename(filename)
    if not safe_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只接受 .pdf 檔案")

    part_path = config.UPLOADS_DIR / f".incoming_chunk_{clean_upload_id}.part"
    
    # 檢查磁碟剩餘空間
    reserve = config.MIN_FREE_DISK_MB * 1024 * 1024
    free = _free_disk_bytes(config.DATA_DIR)
    if free >= 0 and free < reserve:
        part_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=507,
            detail=f"伺服器磁碟空間不足（剩餘 {free / 1048576:.0f} MB，需保留至少 {config.MIN_FREE_DISK_MB} MB）"
        )

    # 第一塊上傳時若已有殘留舊檔則清空重來
    mode = "wb" if chunk_index == 0 else "ab"
    try:
        with open(part_path, mode) as f:
            while True:
                data = await chunk.read(1024 * 1024)
                if not data:
                    break
                f.write(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入分塊失敗: {e}")

    # 若尚未到達最後一塊，回傳接收成功
    if chunk_index < total_chunks - 1:
        return {
            "status": "chunk_received",
            "upload_id": clean_upload_id,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks
        }

    # 最後一塊已寫入完畢，計算全檔特徵碼與大小
    try:
        total_size = part_path.stat().st_size
        f_hash = database.compute_file_sha256(part_path)
    except Exception as e:
        part_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"計算檔案特徵碼失敗: {e}")

    # 檢查是否已存在相同內容的快取檔案
    cached = await database.get_file_by_hash(f_hash)
    if cached and Path(cached["filepath"]).exists():
        part_path.unlink(missing_ok=True)
        return {
            "status": "completed",
            "upload_id": clean_upload_id,
            "file_hash": f_hash,
            "cached": True,
            "filepath": cached["filepath"],
            "filename": safe_name,
            "total_size": total_size
        }

    # 移至正式 uploads 目錄
    final_task_id = f"task_{uuid.uuid4().hex[:10]}"
    final_path = config.UPLOADS_DIR / f"{final_task_id}_{safe_name}"
    try:
        part_path.replace(final_path)
        await database.save_file_hash(f_hash, safe_name, str(final_path), total_size)
    except Exception as e:
        part_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"儲存完整檔案失敗: {e}")

    return {
        "status": "completed",
        "upload_id": clean_upload_id,
        "file_hash": f_hash,
        "cached": False,
        "filepath": str(final_path),
        "filename": safe_name,
        "total_size": total_size
    }

@app.post("/api/tasks")
async def create_ocr_task(
    background_tasks: BackgroundTasks,
    files: Optional[List[UploadFile]] = File(default=None),
    existing_files: Optional[str] = Form(None),
    model: str = Form(config.DEFAULT_MODEL),
    lang: str = Form("traditional"),
    direction: str = Form("auto"),
    column: str = Form("auto"),
    custom_prompt: str = Form(""),
    start_page: int = Form(1),
    end_page: int = Form(0),
    use_paid_model: bool = Form(False),
    paid_account_id: Optional[int] = Form(None)
):
    created_tasks = []
    
    failed_files: List[Dict[str, str]] = []

    # 1. 處理伺服器端已快取的免上傳檔案
    if existing_files:
        try:
            cached_list = json.loads(existing_files)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"existing_files 格式錯誤: {e}")

        for item in cached_list:
            f_hash = item.get("hash")
            cached = await database.get_file_by_hash(f_hash) if f_hash else None
            if not cached:
                failed_files.append({
                    "name": item.get("filename") or "(未命名)",
                    "error": "伺服器端找不到此快取，請重新上傳該檔案"
                })
                continue

            filepath = cached["filepath"]
            if not Path(filepath).exists():
                await database.delete_file_hash_by_path(filepath)
                failed_files.append({
                    "name": item.get("filename") or "(未命名)",
                    "error": "快取檔案已不存在，請重新上傳"
                })
                continue

            task_id = f"task_{uuid.uuid4().hex[:10]}"
            filename = item.get("filename") or cached["filename"]

            # PDF 解析是同步且可能耗時（大檔可達數秒），丟到執行緒避免卡住事件迴圈
            pdf_total_pages = await asyncio.to_thread(get_pdf_page_count, Path(filepath))
            if pdf_total_pages <= 0:
                failed_files.append({
                    "name": filename,
                    "error": "PDF 無法解析或頁數為 0（檔案可能損毀、加密或非 PDF）"
                })
                continue

            await database.create_task(
                task_id=task_id,
                filename=filename,
                filepath=filepath,
                model=model,
                lang=lang,
                direction=direction,
                column=column,
                custom_prompt=custom_prompt,
                start_page=start_page,
                end_page=end_page,
                is_paid=1 if use_paid_model else 0,
                paid_account_id=paid_account_id if (use_paid_model and paid_account_id and paid_account_id > 0) else None,
                pdf_total_pages=pdf_total_pages,
                file_hash=f_hash
            )
            spawn_background(process_task_pipeline(task_id))
            created_tasks.append(task_id)

    # 2. 處理使用者新上傳的檔案
    for file in (files or []):
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            failed_files.append({
                "name": file.filename or "(未命名)",
                "error": "只接受 .pdf 檔案"
            })
            continue

        task_id = f"task_{uuid.uuid4().hex[:10]}"
        tmp_path = config.UPLOADS_DIR / f".incoming_{task_id}.part"

        # 串流寫入暫存檔並同時計算 SHA-256（不把整個檔案讀進記憶體）
        try:
            f_hash, total_bytes = await _stream_upload_to_disk(file, tmp_path)
        except HTTPException as he:
            failed_files.append({"name": file.filename, "error": str(he.detail)})
            continue
        except Exception as e:
            failed_files.append({"name": file.filename, "error": f"寫入檔案失敗: {e}"})
            continue

        created_new = False
        try:
            cached = await database.get_file_by_hash(f_hash)
            if cached and Path(cached["filepath"]).exists():
                # 伺服器已有相同內容 → 直接重用，丟棄這次上傳的暫存檔
                saved_path = Path(cached["filepath"])
                tmp_path.unlink(missing_ok=True)
            else:
                saved_path = config.UPLOADS_DIR / f"{task_id}_{_safe_filename(file.filename)}"
                tmp_path.replace(saved_path)
                created_new = True
                await database.save_file_hash(
                    f_hash, file.filename, str(saved_path), total_bytes
                )

            pdf_total_pages = await asyncio.to_thread(get_pdf_page_count, saved_path)
            if pdf_total_pages <= 0:
                # 不要建立一個永遠不會完成的任務（先前會留下 pending 卡死）
                if created_new:
                    try:
                        saved_path.unlink(missing_ok=True)
                        await database.delete_file_hash_by_path(str(saved_path))
                    except Exception:
                        pass
                failed_files.append({
                    "name": file.filename,
                    "error": "PDF 無法解析或頁數為 0（檔案可能損毀、加密或非 PDF）"
                })
                continue

            await database.create_task(
                task_id=task_id,
                filename=file.filename,
                filepath=str(saved_path),
                model=model,
                lang=lang,
                direction=direction,
                column=column,
                custom_prompt=custom_prompt,
                start_page=start_page,
                end_page=end_page,
                is_paid=1 if use_paid_model else 0,
                paid_account_id=paid_account_id if (use_paid_model and paid_account_id and paid_account_id > 0) else None,
                pdf_total_pages=pdf_total_pages,
                file_hash=f_hash
            )
            spawn_background(process_task_pipeline(task_id))
            created_tasks.append(task_id)
        except Exception as e:
            failed_files.append({"name": file.filename, "error": f"建立任務失敗: {e}"})
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    if not created_tasks:
        detail = "未收到有效的 PDF 檔案或快取資訊"
        if failed_files:
            detail += "；" + "；".join(
                f"{f['name']}: {f['error']}" for f in failed_files[:5]
            )
        raise HTTPException(status_code=400, detail=detail)
        
    pipeline_dispatcher.trigger()
    return {
        "status": "ok", 
        "task_ids": created_tasks, 
        "count": len(created_tasks), 
        "task_id": created_tasks[0],
        "failed": failed_files,
        "failed_count": len(failed_files)
    }

@app.delete("/api/tasks/{task_id}")
async def delete_ocr_task(task_id: str):
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # 即時取消任何正在執行的背景任務 (切圖/OCR)
    cancel_task_active_jobs(task_id)
    
    await database.delete_task(task_id)
    try:
        orig_fp = task.get("original_filepath")
        if orig_fp:
            async with database.get_db() as db:
                c = await db.execute("SELECT COUNT(*) FROM tasks WHERE original_filepath = ? AND id != ?", (orig_fp, task_id))
                other_count = (await c.fetchone())[0]
            if other_count == 0:
                await database.delete_file_hash_by_path(orig_fp)
                if os.path.exists(orig_fp):
                    os.remove(orig_fp)
        render_dir = config.RENDERS_DIR / task_id
        if render_dir.exists():
            import shutil
            shutil.rmtree(render_dir)
    except Exception as e:
        print(f"Error cleaning task files {task_id}: {e}")
    pipeline_dispatcher.trigger()
    return {"status": "ok"}

class BatchDeleteTasksRequest(BaseModel):
    task_ids: List[str]

@app.post("/api/tasks/batch-delete")
async def batch_delete_ocr_tasks(payload: BatchDeleteTasksRequest):
    """批次刪除指定的一組 OCR 任務並清理相關切圖與殘留快取"""
    clean_ids = [tid.strip() for tid in payload.task_ids if tid and tid.strip()]
    if not clean_ids:
        return {"status": "ok", "deleted_count": 0, "deleted_ids": []}

    deleted_ids = []
    # 逐一檢查任務與其 original_filepath
    for tid in clean_ids:
        task = await database.get_task(tid)
        if not task:
            continue
        
        # 即時取消任何正在執行的背景任務 (切圖/OCR)
        cancel_task_active_jobs(tid)
        
        deleted_ids.append(tid)
        orig_fp = task.get("original_filepath")
        
        # 清理切圖目錄
        try:
            render_dir = config.RENDERS_DIR / tid
            if render_dir.exists():
                shutil.rmtree(render_dir)
        except Exception as e:
            print(f"Error cleaning render dir {tid}: {e}")
            
        # 檢查 original_filepath 是否尚被其他任務引用（需排除這批要刪除的所有任務）
        if orig_fp:
            try:
                placeholders = ",".join("?" for _ in clean_ids)
                async with database.get_db() as db:
                    c = await db.execute(
                        f"SELECT COUNT(*) FROM tasks WHERE original_filepath = ? AND id NOT IN ({placeholders})",
                        [orig_fp] + clean_ids
                    )
                    other_count = (await c.fetchone())[0]
                if other_count == 0:
                    await database.delete_file_hash_by_path(orig_fp)
                    if os.path.exists(orig_fp):
                        os.remove(orig_fp)
            except Exception as e:
                print(f"Error checking/cleaning original file {orig_fp}: {e}")

    if deleted_ids:
        await database.delete_tasks(deleted_ids)

    pipeline_dispatcher.trigger()
    return {
        "status": "ok", 
        "deleted_count": len(deleted_ids), 
        "deleted_ids": deleted_ids
    }

class BatchTaskActionRequest(BaseModel):
    task_ids: Optional[List[str]] = None

@app.post("/api/tasks/pause-all")
async def pause_all_tasks(payload: Optional[BatchTaskActionRequest] = None):
    """
    全局/批次暫停 OCR 任務（切圖與辨識雙階段即時中止）：
    - 若指定 task_ids，僅暫停指定任務中處於 processing, rendering, pending 的任務
    - 若未指定，暫停全系統所有處於 processing, rendering, pending 的任務
    """
    clean_ids = [tid.strip() for tid in payload.task_ids if tid and tid.strip()] if (payload and payload.task_ids) else None
    
    async with database.get_db() as db:
        if clean_ids:
            placeholders = ",".join("?" for _ in clean_ids)
            cursor = await db.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({placeholders}) AND status IN ('processing', 'rendering', 'pending', 'pending_render', 'pending_ocr')",
                clean_ids
            )
        else:
            cursor = await db.execute("SELECT id, status FROM tasks WHERE status IN ('processing', 'rendering', 'pending', 'pending_render', 'pending_ocr')")
        rows = await cursor.fetchall()
        
    target_tasks = [dict(r) for r in rows]
    paused_ids = []
    for t in target_tasks:
        tid = t["id"]
        cancel_task_active_jobs(tid)
        await database.update_task_status(tid, status="paused", bg_render_status="paused")
        await database.pause_task_in_progress_pages(tid)
        paused_ids.append(tid)
        print(f"⏸️ [Pause All] 任務 {tid} 已即時暫停切圖與 OCR")
        
    pipeline_dispatcher.trigger()
    return {
        "status": "ok",
        "paused_count": len(paused_ids),
        "task_ids": paused_ids
    }

@app.post("/api/tasks/resume-all")
async def resume_all_tasks(payload: Optional[BatchTaskActionRequest] = None):
    """
    全局/批次接續 OCR 任務（自動重置未完成頁面並重啟流水線）：
    - 若指定 task_ids，僅接續指定任務中處於 paused, failed, pending, pending_render, pending_ocr 的任務
    - 若未指定，接續全系統所有處於 paused, failed, pending, pending_render, pending_ocr 的任務
    """
    clean_ids = [tid.strip() for tid in payload.task_ids if tid and tid.strip()] if (payload and payload.task_ids) else None
    
    async with database.get_db() as db:
        if clean_ids:
            placeholders = ",".join("?" for _ in clean_ids)
            cursor = await db.execute(
                f"SELECT id, status, model FROM tasks WHERE id IN ({placeholders}) AND status IN ('paused', 'failed', 'pending', 'pending_render', 'pending_ocr')",
                clean_ids
            )
        else:
            cursor = await db.execute("SELECT id, status, model FROM tasks WHERE status IN ('paused', 'failed', 'pending', 'pending_render', 'pending_ocr')")
        rows = await cursor.fetchall()
        
    target_tasks = [dict(r) for r in rows]
    resumed_ids = []
    available_render_slots = max(0, RENDER_SEMAPHORE._value)

    for t in target_tasks:
        tid = t["id"]
        task_data = await database.get_task(tid) or t
        await database.resume_task_paused_pages(tid)

        rendering_done = is_task_rendering_completed(task_data)
        if not rendering_done:
            if available_render_slots > 0:
                target_status = "rendering"
                target_bg_status = "rendering"
                available_render_slots -= 1
            else:
                target_status = "pending_render"
                target_bg_status = "pending"
        else:
            is_paid_task = bool(task_data.get("is_paid"))
            free_slots_full = (not is_paid_task) and (len(free_ocr_task_manager.active_tasks) >= await free_ocr_task_manager.get_max_concurrent())
            target_status = "pending_ocr" if free_slots_full else "processing"
            target_bg_status = "completed"

        await database.update_task_status(tid, status=target_status, bg_render_status=target_bg_status)
        spawn_background(process_task_pipeline(tid))
        resumed_ids.append(tid)
        print(f"▶️ [Resume All] 任務 {tid} 已重啟轉譯流水線 (狀態: {target_status})")
        
    pipeline_dispatcher.trigger()
    return {
        "status": "ok",
        "resumed_count": len(resumed_ids),
        "task_ids": resumed_ids
    }

@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    """暫停指定 OCR 任務（涵蓋切圖階段與 OCR 辨識階段）"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    # 1. 即時中斷該任務所有正在運行的背景任務（切圖流水線、背景高清渲染、在途單頁 OCR）
    cancel_task_active_jobs(task_id)

    # 2. 更新任務狀態與背景渲染狀態為 paused
    await database.update_task_status(task_id, status="paused", bg_render_status="paused")
    
    # 3. 將當前處於 rendering 或 processing 狀態的頁面改為 paused
    await database.pause_task_in_progress_pages(task_id)
    
    print(f"⏸️ [Task {task_id}] 使用者手動暫停任務成功（已即時中斷切圖與 OCR 流程）")
    pipeline_dispatcher.trigger()
    return {"status": "ok", "task_status": "paused"}

class ResumeTaskPayload(BaseModel):
    model: Optional[str] = None

class TaskModelUpdate(BaseModel):
    model: str

@app.patch("/api/tasks/{task_id}/model")
async def update_task_model_endpoint(task_id: str, payload: TaskModelUpdate):
    """更新指定任務的 Gemini 模型"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await database.update_task_model(task_id, payload.model)
    return {"status": "ok", "task_id": task_id, "model": payload.model}

@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, payload: Optional[ResumeTaskPayload] = None):
    """接續執行已暫停或失敗的 OCR 任務，支援即時更換模型（支援切圖續切與 OCR 續傳）"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    new_model = payload.model if (payload and payload.model) else None
    if new_model:
        await database.update_task_model(task_id, new_model)
    if task["status"] in ["paused", "failed", "pending", "pending_render", "pending_ocr"]:
        # 將被手動暫停的頁面重置回 pending 以利接續
        await database.resume_task_paused_pages(task_id)
        
        # 務必先檢查切圖完成了嗎？若切圖未完成，threads又滿了的話，顯示等待切圖 (pending_render)
        rendering_done = is_task_rendering_completed(task)
        threads_full = (RENDER_SEMAPHORE._value <= 0)
        
        if not rendering_done:
            target_status = "pending_render" if threads_full else "rendering"
            target_bg_status = "pending" if threads_full else "rendering"
            print(f"▶️ [Resume Task] 任務 {task_id} 切圖未完成，threads_full={threads_full} -> 設定狀態為 {target_status}")
        else:
            is_paid_task = bool(task.get("is_paid"))
            free_slots_full = (not is_paid_task) and (len(free_ocr_task_manager.active_tasks) >= await free_ocr_task_manager.get_max_concurrent())
            target_status = "pending_ocr" if free_slots_full else "processing"
            target_bg_status = "completed"
            print(f"▶️ [Resume Task] 任務 {task_id} 切圖已完成，free_slots_full={free_slots_full} -> 設定狀態為 {target_status}")

        await database.update_task_status(task_id, status=target_status, bg_render_status=target_bg_status, model=new_model)
        spawn_background(process_task_pipeline(task_id))
        pipeline_dispatcher.trigger()
        return {
            "status": "ok", 
            "task_status": target_status, 
            "model": new_model or task.get("model")
        }
    return {
        "status": "ok", 
        "task_status": task.get("status"), 
        "model": new_model or task.get("model")
    }

@app.get("/api/tasks/{task_id}")
async def get_task_detail(task_id: str):
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    pages = await database.get_task_pages(task_id)
    
    # 轉換圖片路徑為靜態 URL
    formatted_pages = []
    for p in pages:
        d = dict(p)
        img_name = Path(p["image_path"]).name
        d["image_url"] = f"/renders/{task_id}/{img_name}"
        formatted_pages.append(d)
        
    return {"task": task, "pages": formatted_pages}

@app.put("/api/tasks/{task_id}/pages/{page_num}")
async def update_page_text(task_id: str, page_num: int, payload: Dict[str, str]):
    new_text = payload.get("text", "")
    await database.save_page_ocr_text(task_id, page_num, new_text)
    return {"status": "ok"}

class PageRetryPayload(BaseModel):
    model: Optional[str] = None
    lang: Optional[str] = None
    direction: Optional[str] = None
    column: Optional[str] = None

@app.post("/api/tasks/{task_id}/pages/{page_num}/retry")
async def retry_single_page(
    task_id: str, 
    page_num: int, 
    payload: Optional[PageRetryPayload] = None,
    model: Optional[str] = None,
    lang: Optional[str] = None,
    direction: Optional[str] = None,
    column: Optional[str] = None
):
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    pages = await database.get_task_pages(task_id)
    target_page = next((p for p in pages if p["page_num"] == page_num), None)
    if not target_page:
        raise HTTPException(status_code=404, detail="Page not found")
        
    # 若該頁面已有在執行中的背景任務，先優雅取消避免重複或衝突
    task_key = f"{task_id}_{page_num}"
    if task_key in active_page_tasks:
        active_page_tasks[task_key].cancel()
        active_page_tasks.pop(task_key, None)
        await asyncio.sleep(0.05)
        
    target_model = (payload.model if (payload and payload.model) else None) or model or target_page.get("used_model") or task.get("model", config.DEFAULT_MODEL)
    target_lang = (payload.lang if (payload and payload.lang) else None) or lang or task.get("lang_pref", "traditional")
    target_direction = (payload.direction if (payload and payload.direction) else None) or direction or task.get("direction_pref", "auto")
    target_column = (payload.column if (payload and payload.column) else None) or column or task.get("column_pref", "auto")

    prompt = build_ocr_prompt(
        lang=target_lang,
        direction=target_direction,
        column=target_column,
        custom_prompt=task.get("custom_prompt", "")
    )
    
    specific_acc_id = None
    if "[PAID]" in target_model:
        accounts = await database.get_accounts()
        paid_acc = next((a for a in accounts if a.get("is_paid") == 1 and a.get("is_active") == 1), None)
        if paid_acc:
            specific_acc_id = paid_acc["id"]
    elif task.get("is_paid") and task.get("paid_account_id"):
        specific_acc_id = task.get("paid_account_id")

    img_path = Path(target_page["image_path"])
    spawn_background(run_page_ocr(
        task_id=task_id, 
        page_num=page_num, 
        image_path=img_path, 
        model=target_model, 
        prompt=prompt,
        specific_account_id=specific_acc_id
    ))
    
    return {
        "status": "ok", 
        "message": f"第 {page_num} 頁重新轉譯已啟動，使用模型: {target_model}",
        "model": target_model
    }

@app.post("/api/tasks/{task_id}/pages/{page_num}/pause")
async def pause_single_page(task_id: str, page_num: int):
    """暫停指定單頁的重新辨識"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    task_key = f"{task_id}_{page_num}"
    cancelled = False
    if task_key in active_page_tasks:
        active_page_tasks[task_key].cancel()
        active_page_tasks.pop(task_key, None)
        cancelled = True
        
    await database.update_page_status(task_id, page_num, "paused", error_message="已手動暫停")
    return {"status": "ok", "message": f"第 {page_num} 頁轉譯已暫停", "cancelled": cancelled}

@app.post("/api/tasks/{task_id}/pages/{page_num}/resume")
async def resume_single_page(
    task_id: str, 
    page_num: int, 
    payload: Optional[PageRetryPayload] = None
):
    """繼續指定單頁的重新辨識"""
    return await retry_single_page(task_id, page_num, payload=payload)

# --- SSE Stream for Live Progress ---

@app.get("/api/tasks/{task_id}/events")
async def sse_task_events(task_id: str, request: Request = None):
    async def event_generator():
        while True:
            if request and await request.is_disconnected():
                break

            task = await database.get_task(task_id)
            if not task:
                break
                
            pages = await database.get_task_pages(task_id)
            
            # 計算富資訊指標
            durations = [p["duration_seconds"] for p in pages if p["status"] == "completed" and p.get("duration_seconds", 0) > 0]
            avg_speed = round(sum(durations) / len(durations), 1) if durations else 0.0
            
            total = task.get("total_pages") or 0
            processed = task.get("processed_pages") or 0
            rendered = task.get("rendered_pages") or 0
            remaining = max(0, total - processed)
            eta_seconds = round(remaining * avg_speed) if (avg_speed > 0 and remaining > 0) else 0
            
            active_p = next((p for p in pages if p["status"] == "processing"), None)
            active_page_num = active_p["page_num"] if active_p else None
            active_msg = active_p["error_message"] if active_p else ""
            if task["status"] == "rendering":
                active_msg = f"🎨 正在 300 DPI 高清切圖第 {min(rendered + 1, total if total > 0 else 1)} 頁 (共 {total} 頁)..."
            elif task["status"] == "pending_ocr":
                active_msg = "⏳ 切圖完成，排隊等待免費 OCR 槽位..."
            elif task["status"] == "pending_render":
                active_msg = "⏳ 佇列排隊中，等待切圖..."
            elif task["status"] == "pending":
                if rendered >= total and total > 0:
                    active_msg = "⏳ 切圖完成，排隊等待免費 OCR 槽位..."
                else:
                    active_msg = "⏳ 佇列排隊中，等待切圖..."

            data_payload = {
                "task_id": task_id,
                "status": task["status"],
                "total_pages": total,
                "processed_pages": processed,
                "rendered_pages": rendered,
                "pdf_total_pages": task.get("pdf_total_pages") or total,
                "bg_render_status": task.get("bg_render_status", "idle"),
                "start_page": task.get("start_page", 1),
                "end_page": task.get("end_page", 0),
                "model": task.get("model", ""),
                "is_paid": task.get("is_paid", 0),
                "avg_speed": avg_speed,
                "eta_seconds": eta_seconds,
                "active_page": active_page_num,
                "active_msg": active_msg,
                "pages": [
                    {
                        "page_num": p["page_num"],
                        "status": p["status"],
                        "error_message": p["error_message"],
                        "has_text": bool(p["ocr_text"]),
                        "duration": p["duration_seconds"],
                        "used_model": p.get("used_model")
                    }
                    for p in pages
                ]
            }

            quota_status = await scheduler.get_quota_status()
            data_payload["quota_exhausted"] = quota_status.get("all_free_exhausted", False)
            data_payload["quota_reset_info"] = quota_status.get("reset_info", {})

            yield f"data: {json.dumps(data_payload)}\n\n"
            
            # 若任務已完成或失敗，且目前沒有任何頁面仍在 processing，才結束串流
            if task["status"] in ["completed", "failed"]:
                if not any(p.get("status") == "processing" for p in pages):
                    break
            await asyncio.sleep(1.2)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# --- Google AI Studio Free Quota Tracking & Status ---

@app.get("/api/quota/status")
async def get_quota_status():
    """取得 Google AI Studio 免費金鑰池額度狀態、連續錯誤次數與每日美西重置倒數資訊"""
    return await scheduler.get_quota_status()

@app.post("/api/quota/reset-tracking")
async def reset_quota_tracking():
    """手動重置金鑰連續錯誤次數（如使用者新增新金鑰或手動接續任務）"""
    await scheduler.reset_quota_tracking()
    return {"status": "ok", "message": "免費金鑰連續錯誤計數已重置"}

# --- Export & Download ---

@app.get("/api/tasks/{task_id}/download/{fmt}")
async def download_export(task_id: str, fmt: str):
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    pages = await database.get_task_pages(task_id)
    filename = Path(task["filename"]).stem
    
    if fmt == "md":
        file_path = exporters.export_markdown(task, pages)
        return FileResponse(file_path, filename=f"{filename}_OCR.md", media_type="text/markdown")
    elif fmt == "txt":
        file_path = exporters.export_txt(task, pages)
        return FileResponse(file_path, filename=f"{filename}_OCR.txt", media_type="text/plain")
    elif fmt == "docx":
        file_path = exporters.export_docx(task, pages)
        return FileResponse(
            file_path,
            filename=f"{filename}_OCR.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    elif fmt == "zip":
        file_path = exporters.export_zip_bundle(task, pages)
        return FileResponse(file_path, filename=f"{filename}_OCR_bundle.zip", media_type="application/zip")
    else:
        raise HTTPException(status_code=400, detail="不支援的匯出格式")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)
