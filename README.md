# 📖 GASOCR (Google AI Studio Gemini PDF OCR)

<p align="center">
  <img src="https://img.shields.io/badge/Project-GASOCR-blue?style=for-the-badge&logo=google" alt="GASOCR" />
  <img src="https://img.shields.io/badge/Build-019-indigo?style=for-the-badge" alt="Build 019" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/Security-Access%20Gate%20%7C%20Admin-emerald?style=for-the-badge&logo=auth0" alt="Security" />
  <img src="https://img.shields.io/badge/Gemini-3.5%20%7C%203.7%20%7C%202.5-4285F4?style=for-the-badge&logo=googlegemini" alt="Gemini Models" />
  <img src="https://img.shields.io/badge/Cost-Strict%200%20Free%20Tier-success?style=for-the-badge" alt="Zero Cost" />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/UI-Light%20%7C%20Dark%20%7C%20Gruvbox-f59e0b?style=for-the-badge" alt="Themes" />
</p>

**GASOCR** 是一套專為文獻古籍、期刊論文與現代書籍打造的高精準度、零成本（\$0 Free Tier）PDF OCR 轉譯 Web 系統。基於 Google AI Studio 所提供的 Gemini 視覺語言模型，整合多帳號負載均衡、300 DPI 逐頁高品質切圖渲染、直排雙欄排版解構、任務暫停換模型續傳，以及左圖右文校對檢視器。

---

## 🌟 核心亮點 (Key Features)

### 1. 🛡️ 嚴格 \$0 免費額度保證 (Zero-Cost Guaranteed)
- **15 RPM 速率保護器**：調度器強制每組金鑰請求間隔 $\ge 4.2$ 秒，嚴格卡位在 Google AI Studio Free Tier 安全線內。
- **429 自動避讓與冷卻**：若遇尖峰限額觸發 `429 RESOURCE_EXHAUSTED`，系統自動將該 Key 進入短暫冷卻佇列，並無縫切換至池中下一組可用帳號接力轉譯，絕不扣款、絕不報錯中斷。

### 2. 🔑 多帳號管理池（雙軌支援）
- **API Key 池**：支援在前端 Web 介面新增任意數量的 Google 帳號 API Key，系統自動多帳號輪詢（Round-Robin），吞吐量成倍提升。
- **OAuth 2.0 支援**：支援 Client ID / Secret / Refresh Token 模式。
- **安全防護**：金鑰儲存於本機 SQLite 資料庫，前端顯示自動進行脫敏遮罩（如 `AIza...5678`），防止截圖外洩。

### 3. 📦 批次檔案轉譯佇列 (Batch Tasks Box)
- **大量上傳佇列**：支援一次拖曳或選取多個 PDF 檔案建立批次轉譯任務。
- **即時獨立進度**：每組任務具備獨立的彩色進度條與狀態指示燈（切圖中、轉譯中、已暫停、已完成、失敗），多任務同時進行時彼此完全隔離，互不干擾。

### 4. ⏸️ 任務暫停、即時切換模型續傳
- **隨時暫停/繼續**：任務執行中可點擊「⏸ 暫停」，稍後點擊「▶ 繼續」即可無縫接續未完成的頁面。
- **膠囊式更換模型選單**：任務在暫停狀態下，表格列自動提供微型膠囊選單，可自由更換欲接續的模型（例如從 `gemini-2.5-flash` 切換成 `gemini-2.0-flash`），原先設定的自訂 Prompt 永久保留，後續頁面自動採用新模型。
- **單頁模型稽核標記**：每頁轉譯成果均記錄實際使用的模型名稱（`used_model`），校對時一目了然。

### 5. 📄 彈性頁碼區間選擇 (Page Range Selector)
- **自訂頁碼範圍**：支援指定「起始頁 (Start)」與「結束頁 (End)」，隨心所欲僅轉譯需要的段落章節（如 P.1 ~ P.10）。
- **快捷按鈕**：提供「全本」、「僅第 1 頁」、「前 5 頁」、「前 10 頁」快速點選。

### 6. 📐 古籍與排版深度解構
- **語言偏好**：繁體中文（預設臺灣常用字與全形標點符號）、簡體中文、保持原文。
- **閱讀方向**：自動偵測、橫排（左→右）、直排/豎排（右→左縱向閱讀）。
- **分欄結構**：自動偵測、單欄、雙欄、三欄（強制依欄閱讀，嚴禁跨欄串讀、垂直貫穿混讀）。
- **多行大提示詞框**：支援輸入客製化 Prompt（例如：忽略讀音標記、完整保留雙行夾註、字詞對照表、缺字標記規則等）。

