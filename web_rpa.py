"""
GASOCR - Web RPA Automation Module

## 兩種瀏覽器模式 (browser_mode)

### 1. "cdp" (預設，建議) — 接入「使用者自己開啟的 Chrome」
由使用者手動啟動一個帶 --remote-debugging-port 的 Chrome（可用 start_chrome_cdp.sh），
GASOCR 再以 Playwright connect_over_cdp() 接入這個「真實」的瀏覽器 session。

為什麼要這樣做（2026-09 審計結論）：
  - 反機器人通行證綁在 TLS／瀏覽器指紋＋IP 信譽上，**不是只綁 cookie**。
    把 storage_state.json 的 cookie 重放進一個全新 context，會被判定為「被盜的 session」
    而重新挑戰 → AI Studio 回 "permission denied"。
  - 使用者自己開的 Chrome 沒有 Playwright 的 automation 指紋（navigator.webdriver 為 false），
    是真實指紋＋真實 IP，因此不會被重新挑戰。
  - 因此本模組**絕不**在這個路徑呼叫 browser.close()／_kill_rpa_chrome()，
    否則會關掉使用者自己的瀏覽器。

### 2. "launch" (舊版，保留相容) — 由 Playwright 自己啟動無頭瀏覽器
即原本的 storage_state.json 重放做法。已知會被 AI Studio 拒絕，僅保留作為除錯對照。

## 登入流程（CDP 模式）
  1. 執行 ./start_chrome_cdp.sh  → 開啟一個專屬 Chrome 視窗
  2. 在該視窗中「手動」登入 Google 帳號（只需一次，profile 會記住）
  3. 保持該視窗開著 → 點「檢查 CDP 連線」確認接入成功
  4. 之後 OCR 全程沿用這個真實 session，不再需要儲存／重放 cookie
"""

import os
import re
import sys
import json
import time
import signal
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "data" / "web_rpa_config.json"
RPA_PROFILE_DIR = BASE_DIR / "data" / "chrome_profile_rpa"
STORAGE_STATE_FILE = BASE_DIR / "data" / "web_rpa_storage.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    # 預設走 Gemini 官方網頁：實測（2026-09）AI Studio Playground 對此帳號
    # 一律回 "An internal error has occurred." / "permission denied"（非程式問題，
    # 且與模型、grounding、自動化偵測都無關），而 Gemini 官方網頁可正常 OCR。
    "target_service": "gemini",  # "aistudio" 或 "gemini"
    # ── 瀏覽器模式 ────────────────────────────────────────────
    # "cdp"    = 接入使用者自己開的 Chrome（建議，唯一能避開 permission denied 的方式）
    # "launch" = 舊版由 Playwright 啟動無頭瀏覽器 + cookie 重放（已知會被拒）
    "browser_mode": "cdp",
    "cdp_port": 9222,
    "cdp_user_data_dir": "data/chrome_profile_rpa",  # 相對於專案根目錄
    # ── 舊版 launch 模式參數 ─────────────────────────────────
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


# ══════════════════════════════════════════════════════════════
#  CDP 模式：接入「使用者自己開啟的 Chrome」
#
#  ⚠️ 不變式（invariant）：GASOCR 對這個瀏覽器只有「借用」權，沒有「擁有」權。
#     因此在 CDP 路徑中：
#       - 絕不呼叫 browser.close()      （會關掉使用者整個 Chrome）
#       - 絕不呼叫 _kill_rpa_chrome()   （會 SIGTERM 使用者自己的 Chrome）
#       - 絕不呼叫 page.close() 於使用者的分頁
#     要脫離連線時，只呼叫 playwright.stop()。
# ══════════════════════════════════════════════════════════════

_cdp_pw = None        # Playwright 實例（保持存活以維持連線）
_cdp_browser = None   # connect_over_cdp 取得的 Browser
_cdp_context = None   # 使用者瀏覽器的既有 context
_cdp_page = None      # GASOCR 專用分頁（建立後重用，不搶使用者正在看的分頁）
_cdp_connect_lock = asyncio.Lock()


def get_cdp_port() -> int:
    cfg = get_web_config()
    try:
        return int(cfg.get("cdp_port", 9222))
    except (TypeError, ValueError):
        return 9222


def get_cdp_endpoint() -> str:
    return f"http://127.0.0.1:{get_cdp_port()}"


def get_cdp_profile_dir() -> Path:
    """取得 CDP 模式使用的 Chrome User Data Dir"""
    cfg = get_web_config()
    raw = cfg.get("cdp_user_data_dir") or "data/chrome_profile_rpa"
    p = Path(raw)
    if not p.is_absolute():
        p = BASE_DIR / raw
    return p


def is_cdp_mode() -> bool:
    return (get_web_config().get("browser_mode") or "cdp").lower() == "cdp"


