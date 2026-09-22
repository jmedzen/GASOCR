import os
import time
import uuid
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx

import database
import config
from scheduler import scheduler
from pdf_engine import render_pdf_to_images, render_single_page
import gemini_ocr
from gemini_ocr import build_ocr_prompt, call_gemini_ocr, refresh_oauth_token_if_needed
import exporters
import web_rpa

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
        accounts = await database.get_accounts()
        active_accounts = [acc for acc in accounts if acc.get("is_active") == 1]
        if not active_accounts:
            return False, "尚未設定啟用的 Google 帳號或 API Key，顯示預設模型清單"
        
        # 嘗試使用第一組可用的啟用帳號向 Google 抓取最新清單
        for acc in active_accounts:
            success, models, msg = await gemini_ocr.fetch_google_models(acc)
            if success and models:
                CACHED_MODELS = models
                print(f"✅ 成功從 Google AI Studio 自動抓取 {len(models)} 個 Gemini 模型")
                return True, f"成功更新 {len(models)} 個最新模型"
        return False, f"嘗試向 Google AI Studio 請求模型清單未果，維持現有清單"
    except Exception as e:
        return False, f"更新模型清單異常: {str(e)}"

# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化資料庫
    await database.init_db()
    # API 上線後自動背景抓取最新模型清單
    asyncio.create_task(update_cached_models())
    yield

app = FastAPI(title="Google AI Studio Gemini OCR", lifespan=lifespan)

# Templates & Static
TEMPLATES_DIR = config.BASE_DIR / "templates"
STATIC_DIR = config.BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/renders", StaticFiles(directory=str(config.RENDERS_DIR)), name="renders")

# --- Background OCR Worker ---

active_page_tasks: Dict[str, asyncio.Task] = {}

async def run_page_ocr(task_id: str, page_num: int, image_path: Path, model: str, prompt: str, max_retries: int = 3):
    """執行單頁 OCR 並配合排程器冷卻切換，支援任務追蹤與即時暫停"""
    task_key = f"{task_id}_{page_num}"
    active_page_tasks[task_key] = asyncio.current_task()
    
    try:
        await database.update_page_status(task_id, page_num, "processing")
        
        # 1. 若為網頁自動化模型，調用本機網頁自動化 RPA
        if web_rpa.is_web_model(model):
            start_time = time.time()
            print(f"🌐 [Page OCR Web RPA] 任務 {task_id} 第 {page_num} 頁透過網頁自動化調用: {model}")
            success, text_or_err, status_code = await web_rpa.run_web_ocr(
                image_path=image_path,
                model_name=model,
                prompt=prompt
            )
            duration = round(time.time() - start_time, 2)
            if success:
                await database.update_page_result(
                    task_id, page_num, "completed",
                    ocr_text=text_or_err,
                    account_id=None,
                    duration=duration,
                    used_model=model
                )
                return
            else:
                await database.update_page_result(
                    task_id, page_num, "failed",
                    error_message=f"[Web RPA] {text_or_err}",
                    account_id=None,
                    duration=duration,
                    used_model=model
                )
                return

        retries = 0
        while retries < max_retries:
            # 2. 向智慧排程器索取可用帳號
            account = await scheduler.get_next_available_account()
            if not account:
                # 等待冷卻中的帳號解除
                account = await scheduler.wait_for_any_account(max_wait_seconds=60.0)
                
            if not account:
                await database.update_page_result(
                    task_id, page_num, "failed",
                    error_message="無可用的 Google 帳號或 API Key。請至「帳號管理」新增或檢查帳號狀態。"
                )
                return
                
            start_time = time.time()
            print(f"🚀 [Page OCR] 任務 {task_id} 第 {page_num} 頁使用模型: {model} (帳號: {account['name']})")
            success, text_or_err, status_code = await call_gemini_ocr(account, image_path, model, prompt)
            duration = round(time.time() - start_time, 2)
            
            if success:
                await scheduler.report_success(account["id"])
                await database.update_page_result(
                    task_id, page_num, "completed",
                    ocr_text=text_or_err,
                    account_id=account["id"],
                    duration=duration,
                    used_model=model
                )
                return
            elif status_code == 429:
                # 觸發 429 限額，登記該帳號冷卻，並以其他帳號重試本頁
                await scheduler.report_rate_limited(account["id"])
                retries += 1
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
                        used_model=model
                    )
                    return
                await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        print(f"⏸️ [Page OCR] 任務 {task_id} 第 {page_num} 頁辨識已被使用者暫停")
        await database.update_page_status(task_id, page_num, "paused", error_message="已手動暫停")
        raise
    except Exception as e:
        print(f"❌ [Page OCR] 任務 {task_id} 第 {page_num} 頁例外: {e}")
        await database.update_page_status(task_id, page_num, "failed", error_message=str(e))
    finally:
        active_page_tasks.pop(task_key, None)

