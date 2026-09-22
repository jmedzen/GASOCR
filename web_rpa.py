"""
GASOCR - Web RPA Automation Module (Playwright + RPA-dedicated Profile)
透過本機 Google Chrome 自動化操作 Google AI Studio / Gemini Web 進行 OCR。

架構設計（解決 macOS Keychain 加密限制）：
  問題：Chrome 在 macOS 用 Keychain 加密 cookies，Playwright 無法解密既有 profile 的 cookies。
  解法：使用獨立的 RPA Profile (chrome_profile_rpa)，讓 Playwright 開一個可見視窗給使用者登入。
        登入完成後保存 storage_state.json（純文字 JSON），之後每次 OCR 直接載入此 state。

登入流程：
  1. launch_login_browser() → Playwright 開可見視窗 → 使用者在 aistudio 登入
  2. save_storage_state() → 從可見視窗提取並儲存 storage_state.json  
  3. check_login_status() → 用 headless + storage_state.json 確認登入有效
  4. run_web_ocr()        → 用 headless + storage_state.json 執行 OCR
"""

import os
import re
import json
import time
import signal
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

CONFIG_FILE = Path(__file__).parent / "data" / "web_rpa_config.json"
RPA_PROFILE_DIR = Path(__file__).parent / "data" / "chrome_profile_rpa"
STORAGE_STATE_FILE = Path(__file__).parent / "data" / "web_rpa_storage.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "target_service": "aistudio",  # "aistudio" 或 "gemini"
    "headless": True,
    "chrome_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "timeout_seconds": 60,
    "supported_models": [
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-2.5-pro",
        "gemini-3-pro-preview"
    ]
}

# 併發鎖：保證單一瀏覽器 Session 依序執行
_rpa_lock = asyncio.Semaphore(1)

# 登入視窗 context（全域保留供 save_storage_state 使用）
_login_context = None
_login_playwright = None


def get_web_config() -> Dict[str, Any]:
    """讀取網頁自動化設定"""
    if not CONFIG_FILE.exists():
        save_web_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
    except Exception as e:
        print(f"⚠️ 讀取 Web RPA 設定失敗: {e}，使用預設值")
        return DEFAULT_CONFIG.copy()