### 7. 🎨 三種現代化主題 (Theme Selector)
- 頁面頂端提供 **淺色 (Light)**、**深色 (Dark)**、**Gruvbox** 三種專業主題切換。
- 全背景滾動防漏白修復，滾動至頁底始終維持統一配色。
- 主題偏好自動保存於瀏覽器 `localStorage`。

### 8. 🔍 左圖右文校對檢視器 (Side-by-Side Proofreader)
- **逐頁對照**：左側呈現高清晰 PDF 渲染原圖，右側為 Markdown / 文字編輯區。
- **鍵盤快速鍵切換頁碼**：
  - **前一頁**：`⌥ + ←`（macOS）／`Alt + ←`（Windows/Linux），或 `⌥ + [`
  - **後一頁**：`⌥ + →`（macOS）／`Alt + →`（Windows/Linux），或 `⌥ + ]`
  - **瀏覽模式**（輸入框未聚焦時）亦可直接按 `←`、`→`、`[`、`]`
  - **快速儲存**：`⌘ + S`（macOS）／`Ctrl + S`（Windows/Linux）
  - **原模重辨**：`⌥/Alt + R`　**升級重辨**：`⌥/Alt + U`　**暫停/繼續**：`⌥/Alt + P`
  - **開關重辨佇列**：`⌥/Alt + Q`，或輸入框未聚焦時直接按 `Q`
  - **關閉彈窗**：`Esc`

> 🍎 **macOS 相容性說明**
> 網頁端快速鍵一律以 `event.code`（實體鍵位置）判斷，而非 `event.key`。
> 原因是 macOS 的 `Option (⌥)` 是**組合鍵**，會改寫 `event.key`：
> `⌥Q`→`œ`、`⌥R`→`®`、`⌥U`→死鍵、`⌥P`→`π`、`⌥[`→`“`、`⌥]`→`‘`，
> 因此用 `event.key` 比對字母／括號在 macOS 上會全部失效。改用 `event.code` 後兩平台皆正常。
>
> 另外兩點 macOS 專屬處理：
> 1. 在**文字編輯框內**編輯時，`⌥←`/`⌥→` 保留給 macOS 原生的「逐詞移動游標」，不會被翻頁快捷鍵搶走；編輯中請改用 `⌥ + [`、`⌥ + ]` 或 `Fn + ↑/↓` 翻頁。Windows/Linux 的 `Alt + ←/→` 無此衝突，編輯中仍可直接翻頁。
> 2. MacBook 沒有 `PageUp`/`PageDown` 實體鍵，對應為 `Fn + ↑` / `Fn + ↓`。
- **線上即時修正**：校對後可直接點擊「儲存修改」，即時持久化至資料庫。
- **單頁重新辨識**：點擊「重新辨識本頁」使用原任務模型直接背景執行。
- **升級模型單頁重新辨識 (`upgrademodel`)**：提供模型下拉選單，可挑選任意高階模型（如 `gemini-2.5-pro`），點擊「升級重新辨識」即可僅對該頁改用指定模型重跑，其餘 Prompt 與排版設定完全繼承原任務。辨識完成後自動更新為該頁專屬模型標籤。

### 9. 🔐 安全管理後台與全站通關密碼保護 (Access Gate)
- **初次安裝引導 (Setup Wizard)**：首次啟動系統時自動引導設置管理員密碼（PBKDF2 加鹽加密儲存），杜絕未授權進入。支援 Docker `ADMIN_PASSWORD` 環境變數全自動初始化。
- **全站通關保護開關**：管理員可一鍵開啟「通關密碼保護」，訪客需輸入通關密碼解鎖後方可進入系統操作，未解鎖前 API 請求一律由中介軟體攔截阻擋（401 Unauthorized）。
- **管理員專屬後台**：提供獨立控制面板，包含通關密碼變更、管理員密碼修改，以及金鑰池與任務狀態監控面板。

### 10. 💾 多格式打包匯出
- **Markdown (`.md`)**：標準語法，含標題、引言與分頁註記。
- **純文字 (`.txt`)**：乾淨純文字排版。
- **Microsoft Word (`.docx`)**：結構化文件，方便排版列印。
- **完整 ZIP 壓縮包 (`.zip`)**：包含所有格式文本及每一頁的 300 DPI 渲染圖。

---

## 🛠️ 技術架構 (Tech Stack)