async def process_task_pipeline(task_id: str):
    """整個任務的流水線處理（支援初次執行與暫停後接續）"""
    task = await database.get_task(task_id)
    if not task:
        return
        
    try:
        pages = await database.get_task_pages(task_id)
        if not pages:
            # 1. 初次啟動：渲染 PDF 頁面為圖片
            await database.update_task_status(task_id, "rendering")
            pdf_path = Path(task["original_filepath"])
            s_page = task.get("start_page", 1) or 1
            e_page = task.get("end_page", 0) or 0
            rendered_pages = render_pdf_to_images(pdf_path, task_id, start_page=s_page, end_page=e_page)
            
            total_pages = len(rendered_pages)
            pages_data = [
                {"task_id": task_id, "page_num": p_num, "image_path": str(img_path)}
                for p_num, img_path in rendered_pages
            ]
            await database.create_task_pages(pages_data)

            # 檢查在切圖渲染期間，使用者是否已按了暫停或刪除
            check_task = await database.get_task(task_id)
            if not check_task or check_task.get("status") in ["paused", "failed"]:
                return

            await database.update_task_status(task_id, "processing", total_pages=total_pages, processed_pages=0)
            pages = await database.get_task_pages(task_id)
        else:
            # 2. 暫停接續：直接切換為 processing
            await database.update_task_status(task_id, "processing")
        
        # 3. 準備 Prompt (重新拉取 task 以取得最新可能被變更之模型與設定)
        task = await database.get_task(task_id) or task
        prompt = build_ocr_prompt(
            lang=task.get("lang_pref", "traditional"),
            direction=task.get("direction_pref", "auto"),
            column=task.get("column_pref", "auto"),
            custom_prompt=task.get("custom_prompt", "")
        )
        
        # 4. 逐頁進行 OCR 辨識（跳過已完成的頁面）
        for p in pages:
            if p["status"] == "completed":
                continue
                
            # 檢查任務是否已被刪除或暫停
            current_task = await database.get_task(task_id)
            if not current_task or current_task.get("status") in ["paused", "failed"]:
                return
                
            model_to_use = current_task.get("model") or task.get("model", config.DEFAULT_MODEL)
            await run_page_ocr(task_id, p["page_num"], Path(p["image_path"]), model_to_use, prompt)
            
        # 5. 全部處理完成檢查
        if await database.get_task(task_id):
            latest_pages = await database.get_task_pages(task_id)
            if all(lp["status"] == "completed" for lp in latest_pages):
                await database.update_task_status(task_id, "completed")
        
    except Exception as e:
        if await database.get_task(task_id):
            print(f"Task {task_id} pipeline error: {e}")
            await database.update_task_status(task_id, "failed")

# --- Web UI Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "models": get_combined_models(),
            "languages": config.LANGUAGE_OPTIONS,
            "directions": config.DIRECTION_OPTIONS,
            "columns": config.COLUMN_OPTIONS,
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

class OAuthAccountCreate(BaseModel):
    name: str
    client_id: str
    client_secret: str
    refresh_token: str

@app.get("/api/accounts")
async def list_accounts():
    accounts = await database.get_accounts()
    return {"accounts": accounts}

@app.post("/api/accounts/api-key")
async def create_api_key_account(payload: ApiKeyAccountCreate):
    acc_id = await database.add_api_key_account(payload.name, payload.api_key, payload.rpm_limit)
    # 新增金鑰後自動觸發更新模型清單
    asyncio.create_task(update_cached_models())
    return {"status": "ok", "account_id": acc_id}

@app.post("/api/accounts/oauth")
async def create_oauth_account(payload: OAuthAccountCreate):
    acc_id = await database.add_oauth_account(payload.name, payload.client_id, payload.client_secret, payload.refresh_token)
    # 新增 OAuth 後自動觸發更新模型清單
    asyncio.create_task(update_cached_models())
    return {"status": "ok", "account_id": acc_id}

@app.post("/api/accounts/{account_id}/toggle")
async def toggle_account(account_id: int):
    account = await database.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    new_state = not bool(account["is_active"])
    await database.toggle_account_active(account_id, new_state)
    return {"status": "ok", "is_active": new_state}

@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int):
    await database.delete_account(account_id)
    return {"status": "ok"}

@app.post("/api/accounts/{account_id}/test")
async def test_account(account_id: int):
    account = await database.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    # 測試發送一個極小的測試請求
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
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
                return {"status": "success", "message": "連線成功！金鑰驗證通過。"}
            elif resp.status_code == 429:
                return {"status": "warning", "message": "此金鑰目前達到 Google 配額限制 (429)。"}
            else:
                return {"status": "error", "message": f"驗證失敗 ({resp.status_code}): {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": f"連線異常: {str(e)}"}

# --- Web RPA Automation API ---

class WebRpaConfigPayload(BaseModel):
    enabled: Optional[bool] = None
    target_service: Optional[str] = None
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

