#!/bin/bash
#
# GASOCR - 啟動「可供 GASOCR 接入的 Chrome」(CDP 模式)
#
# 為什麼需要這支腳本：
#   反機器人通行證綁在 TLS／瀏覽器指紋＋IP 信譽上，不是只綁 cookie。
#   把 cookie 重放進一個全新的無頭 context 會被 Google 判定為被盜 session，
#   於是 AI Studio 回 "permission denied"。
#   解法是讓「你自己」開一個真實的 Chrome，GASOCR 再接入它。
#
# 使用方式：
#   1. 執行 ./start_chrome_cdp.sh
#   2. 在彈出的 Chrome 視窗中「手動」登入 Google 帳號（只需一次）
#   3. 保持該視窗開著，回到 GASOCR 網頁點「檢查 CDP 連線」
#
set -uo pipefail

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "${DIR}"

# ── 預設值 ────────────────────────────────────────────────────────────
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT=9222
PROFILE="${DIR}/data/chrome_profile_rpa"
DEFAULT_URL="https://gemini.google.com/app"

# ── 挑一個可用的 python ───────────────────────────────────────────────
PY=""
if [ -x "${DIR}/.venv/bin/python" ]; then
    PY="${DIR}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
fi

# ── 從 data/web_rpa_config.json 覆寫設定（若有）───────────────────────
CFG="${DIR}/data/web_rpa_config.json"
if [ -f "${CFG}" ] && [ -n "${PY}" ]; then
    CONFIG_ASSIGNMENTS="$("${PY}" - "${CFG}" "${DIR}" 2>/dev/null <<'PYCODE'
import json, shlex, sys

cfg_path, base = sys.argv[1], sys.argv[2]
try:
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
except Exception:
    cfg = {}


def emit(name, value):
    print("%s=%s" % (name, shlex.quote(str(value))))


emit("PORT", cfg.get("cdp_port") or 9222)

profile = cfg.get("cdp_user_data_dir") or "data/chrome_profile_rpa"
if not str(profile).startswith("/"):
    profile = base.rstrip("/") + "/" + str(profile)
emit("PROFILE", profile)

chrome = cfg.get("chrome_path") or ""
if chrome:
    emit("CHROME", chrome)
PYCODE
)"
    if [ -n "${CONFIG_ASSIGNMENTS}" ]; then
        eval "${CONFIG_ASSIGNMENTS}"
    fi
fi

echo "=================================================="
echo "  GASOCR · CDP 模式 Chrome 啟動器"
echo "=================================================="
echo "  Chrome  : ${CHROME}"
echo "  除錯埠  : ${PORT}"
echo "  Profile : ${PROFILE}"
echo "--------------------------------------------------"

if [ ! -x "${CHROME}" ]; then
    echo "❌ 找不到 Chrome：${CHROME}"
    echo "   請確認已安裝 Google Chrome，或在 GASOCR 設定中修改 chrome_path。"
    exit 1
fi

# ── 除錯埠是否已經有 Chrome 在監聽？──────────────────────────────────
port_is_listening() {
    [ -n "${PY}" ] || return 1
    "${PY}" -c "
import socket, sys
s = socket.socket(); s.settimeout(1.0)
sys.exit(0 if s.connect_ex(('127.0.0.1', ${PORT})) == 0 else 1)
"
}

if port_is_listening; then
    echo "✅ 除錯埠 ${PORT} 已經有 Chrome 在監聽，無需重複啟動。"
    echo "   直接回到 GASOCR 網頁點『檢查 CDP 連線』即可。"
    echo
    echo "   若連線失敗，可能是那個 Chrome 不是用本專案 Profile 啟動的，"
    echo "   或它不是 Chrome 而是別的程序佔用了 ${PORT} 埠。"
    exit 0
fi

# ── 清理本專案 Profile 的殘留鎖檔（不影響系統預設 Chrome）────────────
for f in SingletonLock SingletonCookie SingletonSocket; do
    if [ -e "${PROFILE}/${f}" ] || [ -L "${PROFILE}/${f}" ]; then
        rm -f "${PROFILE}/${f}" 2>/dev/null && echo "🧹 已清理殘留的 ${f}"
    fi
done

mkdir -p "${PROFILE}"

echo "🚀 正在啟動 Chrome（帶除錯埠）..."
echo
echo "   【下一步】請在彈出的視窗中手動登入 Google 帳號。"
echo "   登入後「不要關閉」這個視窗，回到 GASOCR 點『檢查 CDP 連線』。"
echo

# 從 Chrome 執行檔路徑推出 .app 路徑，以便使用 macOS 原生 open -na 啟動。
# 用 open -na 的原因：它會讓 Chrome 完全脫離本 shell 的程序群組，
# 因此關閉終端機或本腳本結束都不會連帶殺掉 Chrome。
CHROME_APP=""
case "${CHROME}" in
    *.app/Contents/MacOS/*)
        CHROME_APP="${CHROME%%.app/Contents/MacOS/*}.app"
        ;;
esac

if [ -n "${CHROME_APP}" ] && [ -d "${CHROME_APP}" ] && command -v open >/dev/null 2>&1; then
    open -na "${CHROME_APP}" --args \
        --remote-debugging-port="${PORT}" \
        --user-data-dir="${PROFILE}" \
        --no-first-run \
        --no-default-browser-check \
        "${DEFAULT_URL}"
    LAUNCH_MODE="open -na"
else
    nohup "${CHROME}" \
        --remote-debugging-port="${PORT}" \
        --user-data-dir="${PROFILE}" \
        --no-first-run \
        --no-default-browser-check \
        "${DEFAULT_URL}" \
        >/dev/null 2>&1 &
    disown 2>/dev/null || true
    LAUNCH_MODE="nohup"
fi

# 等 Chrome 起來並開啟除錯埠
READY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if port_is_listening; then
        READY=1
        break
    fi
    sleep 1
done

echo "   （啟動方式：${LAUNCH_MODE}）"
if [ "${READY}" = "1" ]; then
    echo "✅ Chrome 已啟動，除錯埠 ${PORT} 正在監聽。"
    echo "   現在可以回到 GASOCR 網頁，點『① 檢查 CDP 連線』→『② 接入並驗證登入』。"
else
    echo "⚠️  Chrome 已啟動，但除錯埠 ${PORT} 尚未就緒。"
    echo "   請稍等幾秒後，在 GASOCR 點『檢查 CDP 連線』。"
    echo "   若持續失敗，請確認沒有其他 Chrome 正使用同一個 Profile，"
    echo "   或改用『複製啟動指令』在終端機手動執行以查看錯誤訊息。"
fi
