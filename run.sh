#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# 確保虛擬環境存在
if [ ! -d ".venv" ]; then
    echo "⚙️ 正在建立虛擬環境..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
else
    source .venv/bin/activate
fi

URL="http://127.0.0.1:8610"

# 伺服器啟動後自動於預設瀏覽器開啟網址
(
    sleep 1.5
    if command -v open > /dev/null; then
        open "$URL"
    elif command -v xdg-open > /dev/null; then
        xdg-open "$URL"
    fi
) &

echo "=================================================="
echo "🚀 啟動 Google AI Studio Gemini OCR Web 服務..."
echo "📍 網址: $URL (將自動在瀏覽器開啟)"
echo "=================================================="

exec uvicorn main:app --host 0.0.0.0 --port 8610 --reload