- **後端框架**：[FastAPI](https://fastapi.tiangolo.com/) (ASGI 高效能非同步 Web 框架)
- **資料庫**：SQLite + [aiosqlite](https://github.com/omnilib/aiosqlite)
- **PDF 渲染引擎**：[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (高品質 PDF 逐頁渲染)
- **AI 整合**：Google Generative Language API (`google-genai` / REST API)
- **即時串流**：Server-Sent Events (SSE, `EventSource`)
- **前端介面**：[TailwindCSS](https://tailwindcss.com/) + [Alpine.js](https://alpinejs.dev/) + [Lucide Icons](https://lucide.dev/)

---

## 📁 專案目錄結構

```text
GASOCR/
├── run.sh                     # 一鍵啟動腳本（自動啟用虛擬環境、綁定 port 8610 並開啟瀏覽器）
├── requirements.txt           # 專案相依套件清單
├── config.py                  # 全域設定、路徑、排版參數、預設模型
├── database.py                # SQLite 資料庫操作模組 (Accounts, Tasks, TaskPages)
├── scheduler.py               # 智慧多帳號調度器、15 RPM 節流、429 冷卻輪詢
├── pdf_engine.py              # PDF 高解析度逐頁渲染模組 (pypdfium2)
├── gemini_ocr.py              # Prompt 構建器、Gemini 視覺模型調用與重試、動態模型快取
├── exporters.py               # Markdown / TXT / Word docx / ZIP 打包匯出模組
├── main.py                    # FastAPI 應用、REST API、SSE 即時進度、斷點接續管道
├── templates/
│   └── index.html             # 現代響應式單頁 Web 介面 (Alpine.js + TailwindCSS)
├── test_system.py             # 核心模組整合測試
├── test_api.py                # FastAPI Web API 端點自動化測試
├── verify_model_switch.py     # 暫停換模型續傳端到端驗證腳本
├── README.md                  # 專案說明書
└── data/                      # 資料庫與暫存檔案存放目錄（自動建立）
    ├── ocr.db                 # SQLite 主資料庫
    ├── uploads/               # 上傳之原始 PDF
    └── renders/               # 逐頁切圖渲染快取
```

---

## 🐳 Docker 容器化部署指南 (Docker Ready · GHCR)

GASOCR 已完整支援標準 Docker 與 Docker Compose 部署，並透過 GitHub Actions 自動建置並發布多架構映像檔至 **GitHub Container Registry (GHCR)**：
- **GHCR 映像檔路徑**：`ghcr.io/jmedzen/gasocr:latest`（或 `ghcr.io/jmedzen/gasocr:build-007`）
- **支援架構**：`linux/amd64`, `linux/arm64`（Apple Silicon、Raspberry Pi、x86/x64 伺服器通話支援）
- 內建中文字型渲染、Playwright 依賴、健康檢查與資料持久化，**無需在本機耗時編譯，隨拉即用**！

### 1. Docker Compose 一鍵拉取並啟動（推薦）

只需下載本專案的 `docker-compose.yml`，即可直接拉取 GHCR 映像檔並在背景啟動服務：

```bash
# 拉取最新 GHCR 映像檔並啟動
docker compose pull
docker compose up -d
```

服務啟動後，請以瀏覽器訪問：**`http://localhost:8610`**。

#### 常用指令
```bash
# 查看即時日誌
docker compose logs -f

# 停止服務
docker compose down

# 更新至最新版本
docker compose pull && docker compose up -d
```

### 2. 使用標準 Docker CLI 直接從 GHCR 運行

```bash
# 1. 從 GitHub Container Registry 拉取映像檔
docker pull ghcr.io/jmedzen/gasocr:latest

# 2. 啟動容器（掛載本機 ./data 目錄以持久保存資料）
docker run -d \
  --name gasocr \
  -p 8610:8610 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  ghcr.io/jmedzen/gasocr:latest
```

### 3. 資料持久化與掛載說明 (Data Persistence)
容器內部的 `/app/data` 宣告為資料持久層：
- **`./data/ocr.db`**：SQLite 資料庫（存放 API 金鑰、帳號、轉譯任務、各頁辨識結果）。
- **`./data/uploads/`**：上傳之原始 PDF 與特徵碼快取。
- **`./data/renders/`**：300 DPI 逐頁渲染圖檔。
- **`./data/exports/`**：Markdown、Word、TXT 與打包 ZIP 檔案。

所有資料皆安全保存於宿主機上的 `./data` 目錄，重啟或升級容器完全不遺失資料。

### 4. Chrome CDP 模式連線 (Web RPA 模式)
若使用 Web RPA 並在宿主機啟動了 Chrome（Port 9222）：
- 在 `docker-compose.yml` 中已預設加入 `extra_hosts: ["host.docker.internal:host-gateway"]`。
- 容器內可直接透過 `host.docker.internal:9222` 連接至宿主機的 Chrome 瀏覽器。

---

## 🚀 本機安裝與快速開始 (Quick Start)

### 1. 環境需求
- macOS / Linux / Windows (WSL2)
- Python 3.10 或更高版本

### 2. 一鍵啟動（推薦）

專案根目錄已附帶一鍵啟動腳本：

```bash
cd /Users/jm/SyncDev/A1-antigravity/googleOCR
./run.sh
```

腳本會自動：
1. 偵測並建立 Python 虛擬環境 (`.venv`)。
2. 自動安裝 `requirements.txt` 中的必要相依套件。
3. 啟動 Uvicorn 伺服器並綁定至 **`http://127.0.0.1:8610`**。
4. 自動在您的預設瀏覽器中開啟 GASOCR 網頁。

### 3. 手動啟動

```bash
# 1. 建立並啟動虛擬環境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安裝相依套件
pip install --upgrade pip
pip install -r requirements.txt

# 3. 啟動 Web 服務
uvicorn main:app --host 0.0.0.0 --port 8610 --reload
```

啟動後請以瀏覽器訪問：**`http://127.0.0.1:8610`**

---

## 📖 使用教學 (Walkthrough)

### 步驟 1：新增 Google AI Studio API Key
1. 開啟首頁，點擊右上角的 **「帳號金鑰池」**。
2. 前往 [Google AI Studio](https://aistudio.google.com/) 免費點擊 **Get API Key**（可使用多個 Google 帳號各自申請一把）。
3. 輸入備註名稱與 API Key，點擊「新增金鑰」。
4. 點擊清單旁的 **「測試」** 按鈕確認連線成功。

### 步驟 2：上傳 PDF 並設定轉譯選項
1. 將一或多個 PDF 檔案拖曳至上傳區（支援批次上傳）。
2. **選擇模型**：預設推薦 `gemini-3.5-flash-lite`（極速超低延遲）或 `gemini-2.5-flash`。
3. **設定頁碼範圍**：預設為全本，亦可指定起始頁與結束頁（例如 P.1 ~ P.10）。
4. **選擇排版結構**：
   - 語言：繁體中文、簡體中文、保持原文
   - 方向：自動偵測、橫排、直排（豎排）
   - 欄位：自動偵測、單欄、雙欄、三欄
5. **附加提示詞（選填）**：可在文字框中填寫特殊專有名詞、略過頁首頁尾等要求。
6. 點擊 **「🚀 開始批次 OCR 轉譯」**。

### 步驟 3：管理佇列與暫停換模型
- 在 **「批次轉譯檔案列表 (Batch Tasks Box)」** 中可隨時查看所有文件的進度。
- 若想在轉譯途中換模型：
  1. 點擊該任務列的 **「⏸ 暫停」**。
  2. 在模型下拉膠囊選單中挑選新模型。
  3. 點擊 **「▶ 繼續」**，後續頁面即自動以新模型繼續執行。

### 步驟 4：校對與匯出成果
- 點擊任務列的 **「校對」** 按鈕，下方即展開「左右圖文校對檢視器」。
- 左側核對原圖，右側可直接編輯修正錯別字並點擊「儲存修改」。
- 點擊上方按鈕即可單鍵下載：
  - **Word (`.docx`)**
  - **Markdown (`.md`)**
  - **純文字 (`.txt`)**
  - **打包 ZIP（含原圖）**

---

## 🧪 自動化測試與驗證 (Testing)

本專案包含三組完整的測試腳本：

```bash
# 1. 核心模組測試（排程器、429 冷卻避讓、渲染引擎、Prompt 構建、匯出功能）
python test_system.py

# 2. FastAPI Web API 端點測試（REST API、批次上傳、金鑰脫敏、暫停與續傳）
python test_api.py

# 3. 暫停更換模型端到端實測驗證
python verify_model_switch.py
```

---

## 🔒 安全性與費用說明

- **零費用保證**：本系統所有 API 調用均專為 Google AI Studio 的 Free Tier 速率設計，並具備嚴格的 RPM 限流與 429 退避策略，不綁定付費信用卡亦能穩定持續運作。
- **本地私有化**：所有上傳的 PDF 檔案、切圖渲染快取及辨識文字均保存在本地 `data/` 目錄中，不經由任何第三方伺服器中轉。
- **金鑰脫敏**：前端介面均對 API Key 進行遮罩處理，防止直播或螢幕分享時金鑰外洩。

---

## 📄 License

MIT License. 歡迎自由修改、擴充與交流！
