import os
from pathlib import Path

# Application & Version Control
APP_NAME = "GASOCR"
APP_VERSION = "1.0.0"
BUILD_NUMBER = "Build 005"

# Base directories
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
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
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_RENDER_DPI = 300            # 高清渲染解析度 (預設 300 DPI，文獻印刷級清晰度)

# Model options
AVAILABLE_MODELS = [
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash (預設推薦，極速免費)"},
    {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash (穩定高辨識)"},
    {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash-Lite (極低延遲)"},
    {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash (經典版本)"},
    {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro (超高精度，但 RPM 較低)"},
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