@app.post("/api/web-rpa/config")
async def update_web_rpa_config(payload: WebRpaConfigPayload):
    """更新網頁自動化設定"""
    cfg = web_rpa.get_web_config()
    if payload.enabled is not None:
        cfg["enabled"] = payload.enabled
    if payload.target_service is not None:
        cfg["target_service"] = payload.target_service
    if payload.headless is not None:
        cfg["headless"] = payload.headless
    if payload.timeout_seconds is not None:
        cfg["timeout_seconds"] = payload.timeout_seconds
    if payload.chrome_path is not None:
        cfg["chrome_path"] = payload.chrome_path
    web_rpa.save_web_config(cfg)
    return {"status": "ok", "config": cfg}

@app.post("/api/web-rpa/launch-login")
async def launch_web_login(service: Optional[str] = None):
    """以 Playwright 可見視窗開啟登入頁面（使用獨立 RPA Profile，解決 Keychain 加密問題）"""
    result = await web_rpa.launch_login_browser(target_service=service)
    if result.get("success"):
        return {"status": "ok", "message": result["message"]}
    else:
        raise HTTPException(status_code=500, detail=result.get("message", "開啟登入視窗失敗"))

@app.post("/api/web-rpa/save-state")
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

@app.post("/api/web-rpa/verify")
async def verify_web_login():
    """用 headless 瀏覽器實際訪問 AI Studio 驗證 session 是否仍有效"""
    status_info = await web_rpa.check_login_status()
    return status_info

@app.post("/api/web-rpa/sync-cookies")
async def sync_web_rpa_cookies():
    """回傳當前登入快取狀態（相容舊版 UI 呼叫）"""
    info = web_rpa.get_login_status_info()
    info["logged_in"] = info.get("cached", False) and info.get("key_cookie_count", 0) > 0
    return info

@app.post("/api/web-rpa/test")
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

# --- Task & OCR API ---

@app.get("/api/tasks")
async def get_all_tasks():
    tasks = await database.list_tasks()
    return {"tasks": tasks}

@app.post("/api/tasks")
async def create_ocr_task(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    model: str = Form(config.DEFAULT_MODEL),
    lang: str = Form("traditional"),
    direction: str = Form("auto"),
    column: str = Form("auto"),
    custom_prompt: str = Form(""),
    start_page: int = Form(1),
    end_page: int = Form(0)
):
    if not files:
        raise HTTPException(status_code=400, detail="請上傳至少一個 PDF 檔案")
        
    created_tasks = []
    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            continue
            
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        saved_filename = f"{task_id}_{file.filename}"
        saved_path = config.UPLOADS_DIR / saved_filename
        
        content = await file.read()
        saved_path.write_bytes(content)
        
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
            end_page=end_page
        )
        
        # 立即非同步啟動後台流水線
        asyncio.create_task(process_task_pipeline(task_id))
        created_tasks.append(task_id)
        
    if not created_tasks:
        raise HTTPException(status_code=400, detail="未上傳有效的 PDF 檔案")
        
    return {
        "status": "ok", 
        "task_ids": created_tasks, 
        "count": len(created_tasks), 
        "task_id": created_tasks[0]
    }

@app.delete("/api/tasks/{task_id}")
async def delete_ocr_task(task_id: str):
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await database.delete_task(task_id)
    try:
        if os.path.exists(task["original_filepath"]):
            os.remove(task["original_filepath"])
        render_dir = config.RENDERS_DIR / task_id
        if render_dir.exists():
            import shutil
            shutil.rmtree(render_dir)
    except Exception as e:
        print(f"Error cleaning task files {task_id}: {e}")
    return {"status": "ok"}

@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    """暫停指定 OCR 任務"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] in ["rendering", "processing", "pending"]:
        await database.update_task_status(task_id, "paused")
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
    """接續執行已暫停或失敗的 OCR 任務，支援即時更換模型"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    new_model = payload.model if (payload and payload.model) else None
    if new_model:
        await database.update_task_model(task_id, new_model)
    if task["status"] in ["paused", "failed", "pending"]:
        await database.update_task_status(task_id, "processing", model=new_model)
        asyncio.create_task(process_task_pipeline(task_id))
    return {
        "status": "ok", 
        "task_status": "processing", 
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
    
    img_path = Path(target_page["image_path"])
    asyncio.create_task(run_page_ocr(task_id, page_num, img_path, target_model, prompt))
    
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
async def sse_task_events(task_id: str):
    async def event_generator():
        while True:
            task = await database.get_task(task_id)
            if not task:
                break
                
            pages = await database.get_task_pages(task_id)
            data_payload = {
                "task_id": task_id,
                "status": task["status"],
                "total_pages": task["total_pages"],
                "processed_pages": task["processed_pages"],
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
            import json
            yield f"data: {json.dumps(data_payload)}\n\n"
            
            # 若任務已完成或失敗，且目前沒有任何頁面仍在 processing，才結束串流
            if task["status"] in ["completed", "failed"]:
                if not any(p.get("status") == "processing" for p in pages):
                    break
            await asyncio.sleep(1.5)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")

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