def save_web_config(config_data: Dict[str, Any]):
    """儲存網頁自動化設定"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)


def get_rpa_profile_path() -> Path:
    """取得 Playwright 專用 RPA Profile 目錄（與系統 Chrome 完全隔離）"""
    RPA_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return RPA_PROFILE_DIR


def is_web_model(model: Optional[str]) -> bool:
    """判斷指定模型是否為網頁自動化調用的模型"""
    if not model:
        return False
    m = model.strip()
    return (
        m.startswith("[Web]")
        or m.startswith("[Web ")
        or m.startswith("[Gemini")
        or m.startswith("[AI Studio]")
        or "[web]" in m.lower()
        or "[gemini" in m.lower()
        or "[ai studio" in m.lower()
    )


def resolve_web_service(model_name: str, default_service: str = "gemini") -> Tuple[str, str]:
    """
    依模型名稱判定目標網頁端 (gemini 官方網頁 或 aistudio) 及過濾後的模型標籤。
    回傳: (service, clean_model_name)
    """
    m = model_name.strip()
    if m.startswith("[Web Gemini]") or "[gemini web]" in m.lower() or "gemini 官方" in m.lower():
        clean = m.replace("[Web Gemini]", "").replace("[Gemini Web]", "").strip()
        return "gemini", clean
    elif m.startswith("[Web AI Studio]") or "[ai studio]" in m.lower():
        clean = m.replace("[Web AI Studio]", "").replace("[AI Studio]", "").strip()
        return "aistudio", clean
    elif m.startswith("[Web]"):
        clean = m.replace("[Web]", "").strip()
        return default_service, clean
    else:
        return default_service, m


def clean_ocr_response_text(text: str) -> str:
    """去除 LLM 對話式寒暄與前贅詞"""
    t = text.strip()
    t = re.sub(r'^(Gemini\s*說了|Gemini\s*said)[:\s]*', '', t, flags=re.IGNORECASE).strip()
    intro_patterns = [
        r'^(好的，|好的|沒問題，)?(以下為|以下是|這就為您)(圖片[中裏]?|文檔[中裏]?|本頁)?文字(依原樣|的完整)?(辨識|轉錄)?(內容|輸出|結果)?[:：\n\s]*',
        r'^依您要求，(以下為|以下是).+?[:：\n\s]*',
    ]
    for p in intro_patterns:
        t = re.sub(p, '', t, flags=re.IGNORECASE).strip()
    return t


def get_supported_web_models() -> List[Dict[str, Any]]:
    """回傳所有支援的網頁端模型（分別獨立列出 Gemini 官方網頁版 與 AI Studio 開發者模型）"""
    cfg = get_web_config()
    if not cfg.get("enabled", True):
        return []
    
    models = [
        # 1. 官方 Gemini 網頁（普通個人 Google 帳號直接可用，免 GCP 專案，最穩定）
        {
            "id": "[Web Gemini] Flash",
            "name": "🌐 [Web Gemini] Flash (官方網頁·免專案推薦)",
            "display_name": "[Web Gemini] Flash",
            "description": "透過本機 Chrome 自動化調用 Gemini 官方對話網頁 (gemini.google.com)，任何 Google 帳號直接可用，免綁定 GCP 專案",
            "is_web": True,
            "web_service": "gemini",
            "base_model": "gemini-flash"
        },
        {
            "id": "[Web Gemini] Advanced",
            "name": "🌐 [Web Gemini] Advanced (官方網頁·需訂閱)",
            "display_name": "[Web Gemini] Advanced",
            "description": "透過本機 Chrome 調用 Gemini 官方進階對話網頁 (gemini.google.com)，需 Google One AI Premium 訂閱",
            "is_web": True,
            "web_service": "gemini",
            "base_model": "gemini-pro"
        }
    ]

    # 2. Google AI Studio 開發者介面（可指定 3.8/3.7，但帳號需有 GCP 專案權限）
    aistudio_models = cfg.get("supported_models", [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-2.5-pro",
        "gemini-3-pro-preview"
    ])
    for mid in aistudio_models:
        models.append({
            "id": f"[Web AI Studio] {mid}",
            "name": f"🔬 [Web AI Studio] {mid} (需GCP專案)",
            "display_name": f"[Web AI Studio] {mid}",
            "description": f"Google AI Studio 開發者介面 (aistudio.google.com)，需帳號在 Google Cloud 具備專案權限",
            "is_web": True,
            "web_service": "aistudio",
            "base_model": mid
        })

    # 3. 舊版相容性別名：維持 [Web] 前綴（確保現有任務、下拉與測試完全相容）
    for mid in aistudio_models:
        models.append({
            "id": f"[Web] {mid}",
            "name": f"🌐 [Web] {mid} (相容別名)",
            "display_name": f"[Web] {mid}",
            "description": f"網頁相容別名，自動路由至設定之服務",
            "is_web": True,
            "web_service": cfg.get("target_service", "gemini"),
            "base_model": mid
        })

    return models


def _kill_rpa_chrome() -> int:
    """終止所有使用 RPA Profile 的殘留 Chrome/Chromium 程序"""
    killed = 0
    profile_str = str(RPA_PROFILE_DIR.resolve())
    try:
        result = subprocess.run(["pgrep", "-f", profile_str], capture_output=True, text=True)
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    pid = int(pid_str.strip())
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                    print(f"🔫 SIGTERM → PID={pid}")
                except Exception:
                    pass
            if killed:
                time.sleep(1.5)
    except Exception as e:
        print(f"⚠️ 終止 RPA Chrome 時錯誤: {e}")
    return killed


def _remove_singleton_lock() -> None:
    """移除 RPA Profile 中的 SingletonLock"""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        f = RPA_PROFILE_DIR / name
        if f.exists() or f.is_symlink():
            try:
                f.unlink()
                print(f"🗑️ 移除 {name}")
            except Exception:
                pass


def is_logged_in_cached() -> bool:
    """快速檢查是否有已儲存的 storage_state（表示已完成過登入流程）"""
    if not STORAGE_STATE_FILE.exists():
        return False
    try:
        with open(STORAGE_STATE_FILE) as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        # 確認有 Google 的 session cookie
        key_cookies = [c for c in cookies if any(k in c.get("name", "") for k in
                       ["SID", "PSID", "APISID", "SAPISID", "__Secure", "HSID", "SSID"])]
        return len(key_cookies) > 0
    except Exception:
        return False


def get_login_status_info() -> Dict[str, Any]:
    """同步取得登入狀態摘要（不啟動瀏覽器，純檔案檢查）"""
    cfg = get_web_config()
    service = cfg.get("target_service", "aistudio")
    if not STORAGE_STATE_FILE.exists():
        return {
            "cached": False,
            "service": service,
            "cookie_count": 0,
            "message": "⚠️ 尚未登入。請點選「① 開啟登入視窗」完成一次性 Google 帳號登入。"
        }
    try:
        with open(STORAGE_STATE_FILE) as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        key = [c for c in cookies if any(k in c.get("name", "") for k in
               ["SID", "PSID", "APISID", "SAPISID", "__Secure", "HSID", "SSID"])]
        if key:
            return {
                "cached": True,
                "service": service,
                "cookie_count": len(cookies),
                "key_cookie_count": len(key),
                "message": f"✅ 登入 Cookie 有效（{len(key)} 個 session cookies）"
            }
        else:
            return {
                "cached": True,
                "service": service,
                "cookie_count": len(cookies),
                "key_cookie_count": 0,
                "message": "⚠️ Storage state 存在但缺少 session cookies，請重新登入"
            }
    except Exception as e:
        return {
            "cached": False,
            "service": service,
            "cookie_count": 0,
            "message": f"⚠️ Storage state 讀取失敗: {e}"
        }


async def launch_login_browser(target_service: Optional[str] = None) -> Dict[str, Any]:
    """
    以 Playwright 開啟可見視窗讓使用者登入 Google AI Studio / Gemini。
    使用獨立的 RPA Profile，不受 macOS Keychain 加密限制。
    登入完成後，使用者點選「儲存登入狀態」按鈕，系統自動保存 storage_state.json。
    """
    global _login_context, _login_playwright

    cfg = get_web_config()
    service = target_service or cfg.get("target_service", "aistudio")
    url = "https://aistudio.google.com/" if service == "aistudio" else "https://gemini.google.com/app"
    chrome_path = cfg.get("chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    if not Path(chrome_path).exists():
        return {"success": False, "message": f"找不到 Chrome: {chrome_path}"}

    # 清理舊的登入視窗
    if _login_context:
        try:
            await _login_context.close()
        except Exception:
            pass
        _login_context = None
    if _login_playwright:
        try:
            await _login_playwright.__aexit__(None, None, None)
        except Exception:
            pass
        _login_playwright = None

    # 清理殘留程序與 lock
    _kill_rpa_chrome()
    _remove_singleton_lock()

    try:
        from playwright.async_api import async_playwright
        _login_playwright = async_playwright()
        p = await _login_playwright.__aenter__()

        _login_context = await p.chromium.launch_persistent_context(
            user_data_dir=str(get_rpa_profile_path()),
            headless=False,
            executable_path=chrome_path,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 800},
        )
        page = await _login_context.new_page()
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        print(f"🌐 登入視窗已開啟: {url}")
        return {"success": True, "message": f"已開啟登入視窗，請完成 Google 帳號登入後點選「② 儲存登入狀態」"}

    except Exception as e:
        return {"success": False, "message": f"開啟登入視窗失敗: {str(e)}"}


async def save_storage_state() -> Dict[str, Any]:
    """
    從當前開啟的登入視窗提取 cookies 並保存至 storage_state.json。
    關閉登入視窗。
    """
    global _login_context, _login_playwright

    if not _login_context:
        # 嘗試從既有 RPA Profile 提取（Profile 可能已有登入狀態）
        return await _extract_from_rpa_profile()

    try:
        # 取得當前所有 cookies
        cookies = await _login_context.cookies()
        key_cookies = [c for c in cookies if any(k in c.get("name", "") for k in
                       ["SID", "PSID", "APISID", "SAPISID", "__Secure", "HSID", "SSID"])]

        if not key_cookies:
            pages = _login_context.pages
            current_url = pages[-1].url if pages else "unknown"
            if "accounts.google.com" in current_url or "signin" in current_url:
                return {"success": False, "message": "尚未完成 Google 登入，請在瀏覽器中完成登入後再點此按鈕"}
            return {"success": False, "message": f"未找到有效的登入 Cookie（當前頁面: {current_url}），請完整登入 Google 帳號後再試"}

        # 儲存 storage state
        STORAGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        await _login_context.storage_state(path=str(STORAGE_STATE_FILE))
        print(f"✅ Storage state 已儲存 ({len(cookies)} cookies, {len(key_cookies)} key)")

        # 關閉登入視窗
        await _login_context.close()
        _login_context = None
        if _login_playwright:
            await _login_playwright.__aexit__(None, None, None)
            _login_playwright = None

        return {
            "success": True,
            "cookie_count": len(cookies),
            "key_cookie_count": len(key_cookies),
            "message": f"✅ 登入狀態已儲存！({len(key_cookies)} 個 session cookies)"
        }

    except Exception as e:
        return {"success": False, "message": f"儲存登入狀態失敗: {str(e)}"}


async def _extract_from_rpa_profile() -> Dict[str, Any]:
    """從 RPA Profile 目錄直接啟動 Playwright 提取 storage state"""
    cfg = get_web_config()
    chrome_path = cfg.get("chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    _kill_rpa_chrome()
    _remove_singleton_lock()

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(get_rpa_profile_path()),
                headless=True,
                executable_path=chrome_path,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = await context.new_page()
            await page.goto("https://aistudio.google.com/", timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            cookies = await context.cookies()
            key_cookies = [c for c in cookies if any(k in c.get("name", "") for k in
                           ["SID", "PSID", "APISID", "SAPISID", "__Secure", "HSID", "SSID"])]

            if not key_cookies:
                await context.close()
                return {"success": False, "message": "RPA Profile 中未找到有效登入 Cookie，請重新開啟登入視窗完成登入"}

            await context.storage_state(path=str(STORAGE_STATE_FILE))
            await context.close()
            return {
                "success": True,
                "cookie_count": len(cookies),
                "key_cookie_count": len(key_cookies),
                "message": f"✅ 從既有 Profile 提取 ({len(key_cookies)} 個 session cookies)"
            }
    except Exception as e:
        return {"success": False, "message": f"從 Profile 提取失敗: {str(e)}"}


async def check_login_status() -> Dict[str, Any]:
    """
    驗證 storage_state.json 中的登入狀態是否仍然有效（用 headless 訪問 AI Studio）。
    如果沒有 storage_state，直接回報未登入狀態（不啟動瀏覽器）。
    """
    # 先快速檢查
    cached_info = get_login_status_info()
    if not cached_info["cached"] or cached_info.get("key_cookie_count", 0) == 0:
        return {
            "logged_in": False,
            "service": get_web_config().get("target_service", "aistudio"),
            "message": cached_info["message"]
        }

    cfg = get_web_config()
    service = cfg.get("target_service", "aistudio")
    url = "https://aistudio.google.com/" if service == "aistudio" else "https://gemini.google.com/app"
    target_url = "https://aistudio.google.com/prompts/new_chat" if service == "aistudio" else "https://gemini.google.com/app"
    chrome_path = cfg.get("chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    _kill_rpa_chrome()
    _remove_singleton_lock()

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                executable_path=chrome_path,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            context = await browser.new_context(
                storage_state=str(STORAGE_STATE_FILE)
            )
            page = await context.new_page()
            try:
                await page.goto(target_url, timeout=25000, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                current_url = page.url.lower()
                is_signin = "accounts.google.com" in current_url or "signin" in current_url
                title = await page.title()
                await browser.close()
                return {
                    "logged_in": not is_signin,
                    "service": service,
                    "current_url": current_url,
                    "title": title,
                    "message": "✅ 登入有效，AI Studio 正常存取" if not is_signin else "⚠️ Session 已過期，請重新完成登入流程"
                }
            except Exception as e:
                await browser.close()
                return {"logged_in": False, "service": service, "error": str(e), "message": f"驗證失敗: {e}"}
    except Exception as e:
        return {"logged_in": False, "service": service, "error": str(e), "message": f"瀏覽器啟動失敗: {e}"}


async def run_web_ocr(
    image_path: Path,
    model_name: str,
    prompt: str,
    timeout: Optional[int] = None
) -> Tuple[bool, str, int]:
    """
    執行單頁網頁自動化 OCR（headless，從 storage_state.json 載入登入狀態）。
    回傳: (成功, 文字/錯誤訊息, HTTP狀態碼)
    """
    cfg = get_web_config()
    if not cfg.get("enabled", True):
        return False, "網頁自動化 (Web RPA) 功能目前已停用", 400

    if not is_logged_in_cached():
        return False, "尚未登入 Google 帳號。請至「金鑰池 > 網頁自動化」完成登入流程（步驟① 開啟登入視窗 → 登入 → 步驟② 儲存登入狀態）", 401

    default_service = cfg.get("target_service", "gemini")
    service, clean_model = resolve_web_service(model_name, default_service)
    chrome_path = cfg.get("chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    timeout_sec = timeout or cfg.get("timeout_seconds", 60)
    headless = cfg.get("headless", True)

    if not Path(chrome_path).exists():
        return False, f"未找到本機 Chrome 瀏覽器: {chrome_path}", 500
    if not image_path.exists():
        return False, f"頁面切圖檔案不存在: {image_path}", 404

    async with _rpa_lock:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=headless,
                    executable_path=chrome_path,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                context = await browser.new_context(
                    storage_state=str(STORAGE_STATE_FILE),
                    viewport={"width": 1280, "height": 800}
                )
                page = await context.new_page()
                try:
                    if service == "aistudio":
                        success, text_or_err = await _execute_aistudio_ocr(
                            page, image_path, clean_model, prompt, timeout_sec
                        )
                    else:
                        success, text_or_err = await _execute_gemini_web_ocr(
                            page, image_path, clean_model, prompt, timeout_sec
                        )
                    if success:
                        text_or_err = clean_ocr_response_text(text_or_err)
                        try:
                            await context.storage_state(path=str(STORAGE_STATE_FILE))
                        except Exception:
                            pass
                    await browser.close()
                    return success, text_or_err, 200 if success else 500
                except Exception as e:
                    await browser.close()
                    return False, f"網頁操作流程異常: {str(e)}", 500
        except Exception as e:
            return False, f"啟動瀏覽器失敗: {str(e)}", 500


# ── Legacy compat ──────────────────────────────────────────
def get_profile_path() -> Path:
    return get_rpa_profile_path()

def sync_login_cookies() -> Dict[str, Any]:
    """保留 API 相容性，回傳當前快取登入狀態"""
    return get_login_status_info()
# ──────────────────────────────────────────────────────────


async def _execute_aistudio_ocr(
    page, image_path: Path, model_name: str, prompt: str, timeout_sec: int
) -> Tuple[bool, str]:
    """在 Google AI Studio 執行 OCR 流程"""
    target_url = "https://aistudio.google.com/prompts/new_chat"
    await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
    await asyncio.sleep(2.5)

    if "accounts.google.com" in page.url or "signin" in page.url:
        return False, "Session 已過期。請至「金鑰池 > 網頁自動化」重新執行登入流程（步驟① → ②）。"

    try:
        await page.wait_for_selector("textarea, [contenteditable='true'], input[type='file']", timeout=15000)
    except Exception:
        pass

    # 上傳圖片（透過 add_circle 選單與 File Chooser，確保 Angular 組件成功附加多媒體切片）
    add_btn = page.locator("button:has-text('add_circle')").first
    if await add_btn.count() > 0:
        await add_btn.click()
        await asyncio.sleep(0.6)
        try:
            async with page.expect_file_chooser(timeout=6000) as fc_info:
                upload_item = page.locator("[role='menuitem']:has-text('Upload files'), button:has-text('Upload files')").first
                await upload_item.click()
            fc = await fc_info.value
            await fc.set_files(str(image_path.resolve()))
            await asyncio.sleep(2.0)
        except Exception:
            fi = page.locator("input[type='file'].file-input, input[type='file']").first
            if await fi.count() > 0:
                await fi.set_input_files(str(image_path.resolve()))
                await asyncio.sleep(2.0)
    else:
        fi = page.locator("input[type='file'].file-input, input[type='file']").first
        if await fi.count() > 0:
            await fi.set_input_files(str(image_path.resolve()))
            await asyncio.sleep(2.0)

    # 關閉任何可能彈出的確認對話框（如圖片版權條款 Acknowledge、ToS 等，避免 cdk-overlay 攔截點擊）
    for _ in range(3):
        ack_btn = page.locator("button:has-text('Acknowledge'), button:has-text('I agree'), button:has-text('Agree'), button:has-text('Get started'), button:has-text('Got it')").first
        if await ack_btn.count() > 0 and await ack_btn.is_visible():
            try:
                await ack_btn.click(timeout=3000)
                await asyncio.sleep(0.8)
            except Exception:
                pass
        else:
            break

    # 填入 prompt（直接使用 fill，不觸發 click，完全避開 pointer interception）
    prompt_box = page.locator("textarea[aria-label='Enter a prompt'], textarea.textarea, textarea").last
    if await prompt_box.count() > 0:
        await prompt_box.fill(prompt)
    else:
        await page.keyboard.type(prompt)
    await asyncio.sleep(1.0)

    # 送出 (點擊 Run 按鈕或按 Control+Enter)
    run_btn = page.locator("button:has-text('Run')").first
    if await run_btn.count() > 0 and await run_btn.is_enabled():
        try:
            await run_btn.click(timeout=5000)
        except Exception:
            await page.keyboard.press("Control+Enter")
    else:
        await page.keyboard.press("Control+Enter")

    # 等待回傳 (嚴格限定於 Model 回應區塊，絕不抓取使用者 Prompt 區塊)
    start = time.time()
    last_text = ""
    stable = 0
    clean_prompt = prompt.strip()

    while time.time() - start < timeout_sec:
        await asyncio.sleep(2.0)

        # 透過 evaluate 在瀏覽器內部精準提取最後一個 Model Turn 的文字
        res = await page.evaluate('''(promptStr) => {
            const modelContainers = Array.from(document.querySelectorAll('[data-turn-role="Model"], .chat-turn-container.model, ms-chat-turn:has(.model-prompt-container)'));
            if (!modelContainers.length) {
                return { status: 'waiting' };
            }
            const lastModel = modelContainers[modelContainers.length - 1];

            // 檢查是否有錯誤標籤
            const errorElem = lastModel.querySelector('.model-error, .error-container, [role="alert"]');
            if (errorElem) {
                return { status: 'error', text: errorElem.innerText.trim() };
            }

            // 拷貝節點並排除標頭、時間戳、操作按鈕等干擾文字
            const clone = lastModel.cloneNode(true);
            clone.querySelectorAll('.author-label, .actions-container, button, .turn-footer, .timestamp').forEach(n => n.remove());
            const rawText = clone.innerText.trim();

            if (!rawText) {
                return { status: 'generating' };
            }

            // 雙重防護：確認不是使用者輸入的 prompt
            if (rawText === promptStr || (promptStr.length > 20 && promptStr.startsWith(rawText.slice(0, 50)))) {
                return { status: 'prompt_echo' };
            }

            return { status: 'success', text: rawText };
        }''', clean_prompt)

        status = res.get("status")
        if status == "error":
            err_msg = res.get("text", "An internal error has occurred.")
            if "internal error" in err_msg.lower() or "permission" in err_msg.lower():
                return False, (
                    f"Google AI Studio 伺服器回傳錯誤: {err_msg}。"
                    "【原因】：此 Google 帳號尚未在 AI Studio 建立或綁定 Google Cloud (GCP) 專案。"
                    "【建議】：請改選「[Web Gemini] Flash (官方網頁·免專案)」即可直接辨識，免 GCP 專案；"
                    "或手動至 aistudio.google.com 點選「Get API key」完成專案啟用。"
                )
            return False, f"Google AI Studio 伺服器回傳錯誤: {err_msg}（建議改用 [Web Gemini] 官方網頁服務）"

        if status == "success":
            cur = res.get("text", "").strip()
            if cur:
                if cur == last_text:
                    stable += 1
                    if stable >= 2:
                        return True, cur
                else:
                    last_text = cur
                    stable = 0

    if last_text and last_text != clean_prompt:
        return True, last_text.strip()
    return False, f"AI Studio OCR 超時 ({timeout_sec}s)，未取得完整模型回傳"


async def _execute_gemini_web_ocr(
    page, image_path: Path, model_name: str, prompt: str, timeout_sec: int
) -> Tuple[bool, str]:
    """在 Gemini 官方網頁執行 OCR 流程"""
    await page.goto("https://gemini.google.com/app", timeout=30000, wait_until="domcontentloaded")
    await asyncio.sleep(2.5)

    if "accounts.google.com" in page.url or "signin" in page.url:
        return False, "Session 已過期。請重新執行登入流程（步驟① → ②）。"

    # 1. 點擊「上傳與工具」按鈕掛載隱藏的 input[type=file]
    upload_btn = page.locator("button[aria-label*='上傳與工具'], button[aria-label*='Upload'], button[aria-label*='新增']").first
    if await upload_btn.count() > 0:
        await upload_btn.click()
        await asyncio.sleep(0.8)

    # 2. 在掛載的 hidden-file-input 上注入切圖檔案
    fi = page.locator("input.hidden-file-input, input[type='file']").first
    if await fi.count() > 0:
        await fi.set_input_files(str(image_path.resolve()))
        await asyncio.sleep(2.5)

    # 3. 輸入 prompt
    ib = page.locator(".ql-editor, [contenteditable='true'], textarea").first
    if await ib.count() > 0:
        await ib.click()
        await ib.fill(prompt)
    else:
        await page.keyboard.type(prompt)
    await asyncio.sleep(1.0)

    # 4. 點擊傳送 (優先尋找包含「傳送」字樣的發送鈕)
    send_btn = page.locator("button[aria-label*='傳送訊息'], button[aria-label*='傳送'], button[aria-label*='Send']").first
    if await send_btn.count() > 0 and await send_btn.is_enabled():
        await send_btn.click()
    else:
        await page.keyboard.press("Enter")

    start = time.time()
    last_text = ""
    stable = 0
    clean_prompt = prompt.strip()

    while time.time() - start < timeout_sec:
        await asyncio.sleep(2.0)
        res = await page.evaluate('''(promptStr) => {
            const responses = Array.from(document.querySelectorAll('model-response'));
            if (!responses.length) return { status: 'waiting' };
            const lastResp = responses[responses.length - 1];

            const clone = lastResp.cloneNode(true);
            clone.querySelectorAll('button, .action-buttons, .tts-button').forEach(n => n.remove());
            let text = clone.innerText.trim();

            // 去除「Gemini 說了」或「Gemini said」等前綴
            text = text.replace(/^(Gemini\\s*說了|Gemini\\s*said)[:\\s]*/i, '').trim();

            if (!text) return { status: 'generating' };
            if (text === promptStr || (promptStr.length > 20 && promptStr.startsWith(text.slice(0, 50)))) {
                return { status: 'prompt_echo' };
            }

            // 檢查是否回傳了「尚未上傳圖片」對話式錯誤
            if (text.includes("尚未夾帶") || text.includes("未夾帶") || text.includes("未上傳圖片") || text.includes("尚未上傳圖片")) {
                return { status: 'upload_error', text: text };
            }

            return { status: 'success', text: text };
        }''', clean_prompt)

        status = res.get("status")
        if status == "upload_error":
            return False, f"Gemini 官方網頁回報未成功接收圖片: {res.get('text')}。建議重試本頁。"

        if status == "success":
            cur = res.get("text", "").strip()
            if cur:
                if cur == last_text:
                    stable += 1
                    if stable >= 2:
                        return True, cur
                else:
                    last_text = cur
                    stable = 0

    if last_text and last_text != clean_prompt:
        return True, last_text.strip()
    return False, f"Gemini 網頁生成超時 ({timeout_sec}s)，未取得完整回傳文字"