def get_chrome_launch_command() -> str:
    """回傳使用者應該在終端機執行的 Chrome 啟動指令（供 UI 顯示／複製）"""
    cfg = get_web_config()
    chrome = cfg.get("chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return (
        f'"{chrome}" \\\n'
        f'  --remote-debugging-port={get_cdp_port()} \\\n'
        f'  --user-data-dir="{get_cdp_profile_dir()}" \\\n'
        f'  --no-first-run --no-default-browser-check'
    )


async def check_cdp_available(timeout: float = 2.0) -> Dict[str, Any]:
    """
    探測使用者是否已經開好帶 remote-debugging-port 的 Chrome。
    （只讀 /json/version，不會啟動任何瀏覽器）
    """
    import urllib.request
    import urllib.error

    endpoint = get_cdp_endpoint()

    def _probe() -> Dict[str, Any]:
        try:
            req = urllib.request.Request(f"{endpoint}/json/version", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            return {"available": True, "info": data}
        except urllib.error.URLError as e:
            return {"available": False, "error": str(e.reason if hasattr(e, "reason") else e)}
        except Exception as e:
            return {"available": False, "error": str(e)}

    res = await asyncio.to_thread(_probe)
    res["endpoint"] = endpoint
    res["profile_dir"] = str(get_cdp_profile_dir())
    res["launch_command"] = get_chrome_launch_command()
    if res.get("available"):
        info = res.get("info", {})
        res["browser"] = info.get("Browser", "unknown")
        res["message"] = f"✅ 已偵測到可控制的 Chrome：{info.get('Browser', '')}"
    else:
        res["message"] = (
            f"⚠️ 在 {endpoint} 找不到可控制的 Chrome。\n"
            "請先執行 ./start_chrome_cdp.sh（或下方指令）開啟專屬 Chrome 並手動登入，"
            "並保持該視窗開著。"
        )
    return res


async def _get_cdp_page():
    """
    取得（或建立）連到使用者 Chrome 的 GASOCR 專用分頁。
    全程重用同一個分頁；若連線或分頁失效則自動重連。
    （每頁是否為新對話由 executor 導航至 /prompts/new_chat 決定。）
    """
    global _cdp_pw, _cdp_browser, _cdp_context, _cdp_page

    async with _cdp_connect_lock:
        # 判斷既有連線是否仍可用
        if _cdp_browser is not None:
            try:
                if not _cdp_browser.is_connected():
                    raise RuntimeError("CDP 連線已中斷")
                if _cdp_page is not None and not _cdp_page.is_closed():
                    return _cdp_page
                # 分頁被使用者關掉了 → 重開一個
                _cdp_page = await _cdp_context.new_page()
                return _cdp_page
            except Exception as e:
                print(f"♻️ CDP 連線失效，重新接入: {e}")
                await _reset_cdp_state()

        # 建立新連線
        from playwright.async_api import async_playwright

        _cdp_pw = await async_playwright().start()
        try:
            _cdp_browser = await _cdp_pw.chromium.connect_over_cdp(get_cdp_endpoint())
        except Exception as e:
            await _reset_cdp_state()
            raise RuntimeError(
                f"無法接入 {get_cdp_endpoint()}：{e}\n"
                "請確認已用 start_chrome_cdp.sh 開啟 Chrome，且該視窗仍開著。"
            ) from e

        contexts = _cdp_browser.contexts
        if not contexts:
            await _reset_cdp_state()
            raise RuntimeError("已連上 Chrome，但找不到任何 browser context")
        # 使用使用者既有的 context（保有真實登入狀態與指紋）
        _cdp_context = contexts[0]

        # 建立 GASOCR 專屬分頁，避免干擾使用者正在檢視的分頁
        _cdp_page = await _cdp_context.new_page()
        print(f"🔗 已接入使用者 Chrome ({get_cdp_endpoint()})，建立 GASOCR 專用分頁")
        return _cdp_page


async def _reset_cdp_state():
    """只脫離連線，絕不關閉使用者的瀏覽器"""
    global _cdp_pw, _cdp_browser, _cdp_context, _cdp_page
    _cdp_browser = None
    _cdp_context = None
    _cdp_page = None
    if _cdp_pw is not None:
        try:
            # playwright.stop() 只關閉 Playwright 自己的連線／driver，
            # 不會終止使用者手動啟動的 Chrome 程序。
            await _cdp_pw.stop()
        except Exception:
            pass
        _cdp_pw = None


async def close_cdp_session() -> Dict[str, Any]:
    """公開 API：主動脫離 CDP 連線（不會關閉使用者的 Chrome）"""
    await _reset_cdp_state()
    return {"success": True, "message": "已脫離 CDP 連線（您的 Chrome 保持開啟）"}


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


# 拒答／只摘要的偵測樣式（保守設計：避免誤殺正常古籍文本）
# 注意：實測模型有時會用「簡體中文」拒答（例：我无法提供这方面的帮助），
# 因此繁簡兩種寫法都必須涵蓋，否則拒答會被當成成功寫入資料庫。
_REFUSAL_HEAD_PATTERNS = [
    # 繁體
    r"^我無法", r"^我沒有辦法", r"^恕我無法", r"^我不能", r"^很抱歉", r"^抱歉", r"^對不起",
    # 簡體
    r"^我无法", r"^我没有办法", r"^恕我无法", r"^我不能", r"^很抱歉", r"^抱歉", r"^对不起",
    # 「我只是個語言模型」類型
    r"我(只是|只)是(一)?[個个]語言模型", r"我(只是|只)是(一)?[個个]语言模型",
    r"作為(一)?[個个](AI|人工智能|語言模型)", r"作为(一)?[个個](AI|人工智能|语言模型)",
    # 英文
    r"^I (cannot|can't|am unable|'m unable)", r"^I'm sorry", r"^Sorry", r"^I apologize",
    r"as an AI", r"as a language model",
]
# 短回應中的明確拒答詞（不可只用「不能」，否則會誤殺「菩薩不能知」這類正文）
_REFUSAL_SHORT_PATTERN = (
    r"(無法提供|无法提供|無法協|无法协|無法爲|無法為|不能提供|無法進行|无法进行"
    r"|抱歉|對不起|对不起|我无法|我無法"
    r"|unable to (help|assist|provide)|cannot (help|assist|provide)|as an AI|語言模型|语言模型)"
)
_REFUSAL_COPYRIGHT_PATTERN = (
    r"(著作權|版權|copyright).{0,30}(限制|考量|問題|因素|無法|不能|故|因此|僅能|只能)"
)

# 「沒收到圖片」的樣式。這是實測踩到的坑：模型回「目前沒有看到您上傳或提供的圖片，請附上圖片…」
# 時，舊版程式因為只比對 4 個字串而把它當成 success，造成靜默資料遺失。
_NO_IMAGE_PATTERNS = [
    r"沒(有)?看到.{0,6}圖片",
    r"未(能)?(看到|收到|取得|偵測到).{0,6}圖片",
    r"沒有.{0,4}圖片",
    r"請(您)?(附上|上傳|提供).{0,4}圖片",
    r"未夾帶", r"尚未夾帶", r"未上傳圖片", r"尚未上傳圖片",
    r"圖片.{0,4}(遺失|缺失|不存在)",
    r"no image", r"didn'?t (see|receive|get) an image", r"don'?t see an image",
    r"please (attach|upload|provide) an image",
]


def detect_no_image(text: str) -> Optional[str]:
    """
    偵測「模型表示沒有收到圖片」的回應。

    這種回應必須判定為**失敗**，否則會以空白/無意義內容寫入資料庫。
    回傳命中的片段，或 None。
    """
    t = (text or "").strip()
    if not t:
        return None
    # 只在相對短的回應中判定，避免誤殺正常 OCR 長文內偶然出現的詞句
    if len(t) > 300:
        probe = t[:300]
    else:
        probe = t
    for pat in _NO_IMAGE_PATTERNS:
        if re.search(pat, probe, re.IGNORECASE):
            return t[:200]
    return None


def _validate_ocr_output(text: str) -> Optional[str]:
    """
    統一驗證網頁端回傳的內容是否可信。
    回傳錯誤訊息字串（表示不可信、應判定失敗），或 None 表示通過。

    這是修掉「靜默資料遺失」的關鍵：先前模型拒答或回報沒收到圖片時，
    都會被當成 success 寫進資料庫。
    """
    no_image = detect_no_image(text)
    if no_image:
        return (
            "模型回報沒有收到圖片，代表圖片上傳未成功。"
            f"此頁已判定為失敗，以免寫入無效內容。回應：{no_image}"
        )
    refusal = detect_refusal(text)
    if refusal:
        return (
            "模型拒絕逐字轉錄（疑似著作權／安全策略拒答），"
            f"為避免靜默資料遺失已判定為失敗。回應開頭：{refusal}"
        )
    return None


def detect_refusal(text: str) -> Optional[str]:
    """
    偵測模型「拒答」或「只給摘要／描述」而非逐字轉錄的情況。

    為什麼需要：`_execute_gemini_web_ocr` 原本只檢查 4 個「未上傳圖片」字串，
    因此模型拒答時會被當成 success 寫入資料庫，造成**靜默資料遺失**。
    這裡回傳拒答片段（供錯誤訊息使用），或 None 表示不是拒答。
    """
    t = (text or "").strip()
    if not t:
        return None
    head = t[:120]
    for pat in _REFUSAL_HEAD_PATTERNS:
        if re.search(pat, head, re.IGNORECASE):
            return t[:200]
    if re.search(_REFUSAL_COPYRIGHT_PATTERN, t[:300], re.IGNORECASE):
        return t[:200]
    # 短回應（<120 字）若含明確拒答詞，幾乎必定是拒答。
    # 實測案例：「我无法提供这方面的帮助，因为我只是一个语言模型。」（24 字）
    if len(t) < 120 and re.search(_REFUSAL_SHORT_PATTERN, t, re.IGNORECASE):
        return t[:200]
    return None


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

    return models


def _is_cdp_port_alive(timeout: float = 0.5) -> bool:
    """同步檢查 CDP 埠是否有 Chrome 在監聽（用於保護使用者的瀏覽器不被誤殺）"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", get_cdp_port()), timeout=timeout):
            return True
    except Exception:
        return False


def _kill_rpa_chrome() -> int:
    """
    終止所有使用 RPA Profile 的殘留 Chrome/Chromium 程序。

    ⚠️ 安全防護：若偵測到使用者自己的 Chrome 正以 CDP 模式開著（本專案的建議用法），
    則**完全不終止任何程序**，因為那個瀏覽器是使用者的，不是我們擁有的一次性實例。
    """
    if _is_cdp_port_alive():
        print("🛡️ 偵測到 CDP Chrome 正在執行（使用者自己的瀏覽器），為安全起見略過程序終止")
        return 0

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
    """
    同步取得登入狀態摘要（不啟動瀏覽器，純檔案檢查）。

    注意：CDP 模式（browser_mode="cdp"）下，登入狀態存在於**使用者的 Chrome profile**中，
    與 storage_state.json 無關。因此這裡只回報 storage_state 的狀態供舊版模式參考，
    並附上 browser_mode 讓 UI 能顯示正確的提示。
    """
    cfg = get_web_config()
    service = cfg.get("target_service", "aistudio")
    mode = (cfg.get("browser_mode") or "cdp").lower()

    base: Dict[str, Any] = {
        "browser_mode": mode,
        "cdp_endpoint": get_cdp_endpoint(),
        "cdp_profile_dir": str(get_cdp_profile_dir()),
        "launch_command": get_chrome_launch_command(),
        "service": service,
    }

    if mode == "cdp":
        # CDP 模式不依賴 storage_state；真正的驗證要呼叫 check_cdp_available()/verify。
        base.update({
            "cached": False,
            "cookie_count": 0,
            "key_cookie_count": 0,
            "message": (
                "ℹ️ 目前為「接入現有 Chrome」模式：登入狀態保存在您的 Chrome profile 中，"
                "不使用 cookie 重放。請確認已用 ./start_chrome_cdp.sh 開啟 Chrome 並手動登入，"
                "再點「檢查 CDP 連線」。"
            ),
        })
        return base

    if not STORAGE_STATE_FILE.exists():
        base.update({
            "cached": False,
            "cookie_count": 0,
            "message": "⚠️ 尚未登入。請點選「① 開啟登入視窗」完成一次性 Google 帳號登入。"
        })
        return base
    try:
        with open(STORAGE_STATE_FILE) as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        key = [c for c in cookies if any(k in c.get("name", "") for k in
               ["SID", "PSID", "APISID", "SAPISID", "__Secure", "HSID", "SSID"])]
        if key:
            base.update({
                "cached": True,
                "cookie_count": len(cookies),
                "key_cookie_count": len(key),
                "message": f"✅ 登入 Cookie 有效（{len(key)} 個 session cookies）"
            })
        else:
            base.update({
                "cached": True,
                "cookie_count": len(cookies),
                "key_cookie_count": 0,
                "message": "⚠️ Storage state 存在但缺少 session cookies，請重新登入"
            })
        return base
    except Exception as e:
        base.update({
            "cached": False,
            "cookie_count": 0,
            "message": f"⚠️ Storage state 讀取失敗: {e}"
        })
        return base


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


async def check_cdp_login() -> Dict[str, Any]:
    """
    CDP 模式：接入使用者自己的 Chrome，實際開啟 AI Studio 檢查登入狀態。
    這取代了舊版「headless + cookie 重放」的驗證方式。
    """
    probe = await check_cdp_available()
    if not probe.get("available"):
        return {
            "logged_in": False,
            "browser_mode": "cdp",
            "message": probe.get("message", "找不到可控制的 Chrome"),
            "launch_command": probe.get("launch_command"),
            "endpoint": probe.get("endpoint"),
        }

    cfg = get_web_config()
    service = cfg.get("target_service", "aistudio")
    url = ("https://aistudio.google.com/prompts/new_chat"
           if service == "aistudio" else "https://gemini.google.com/app")

    try:
        page = await _get_cdp_page()
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await asyncio.sleep(2.5)
        current_url = (page.url or "").lower()
        title = await page.title()
        is_signin = "accounts.google.com" in current_url or "signin" in current_url
        site_label = "Google AI Studio" if service == "aistudio" else "Gemini 官方網頁"
        out: Dict[str, Any] = {
            "logged_in": not is_signin,
            "browser_mode": "cdp",
            "service": service,
            "site": site_label,
            "current_url": current_url,
            "title": title,
            "endpoint": probe.get("endpoint"),
            "browser": probe.get("browser"),
            "message": (
                f"✅ 已接入您的 Chrome，{site_label} 為已登入狀態（真實 session，無 cookie 重放）"
                if not is_signin else
                f"⚠️ 已接入您的 Chrome，但尚未登入 {site_label}。"
                "請在該 Chrome 視窗中手動登入後再試。"
                + ("（注意：AI Studio 與 Gemini 的登入狀態是分開的）" if service == "aistudio" else "")
            ),
        }
        return out
    except Exception as e:
        return {
            "logged_in": False,
            "browser_mode": "cdp",
            "error": str(e),
            "message": f"CDP 驗證失敗: {e}",
        }


async def check_login_status() -> Dict[str, Any]:
    """
    驗證登入狀態。CDP 模式走 check_cdp_login()；舊版模式走 storage_state + headless。
    """
    if is_cdp_mode():
        return await check_cdp_login()

    # ── 以下為舊版 launch 模式（storage_state + headless）──
    # 驗證 storage_state.json 中的登入狀態是否仍然有效（用 headless 訪問 AI Studio）。
    # 如果沒有 storage_state，直接回報未登入狀態（不啟動瀏覽器）。
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
    執行單頁網頁自動化 OCR。
    依 browser_mode 決定走 CDP（接入使用者自己的 Chrome）或舊版 launch。
    回傳: (成功, 文字/錯誤訊息, HTTP狀態碼)
    """
    cfg = get_web_config()
    if not cfg.get("enabled", True):
        return False, "網頁自動化 (Web RPA) 功能目前已停用", 400

    default_service = cfg.get("target_service", "aistudio")
    service, clean_model = resolve_web_service(model_name, default_service)
    timeout_sec = timeout or cfg.get("timeout_seconds", 60)

    if not image_path.exists():
        return False, f"頁面切圖檔案不存在: {image_path}", 404

    if is_cdp_mode():
        return await _run_ocr_via_cdp(image_path, service, clean_model, prompt, timeout_sec, cfg)
    return await _run_ocr_via_launch(image_path, service, clean_model, prompt, timeout_sec, cfg)


async def _run_ocr_via_cdp(
    image_path: Path, service: str, clean_model: str,
    prompt: str, timeout_sec: int, cfg: Dict[str, Any]
) -> Tuple[bool, str, int]:
    """
    CDP 模式：接入使用者自己開的 Chrome。
    - 不啟動新瀏覽器、不重放 cookie、不關閉瀏覽器
    - 全程重用同一個 GASOCR 專用分頁
    """
    probe = await check_cdp_available()
    if not probe.get("available"):
        return False, probe["message"], 503

    max_attempts = 3
    async with _rpa_lock:
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            try:
                page = await _get_cdp_page()
                if service == "aistudio":
                    success, text_or_err = await _execute_aistudio_ocr(
                        page, image_path, clean_model, prompt, timeout_sec
                    )
                else:
                    success, text_or_err = await _execute_gemini_web_ocr(
                        page, image_path, clean_model, prompt, timeout_sec
                    )
                if success:
                    cleaned = clean_ocr_response_text(text_or_err)
                    invalid = _validate_ocr_output(cleaned)
                    if not invalid:
                        # CDP 模式不需回存 cookie：profile 本身就是真實登入狀態
                        return True, cleaned, 200
                    # 拒答／沒收到圖片是「隨機發生」的（實測同一頁有時成功有時拒答），
                    # 因此重試而非直接失敗。
                    last_err = invalid
                    print(f"⚠️ 第 {attempt}/{max_attempts} 次輸出無效，將重試：{invalid[:90]}")
                    if attempt < max_attempts:
                        await asyncio.sleep(2.0)
                        continue
                    return False, f"{invalid}（已重試 {max_attempts} 次）", 502

                # executor 本身回報失敗
                last_err = text_or_err
                if attempt < max_attempts:
                    print(f"⚠️ 第 {attempt}/{max_attempts} 次執行失敗，將重試：{text_or_err[:90]}")
                    await asyncio.sleep(1.5)
                    continue
                return False, text_or_err, 500
            except Exception as e:
                last_err = str(e)
                print(f"⚠️ CDP OCR 第 {attempt}/{max_attempts} 次嘗試異常: {e}")
                # 只脫離連線再重連；絕不關閉使用者的瀏覽器
                await _reset_cdp_state()
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(1.5)
        return False, f"CDP 網頁自動化失敗（已重試 {max_attempts} 次）：{last_err}", 500


async def _run_ocr_via_launch(
    image_path: Path, service: str, clean_model: str,
    prompt: str, timeout_sec: int, cfg: Dict[str, Any]
) -> Tuple[bool, str, int]:
    """
    舊版模式：Playwright 自行啟動瀏覽器 + 重放 storage_state cookie。
    已知會被 AI Studio 的自動化偵測拒絕（permission denied），僅保留作對照。
    """
    if not is_logged_in_cached():
        return False, (
            "尚未登入 Google 帳號。建議改用 CDP 模式：執行 ./start_chrome_cdp.sh "
            "開啟專屬 Chrome 並手動登入後，將瀏覽器模式切換為「接入現有 Chrome」。"
        ), 401

    chrome_path = cfg.get("chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    headless = cfg.get("headless", True)
    if not Path(chrome_path).exists():
        return False, f"未找到本機 Chrome 瀏覽器: {chrome_path}", 500

    async with _rpa_lock:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=headless,
                    executable_path=chrome_path,
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-blink-features=AutomationControlled",
                    ],
                    ignore_default_args=["--enable-automation"],
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
                        invalid = _validate_ocr_output(text_or_err)
                        if invalid:
                            await browser.close()
                            return False, invalid, 502
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
    # 用 ?model= 參數真正切換模型（先前 model_name 完全沒被使用，下拉選單形同裝飾）
    base_url = "https://aistudio.google.com/prompts/new_chat"
    target_url = f"{base_url}?model={model_name}" if model_name else base_url
    if model_name:
        print(f"🌐 [AI Studio] 導航至新對話，指定模型: {model_name}")
    await page.goto(target_url, timeout=45000, wait_until="domcontentloaded")
    await asyncio.sleep(2.5)

    if "accounts.google.com" in page.url or "signin" in page.url:
        return False, (
            "尚未登入 Google AI Studio。請在您自己開啟的那個 Chrome 視窗中，"
            "前往 aistudio.google.com 完成登入（AI Studio 與 Gemini 的登入狀態是分開的），"
            "登入後回到 GASOCR 重新執行。"
        )

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

    # 送出（點擊 Run 按鈕，失敗才用鍵盤快速鍵）
    # ⚠️ macOS 的送出鍵是 ⌘ + Enter（Meta+Enter），不是 Ctrl+Enter。
    #    實測：在 macOS 按 Control+Enter 完全不會送出（連請求都不會發出），
    #    畫面提示也是「Send prompt (⌘ + Enter)」。故依平台選擇正確的修飾鍵。
    send_key = "Meta+Enter" if sys.platform == "darwin" else "Control+Enter"
    run_btn = page.locator("button:has-text('Run')").first
    if await run_btn.count() > 0 and await run_btn.is_enabled():
        try:
            await run_btn.click(timeout=5000)
        except Exception:
            await page.keyboard.press(send_key)
    else:
        await page.keyboard.press(send_key)

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


# ══════════════════════════════════════════════════════════════
#  HTML → Markdown 轉換器（在瀏覽器內執行）
#
#  為什麼需要：Gemini / AI Studio 的網頁會把 Markdown「渲染成 HTML」再顯示。
#  用 innerText 取文字等於只拿到渲染後的字，`#`、`**`、表格語法在渲染時就被吃掉了，
#  所以輸出會失去 Markdown 結構。這裡改成走訪 DOM 並還原成 Markdown。
# ══════════════════════════════════════════════════════════════
_JS_HTML_TO_MARKDOWN = r"""
(promptStr) => {
  // ── inline 層級 ──────────────────────────────────────────
  const mdInline = (node) => {
    let out = '';
    node.childNodes.forEach(ch => {
      if (ch.nodeType === 3) { out += ch.textContent.replace(/\s+/g, ' '); return; }
      if (ch.nodeType !== 1) return;
      const tag = ch.tagName.toLowerCase();
      const cls = (ch.className || '').toString();
      if (/cdk-visually-hidden|screen-reader/.test(cls)) return;
      if (tag === 'br') { out += '\n'; return; }
      if (tag === 'strong' || tag === 'b') {
        const t = mdInline(ch).trim(); out += t ? '**' + t + '**' : ''; return;
      }
      if (tag === 'em' || tag === 'i') {
        const t = mdInline(ch).trim(); out += t ? '*' + t + '*' : ''; return;
      }
      if (tag === 'code') { out += '`' + ch.textContent.trim() + '`'; return; }
      if (tag === 'a') {
        const h = ch.getAttribute('href') || '';
        const t = mdInline(ch).trim();
        out += (h && !h.startsWith('#')) ? '[' + t + '](' + h + ')' : t; return;
      }
      if (tag === 'sup') { out += '^' + mdInline(ch).trim(); return; }
      out += mdInline(ch);
    });
    return out;
  };

  // ── 清單 ────────────────────────────────────────────────
  const mdList = (list, depth) => {
    const ordered = list.tagName.toLowerCase() === 'ol';
    let out = '';
    let i = 1;
    Array.from(list.children).forEach(li => {
      if (li.tagName.toLowerCase() !== 'li') return;
      const marker = ordered ? (i++) + '. ' : '- ';
      let text = '', nested = '';
      li.childNodes.forEach(c => {
        if (c.nodeType === 1 && /^(ul|ol)$/i.test(c.tagName)) nested += mdList(c, depth + 1);
        else if (c.nodeType === 1) text += mdInline(c);
        else if (c.nodeType === 3) text += c.textContent.replace(/\s+/g, ' ');
      });
      out += '  '.repeat(depth) + marker + text.trim() + '\n';
      if (nested) out += nested;
    });
    return out;
  };

  // ── 表格 ────────────────────────────────────────────────
  const mdTable = (tbl) => {
    const rows = [];
    tbl.querySelectorAll('tr').forEach(tr => {
      const cells = [];
      tr.querySelectorAll('th,td').forEach(c => {
        cells.push(mdInline(c).trim().replace(/\|/g, '\\|').replace(/\n+/g, ' '));
      });
      if (cells.length) rows.push({ cells, header: !!tr.querySelector('th') });
    });
    if (!rows.length) return '';
    const cols = Math.max(...rows.map(r => r.cells.length));
    const pad = (r) => { const c = r.cells.slice(); while (c.length < cols) c.push(''); return c; };
    let out = '| ' + pad(rows[0]).join(' | ') + ' |\n';
    out += '| ' + Array(cols).fill('---').join(' | ') + ' |\n';
    rows.slice(1).forEach(r => { out += '| ' + pad(r).join(' | ') + ' |\n'; });
    return out;
  };

  // ── block 層級 ──────────────────────────────────────────
  const mdBlock = (node, depth) => {
    let out = '';
    node.childNodes.forEach(ch => {
      if (ch.nodeType === 3) {
        const t = ch.textContent.replace(/\s+/g, ' ');
        if (t.trim()) out += t;
        return;
      }
      if (ch.nodeType !== 1) return;
      const tag = ch.tagName.toLowerCase();
      const cls = (ch.className || '').toString();
      if (/cdk-visually-hidden|screen-reader|sources-list|action-buttons|tts-button|tooltip/.test(cls)) return;
      if (tag === 'button' || tag === 'sources-list') return;

      if (/^h[1-6]$/.test(tag)) {
        const lvl = parseInt(tag[1], 10);
        const t = mdInline(ch).trim();
        if (t) out += '\n\n' + '#'.repeat(lvl) + ' ' + t + '\n\n';
      } else if (tag === 'p') {
        const t = mdInline(ch).trim();
        if (t) out += '\n\n' + t + '\n\n';
      } else if (tag === 'br') {
        out += '\n';
      } else if (tag === 'hr') {
        out += '\n\n---\n\n';
      } else if (tag === 'ul' || tag === 'ol') {
        out += '\n\n' + mdList(ch, depth) + '\n';
      } else if (tag === 'blockquote') {
        const t = mdInline(ch).trim();
        if (t) out += '\n\n' + t.split('\n').map(l => '> ' + l).join('\n') + '\n\n';
      } else if (tag === 'pre') {
        out += '\n\n```\n' + (ch.innerText || '').trim() + '\n```\n\n';
      } else if (tag === 'table') {
        const t = mdTable(ch);
        if (t) out += '\n\n' + t + '\n';
      } else {
        out += mdBlock(ch, depth);
      }
    });
    return out;
  };

  // ── 取得最後一個模型回應 ────────────────────────────────
  const responses = document.querySelectorAll('model-response, [data-turn-role="Model"]');
  if (!responses.length) return { status: 'waiting' };
  const last = responses[responses.length - 1];

  const errEl = last.querySelector('.model-error, .error-container, [role="alert"]');
  if (errEl) {
    const t = (errEl.innerText || '').trim();
    if (t) return { status: 'error', text: t };
  }

  // 內容所在的 Markdown 面板（實測 DOM：div.markdown.markdown-main-panel）
  const mdRoot = last.querySelector('.markdown-main-panel')
              || last.querySelector('message-content .markdown')
              || last.querySelector('.markdown')
              || last.querySelector('message-content');
  if (!mdRoot) return { status: 'generating' };

  let text = mdBlock(mdRoot, 0)
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  if (!text) return { status: 'generating' };

  const plain = (mdRoot.innerText || '').trim();
  if (plain === promptStr ||
      (promptStr.length > 20 && promptStr.startsWith(plain.slice(0, 50)))) {
    return { status: 'prompt_echo' };
  }
  if (/未夾帶|尚未夾帶|未上傳圖片|尚未上傳圖片/.test(plain)) {
    return { status: 'upload_error', text: plain };
  }

  // 是否還在生成？用「停止回覆」按鈕判斷（實測 aria-label="停止回覆"，生成中出現、完成後消失）。
  // 這比只靠文字穩定可靠：Gemini 串流中間會停頓，純穩定度判斷會提早收工造成截斷。
  const stopBtn = last.querySelector('button[aria-label*="停止"], button[aria-label*="Stop"]')
               || document.querySelector('button[aria-label*="停止"], button[aria-label*="Stop"]');
  return { status: 'success', text: text, streaming: !!stopBtn };
}
"""


async def _count_gemini_blob_images(page) -> int:
    """計算頁面上已附加的圖片縮圖數量（用於判斷新附件是否真的出現）"""
    try:
        return await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('img')).filter(i => {
                const s = i.currentSrc || i.src || '';
                return s.startsWith('blob:') || s.startsWith('data:image');
            }).length;
        }''')
    except Exception:
        return 0


async def _wait_gemini_attachment(page, baseline: int, timeout: int = 15) -> bool:
    """等待 Gemini 輸入區「新增」圖片縮圖，確認附件真的就位"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await _count_gemini_blob_images(page) > baseline:
            return True
        await asyncio.sleep(0.8)
    return False


async def _attach_image_gemini(page, image_path: Path) -> bool:
    """
    把切圖附加到 Gemini 輸入框，並以「縮圖數量增加」驗證真的附加成功。

    先前版本只做 set_input_files 就直接送出，沒有任何驗證，
    結果模型回「沒有看到您上傳的圖片」卻被當成 success。
    """
    resolved = str(image_path.resolve())
    baseline = await _count_gemini_blob_images(page)

    # 策略 1：DOM 中已有 file input → 直接注入（最可靠，不需點任何選單）
    try:
        fi = page.locator("input[type='file']")
        if await fi.count() > 0:
            await fi.first.set_input_files(resolved)
            if await _wait_gemini_attachment(page, baseline, timeout=15):
                print("📎 Gemini 附件已就位（策略 1：直接注入 file input）")
                return True
    except Exception as e:
        print(f"⚠️ Gemini 策略 1 失敗: {e}")

    # 策略 2：點「+ / 上傳與工具」按鈕並攔截 file chooser
    button_selectors = [
        "button[aria-label*='上傳與工具']",
        "button[aria-label*='Upload']",
        "button[aria-label*='upload']",
        "button[aria-label*='新增']",
        "button[aria-label*='Add']",
    ]
    for sel in button_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                await btn.click()
            fc = await fc_info.value
            await fc.set_files(resolved)
            if await _wait_gemini_attachment(page, baseline, timeout=15):
                print(f"📎 Gemini 附件已就位（策略 2：{sel} + file chooser）")
                return True
        except Exception:
            continue

    # 策略 3：先開選單，再點「上傳檔案」選單項
    menu_selectors = [
        "[role='menuitem']:has-text('上傳')",
        "[role='menuitem']:has-text('Upload')",
        "button:has-text('上傳檔案')",
        "button:has-text('Upload file')",
    ]
    for sel in menu_selectors:
        try:
            item = page.locator(sel).first
            if await item.count() == 0 or not await item.is_visible():
                continue
            async with page.expect_file_chooser(timeout=6000) as fc_info:
                await item.click()
            fc = await fc_info.value
            await fc.set_files(resolved)
            if await _wait_gemini_attachment(page, baseline, timeout=15):
                print(f"📎 Gemini 附件已就位（策略 3：{sel}）")
                return True
        except Exception:
            continue

    return False


async def _execute_gemini_web_ocr(
    page, image_path: Path, model_name: str, prompt: str, timeout_sec: int
) -> Tuple[bool, str]:
    """在 Gemini 官方網頁執行 OCR 流程"""
    await page.goto("https://gemini.google.com/app", timeout=45000, wait_until="domcontentloaded")
    await asyncio.sleep(2.5)

    if "accounts.google.com" in page.url or "signin" in page.url:
        return False, (
            "尚未登入 Gemini。請在您自己開啟的那個 Chrome 視窗中完成 Google 登入後再試。"
        )

    # 1-2. 附加切圖，並驗證縮圖真的出現。
    #      沒有附加成功就大聲失敗，絕不送出無圖片的請求（否則模型會回「沒有看到圖片」）。
    attached = await _attach_image_gemini(page, image_path)
    if not attached:
        return False, (
            "圖片附加失敗：在 Gemini 輸入區等不到圖片縮圖，已中止送出。"
            "（若持續發生，代表 Gemini 網頁版面已變更，需要更新上傳選擇器。）"
        )

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
        # 使用 HTML→Markdown 轉換器：網頁端是「渲染後」的內容，
        # innerText 會吃掉 #、**、表格等 Markdown 語法。
        res = await page.evaluate(_JS_HTML_TO_MARKDOWN, clean_prompt)

        status = res.get("status")
        if status == "error":
            return False, f"Gemini 官方網頁回傳錯誤: {res.get('text')}"

        if status == "upload_error":
            return False, f"Gemini 官方網頁回報未成功接收圖片: {res.get('text')}。建議重試本頁。"

        if status == "success":
            # 仍在生成中就再等一輪，避免把串流中的片段當成完整結果
            if res.get("streaming"):
                continue
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
