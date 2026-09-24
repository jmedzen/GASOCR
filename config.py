import os
from pathlib import Path

# Application & Version Control
APP_NAME = "GASOCR"
APP_VERSION = "1.0.0"
BUILD_NUMBER = "Build 026"

# Host & Port settings
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8610"))

# Base directories
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
RENDERS_DIR = DATA_DIR / "renders"
EXPORTS_DIR = DATA_DIR / "exports"

# Create necessary directories
for directory in [DATA_DIR, UPLOADS_DIR, RENDERS_DIR, EXPORTS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Database
DB_PATH = DATA_DIR / "ocr.db"

# Free-tier rate limiting defaults
DEFAULT_RPM_LIMIT = 15
MIN_REQUEST_INTERVAL_SECONDS = 4.2  # 60s / 15 = 4s; 4.2s for safe margin
DEFAULT_COOLDOWN_SECONDS = 60       # If 429 occurs, cooldown key for 60s
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_RENDER_DPI = 300            # 高清渲染解析度 (預設 300 DPI，文獻印刷級清晰度)

# PDF 渲染並發與執行緒控制 (CPU Core - 2，保底至少 1；單一 PDF 使用單一執行緒)
_detected_cores = os.cpu_count() or 4
MAX_RENDER_WORKERS = max(1, int(os.environ.get("MAX_RENDER_WORKERS", _detected_cores - 2)))

# ⚠️ 同時「持有全頁點陣圖」的文件數上限 —— 這是記憶體限制，不是 CPU 限制。
#    300 DPI 的大尺寸掃描，單頁點陣圖就可能佔 100MB 以上；
#    先前此值等於 MAX_RENDER_WORKERS（本機為 8），一次上傳 5~6 個大 PDF 時
#    會同時渲染 5~6 份全頁點陣圖，把記憶體吃爆導致 Python 被 OOM 終止。
#    因此獨立出來並預設保守值 2，可用環境變數 MAX_CONCURRENT_RENDERS 調整。
MAX_CONCURRENT_RENDERS = max(1, int(os.environ.get("MAX_CONCURRENT_RENDERS", 2)))

# 上傳限制：單檔大小上限（MB）。0 表示不限制。
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", 4096))

# 保留的可用磁碟空間（MB）。低於此值就拒收上傳／停止切圖，
# 避免把磁碟寫爆導致 OSError 中斷整批上傳或讓背景渲染崩潰。
MIN_FREE_DISK_MB = int(os.environ.get("MIN_FREE_DISK_MB", 2048))

# Model options
AVAILABLE_MODELS = [
    {"id": "gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash-Lite (預設推薦，極速超低延遲)"},
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash (極速高精度)"},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro (旗艦高精度，複雜古籍推薦)"},
    {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash (次世代旗艦)"},
    {"id": "gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash-Lite (極速輕量)"},
    {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash-Lite (穩定輕量)"},
    {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash (經典穩定)"},
    {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash-Lite (極低延遲)"},
    {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash (舊版相容)"},
    {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro (舊版高精度)"},
]

# Layout configurations
LANGUAGE_OPTIONS = {
    "traditional": "繁體中文 (Traditional Chinese)",
    "simplified": "簡體中文 (Simplified Chinese)",
    "original": "保持原文語言 (Keep Original Language)",
}

DIRECTION_OPTIONS = {
    "auto": "自動偵測 (Auto-detect)",
    "horizontal": "橫排（由左至右、由上至下）",
    "vertical": "直排/豎排（由右至左、由上至下縱向閱讀）",
}

COLUMN_OPTIONS = {
    "auto": "自動偵測 (Auto-detect)",
    "single": "單欄排版 (Single Column)",
    "double": "雙欄排版 (Two Columns - 嚴格按欄閱讀順序輸出)",
    "triple": "三欄排版 (Three Columns - 嚴格按欄閱讀順序輸出)",
}
