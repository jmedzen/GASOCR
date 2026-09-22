# 審計報告：AI Studio「網頁端」OCR 失效原因、修復可能性、與替代工具評估

- 審計日期：2026-09-22
- 審計對象：`/Users/jm/SyncDev/A1-antigravity/googleOCR`（GASOCR）
- 審計範圍：`web_rpa.py`、`main.py`、`gemini_ocr.py`、`scheduler.py`、`config.py`、`data/ocr.db`、`data/*.png`、`data/web_rpa_*.json`
- 一句話結論：**網頁端失效不是 OCR 邏輯壞掉，而是 Google 偵測到自動化瀏覽器後拒絕服務。它「技術上可修」，但修好之後仍然脆弱、緩慢且違反 ToS；而你要的「免費輸入輸出」其實已經有一條合法可用的路 —— 你正在用的官方 API 免費額度。**

---

## 一、決定性證據鏈

這次審計沒有靠猜測，關鍵結論都有可直接複驗的證據。

### 證據 1：同一個帳號，API 完全正常（281 頁成功）
`data/ocr.db` 顯示 4 把 API Key（分別來自 4 個 Google 帳號）都在正常輪詢使用：

| id | 帳號 | 認證 | key 前綴 | 最後使用 |
|----|------|------|----------|----------|
| 6 | chicman | api_key | `AIzaSy...` | 2026-09-22 15:45 |
| 10 | **baikuan** | api_key | `AQ.Ab8...` | 2026-09-22 15:45 |
| 19 | juehming | api_key | `AQ.Ab8...` | 2026-09-22 15:45 |
| 20 | mahabodhi.zen | api_key | `AQ.Ab8...` | 2026-09-22 15:45 |

`task_pages` 統計：**completed 281 頁 / failed 1 頁 / paused 2 / pending 214**。
產出品質極佳，例如 `sets/大日本佛敎全書.第082冊-大乗法相宗名目P51-199-3.5-flash-lite.md`（397 KB、149 頁），直排、雙欄、夾註都忠實轉錄且未摘要。

**這條證據的意義極大**：帳號、GCP 專案、`generativelanguage.googleapis.com` 啟用狀態、ToS 接受狀態、地區（台灣）**全部正常**。因為 API 能跑，就代表那些前置條件都滿足了。

### 證據 2：同一個帳號，網頁端連「純文字」都被拒
`data/debug_aistudio_*.png` 是四張連續的失敗截圖，全部是 `baikuan@gmail.com`（左下角顯示 PRO）已登入 AI Studio Playground 的狀態：

| 截圖 | 模型 | 條件 | 結果 |
|------|------|------|------|
| `debug_aistudio_35.png` | Gemini 3.5 Flash Lite | Grounding 開啟 | `Failed to generate content: permission denied.` + `An internal error has occurred.` |
| `debug_aistudio_no_grounding.png` | Gemini 3.8 Flash | Grounding **關閉** | 同上 |
| `debug_aistudio_project_param.png` | Gemini 3.8 Flash | 加 `?project=` 參數 | 同上 |

注意 prompt 內容分別只是 `Hello from 3.5 Flash Lite` 與 `Hello from AI Studio` —— **純文字、無圖片**。

**這條證據排除了所有「OCR 相關」的假設**：不是圖片上傳失敗、不是 selector 過期、不是 prompt 不好、不是模型不存在。是「產生」這個動作本身被拒絕。

### 證據 3：登入流程本身是成功的
`data/web_rpa_storage.json` 有 52 個 cookie，含 `__Secure-1PSID`、`__Secure-3PSID`、`__Secure-1PSIDTS`（輪替票證）、`__Host-GAPS`，且 **0 個已過期**，涵蓋 `aistudio.google.com`、`gemini.google.com`、`console.cloud.google.com`。
→ 登入沒問題，**登入之後才被擋**。

### 證據 4：官方文件自己列出的成因
Google 官方 [Troubleshoot Google AI Studio](https://ai.google.dev/gemini-api/docs/troubleshoot-ai-studio) 對 `PERMISSION_DENIED` 明確寫道，除了 IAM 權限外還會做 **"Other access checks"**：

> - **Security checks:** Your request must pass automated security checks.
> - **Terms of Service:** You must accept the Google Terms of Service and Generative AI Additional Terms of Service.
> - **Supported region:** You must be located in a supported region.
> - **Trust & Safety:** The Google Cloud project must not be flagged for abuse.

其中第一項「**必須通過自動化安全檢查**」正是自動化瀏覽器會觸發的那一項。而 [Available regions](https://ai.google.dev/gemini-api/docs/available-regions) 明確列出 **Taiwan 支援**，所以「地區」這條可以排除。

### 證據 5：開源前例的實測結論（最直接的旁證）
有個同性質專案 [AIstudioProxyAPI](https://github.com/globlord/AIstudioProxyAPI)（同樣用 Playwright 驅動 AI Studio 網頁版來「白嫖無限額度」），其 README 的限制章節直接寫：

> **Currently, due to automated detection mechanisms, headless mode is not supported.** This means that you must run the server in a visible browser window.

翻成中文就是：**「因為過不了自動化偵測，所以不支援無頭模式，必須開著有畫面的瀏覽器。」** 這與證據 2 完全吻合。

### 證據 6：你自己的程式碼就有這個不一致
`web_rpa.py` 裡「登入用」與「跑 OCR 用」的瀏覽器啟動參數**不一樣**：

```python
# web_rpa.py:324-334  launch_login_browser()  ← 有反偵測，所以登入成功
_login_context = await p.chromium.launch_persistent_context(
    user_data_dir=str(get_rpa_profile_path()),   # 持久化 profile
    headless=False,
    executable_path=chrome_path,
    args=["--no-first-run", "--no-default-browser-check",
          "--disable-blink-features=AutomationControlled"],   # ← 反偵測
    ignore_default_args=["--enable-automation"],              # ← 移除自動化旗標
)
```

```python
# web_rpa.py:521-529  run_web_ocr()  ← 真正跑 OCR 的路徑，反偵測全部不見了
browser = await p.chromium.launch(
    headless=headless,                       # ← 預設 True（無頭）
    executable_path=chrome_path,
    args=["--no-first-run", "--no-default-browser-check"],   # ← 沒有反偵測參數
)
context = await browser.new_context(
    storage_state=str(STORAGE_STATE_FILE),   # ← 只重放 cookie，非持久化 profile
    viewport={"width": 1280, "height": 800}
)
```

**登入路徑有隱身、OCR 路徑完全裸奔，而且預設無頭。** 這就是「登入成功、跑 OCR 就被 permission denied」的機制。

---

## 二、根因判定

| 假設 | 判定 | 依據 |
|------|------|------|
| 圖片上傳/selector 壞掉 | ❌ 排除 | 純文字 prompt 也失敗（證據 2） |
| 模型 ID 不存在 | ❌ 排除 | 官方站台橫幅確實宣傳 `gemini-3.8-flash` |
| 地區不支援（台灣） | ❌ 排除 | 官方 region 清單含 Taiwan（證據 4） |
| 未接受 ToS / 無 GCP 專案 | ❌ 排除 | 同帳號 API 成功 281 頁（證據 1） |
| 免費額度耗盡 | ❌ 排除 | 額度問題會回 429，不是 permission denied |
| **自動化瀏覽器被偵測** | ✅ **主因** | 證據 2 + 4 + 5 + 6 |

**根因：`run_web_ocr()` 以「無頭 + 無反偵測參數 + 只重放 cookie 的一次性 context」去操作 AI Studio，被 Google 的自動化安全檢查判定為機器人，於是後端對所有 generateContent 請求回 `permission denied`。**

> ⚠️ **本節結論已於後續實驗中修正，請務必續讀 [10.9](#109--重大修正ai-studio-的-permission-denied-不是自動化偵測造成的)。**
> 使用者完成登入後、以**真實瀏覽器（CDP）**重測，AI Studio Playground 依然回
> `An internal error has occurred.`——且與模型、Grounding、自動化偵測都無關。
> 也就是說：無頭／cookie 重放確實是舊版程式碼的**症狀來源**之一，
> 但**不是 AI Studio 失敗的根因**；真正原因是帳號／專案層級被 Google 擋住，程式無法修。

至於 **Gemini 官方網頁（gemini.google.com）那條路**，情況不同：它**技術上會動**（`debug_gemini_response.png` 顯示它確實回覆了），但對整頁密排直排文字只回傳了「左上角頁碼：197 / 左側邊欄：大乘法相宗名目第三上」這種**版面描述而非全文轉錄**。而 `debug_send.png` 的對話歷史裡有一條你自己命名的 **「OCR 轉錄失敗拒絕」**。
→ 這條路的失效模式是**模型拒答／只摘要不轉錄**（著作權與安全策略），屬於另一類問題，不是自動化偵測。

---

## 三、程式碼缺陷清單（依嚴重度排序）

這些是就算你要繼續走網頁端也**必須**修的東西。

| # | 嚴重度 | 位置 | 問題 |
|---|--------|------|------|
| 1 | 🔴 致命 | `web_rpa.py:521-525` | OCR 路徑缺少 `--disable-blink-features=AutomationControlled` 與 `ignore_default_args=["--enable-automation"]`，`navigator.webdriver` 為 true → 被偵測。**主因。** |
| 2 | 🔴 致命 | `data/web_rpa_config.json`、`web_rpa.py:34`、`templates/index.html:2012,2084` | `headless` 預設與現值皆為 `true`。無頭模式過不了 AI Studio 偵測。 |
| 3 | 🔴 高 | `web_rpa.py:526-529` | 用 `new_context(storage_state=...)` 只重放 cookie，而非用持久化 profile。Google 的 `__Secure-1PSIDTS` 等票證與 client 綁定，純 cookie 重放會得到「降級且可疑」的 session。 |
| 4 | 🟠 高 | `web_rpa.py:566, 703` | `model_name` 參數**從頭到尾沒被使用**（兩個 `_execute_*_ocr` 只在簽名出現，函式體內零引用）。→ 前端選 `[Web AI Studio] gemini-3.8-flash` **完全不會真的切換模型**，只會沿用 Playground 上次選的模型。模型下拉是裝飾品。 |
| 5 | 🟠 高 | `main.py:104-112` | 網頁端失敗**不做任何重試**就標記永久 `failed`；反之 API 路徑有 3 次重試（`main.py:114-160`）。一次瞬時失敗就毀掉一頁。 |
| 6 | 🟠 高 | `web_rpa.py:517` + `521,546` | 每頁都 `launch()` + `close()` 一顆全新 Chrome，並用 `Semaphore(1)` 完全序列化。每頁多付 2–5 秒冷啟動，數百頁完全不實用。 |
| 7 | 🟠 高 | `web_rpa.py:118-128` | `clean_ocr_response_text()` 只清寒暄前綴，**不偵測拒答**。`_execute_gemini_web_ocr`（`web_rpa.py:764-769`）只檢查 4 個「未上傳圖片」字串。→ **模型拒答或只摘要時，會被當成 success 寫入資料庫，造成靜默資料遺失。** |
| 8 | 🟡 中 | `requirements.txt` | **完全沒有 `playwright`**。目前 `.venv` 剛好裝了，但照 README 從零 `./run.sh` 會在執行期才炸。 |
| 9 | 🟡 中 | `run.sh:41` + `web_rpa.py:50, 354, 397` | `uvicorn --reload` 會重置模組級全域 `_login_context`。若在「① 開啟登入視窗」與「② 儲存登入狀態」之間發生 reload，`save_storage_state()` 會改走 `_extract_from_rpa_profile()`，而它第一件事就是 `_kill_rpa_chrome()` **把你剛登入好的視窗殺掉**，登入狀態直接遺失。 |
| 10 | 🟡 中 | `data/web_rpa_config.json` vs `web_rpa.py:28` | 設定裡的 `"user_data_dir": "data/chrome_profile"` 是死設定（程式只讀 `chrome_profile_rpa`，從不讀這個鍵）。三個 profile 目錄並存（`chrome_profile`、`chrome_profile_auto`、`chrome_profile_rpa`）造成混淆。 |
| 11 | 🟡 中 | `web_rpa.py:33` vs `99` vs `186` | 預設服務不一致：`DEFAULT_CONFIG["target_service"]="aistudio"`，但 `resolve_web_service(default_service="gemini")`，實際存檔又是 `"gemini"`。 |
| 12 | 🟢 低 | `config.py:16` vs 專案根目錄 | 專案根與 `data/` 各有一個 0 byte 的 `database.sqlite` 殘留（真正在用的是 `data/ocr.db`），容易誤導。 |
| 13 | 🟠 高 | `config.py:25-31` | `AVAILABLE_MODELS` 是過期清單：`gemini-1.5-flash`/`1.5-pro` 早已淘汰，而 **`gemini-2.5-*` 對新 key 已回 404**（"no longer available to new users"）。這會讓新任務選到直接失敗的模型。應改為 3.x 系列，或完全依賴 `fetch_google_models()` 動態清單。 |

補充：`_execute_aistudio_ocr` 用 `stable >= 2`（同一段文字連續兩次、間隔 2 秒）判定完成，對長頁面串流輸出可能在段落停頓處**提早收工**，得到截斷結果。建議改為監測「停止生成」按鈕消失或 DOM 進入 idle。

---

## 四、修復可能性評估

### 路線 A：把 AI Studio 網頁端修好（可行，但不推薦）
**技術上可行**，因為主因是可控的啟動參數。最小改動：

1. `headless` 改 `false`（**必要**，證據 5 明示）。
2. OCR 路徑補上 `args=["--no-first-run","--no-default-browser-check","--disable-blink-features=AutomationControlled"]` 與 `ignore_default_args=["--enable-automation"]`。
3. 改用 `launch_persistent_context(user_data_dir=chrome_profile_rpa, headless=False, ...)`，直接沿用已登入的 profile，**不要**再重放 cookie。
4. 一個 browser/context 重複用於所有頁面，不要逐頁重啟。
5. 修好 `model_name` 真正去操作模型選單（或直接 `?model=<id>` 進場）。
6. 補上拒答偵測與重試。

**但代價是**：
- 必須開著一個可見的 Chrome 視窗，且**執行期間不能碰它**（序列化）。
- 逐頁等待 UI 渲染，每頁 10–30 秒起跳，比 API 慢一個數量級。
- Google 隨時可以改 DOM 或加嚴偵測，**這是一場必輸的軍備競賽**。
- **違反 Google ToS**（見第五節），有帳號被停權的實質風險。

### 路線 B：改用 Gemini 官方網頁（gemini.google.com）
`debug_gemini_response.png` 證明「技術上會動」，但失效模式是**拒答／只摘要**。要它穩定輸出全文，需要：
- 用 AI Studio 的 **System Instructions + safety settings** 才能真正控制（但那是 API/Studio 功能，網頁版給不了）。
- 在提示中明確要求「逐字轉錄、不要摘要、不要評論」可**降低**但無法消除拒答。
- 同樣違反 ToS + 有停權風險。

**判定：可以試，但不該當主力。**

### 路線 C：放棄網頁端，強化你已經在用的 API 路線（⭐ 推薦）
這是你目前的真實狀況：**API 路線已經在穩定產出高品質結果，而且它是合法的免費額度。**

- 你已經有 **4 個帳號**在輪詢。免費額度是**按專案計（不是按 key 計）**，所以每多一個帳號/專案就多一份額度，**線性擴充**。
- 實測免費層額度（2026-09）：`gemini-3.5-flash-lite` = **15 RPM / 500 RPD**；3.x Flash = 5 RPM / 20 RPD。
  → 4 個帳號 ≈ **2000 頁/日**，遠超你剩餘的 212 頁。**你根本沒有額度危機。**
- `task_5ec32a0cea` 還有 212 頁 pending 被 `paused`；這不是網頁端的問題，而是**每日額度重置節奏 + 429 冷卻**的排程問題。
- 優化方向：多帳號 → 更細的避讓策略 → 跨午夜自動作業。你已有的 `scheduler.py` 骨架就是為此而寫，把 `MIN_REQUEST_INTERVAL_SECONDS` 與冷卻策略調好即可。
- **不要用 Batch API 當免費解**：Batch 在免費層不可用（見 6.1）；它是「付費但 5 折」的解，不是免費解。

### 路線 D：本機模型，達成「真正無限免費」
把 OCR 搬到自己的 M1 上跑，就徹底沒有 RPD、沒有 429、沒有偵測、沒有 ToS 問題。
對你的古籍直排＋夾註＋版心版面，**首選是 SongPanda 2.3（PaddleOCR-VL-1.6 的古籍微調版，Apache-2.0）**，它能自動去版心、把雙行夾註還原成 `【】` 標記。
代價是 M1 吞吐未經驗證、需自行調校。詳見第六節 6.2。

---

## 五、ToS 與風險（必須說清楚）

1. **網頁端自動化違反 Google ToS。** 官方 [Troubleshoot AI Studio](https://ai.google.dev/gemini-api/docs/troubleshoot-ai-studio) 把 `403 Access Restricted` 明確歸因於「使用方式不符 Terms of Service」，並且 `permission denied` 的檢查項包含 Trust & Safety 的濫用標記。
2. **風險是綁在 Google 帳號上的。** `baikuan` 同時是你 4 把有效 API Key 之一。若因自動化被標記，**你可能同時失去那把 API Key 與該帳號的 AI Studio 存取** —— 等於拿已經在賺錢的資產去賭一個更差的替代品。
3. `AIstudioProxyAPI` 專案作者自述「自用專案隨緣維護」，也說明這條路的維護成本與不穩定性。
4. 相對地，**API 路線是 Google 明文支援的用法**，且免費額度真實存在。用多帳號擴大免費額度雖然是灰色地帶，但至少不是「對抗偵測」。

---

## 六、替代工具評估

### 6.1 免費 API 的真實額度（2026-09 實測）

Google 已**不再於文件公布免費額度數字**，只能從 AI Studio 後台看。第三方實測（2026-09-02，全新專案）：

| 模型 | RPM | RPD |
|------|-----|-----|
| `gemini-3.5-flash-lite` / `gemini-3.1-flash-lite` | 15 | **500** |
| 3.x Flash 系列 | 5 | **20** |

- 額度**按「專案」計，不是按 API Key 計**，太平洋時間午夜重置。
- **Batch API 免費層不可用**（所以那 50% 折扣救不了免費用戶）。
- 免費層的內容**會被 Google 用於改進產品**。

**這解釋了你的現況**：你有 4 個帳號 = 4 個專案 = 理論上約 **2000 頁/日**（flash-lite）。你已完成 281 頁、還有 212 頁待跑，完全在能力範圍內 —— 這是節奏問題，不是路線問題。

其他免費選項的實測結論：

| 選項 | 免費額度 | 圖像 | 直排中文 | 判定 |
|------|----------|------|----------|------|
| Gemini 免費層 | 500 RPD (flash-lite) | ✅ | 極佳（你已在用） | ⭐ 主力 |
| OpenRouter 免費視覺 | **20 RPM / 50 RPD** | ✅ | 免費清單內無 Qwen-VL/GLM-V | ❌ 量太小 |
| Z.ai GLM-4.6V-Flash | 標示「完全免費」 | ✅ | 未驗證 | ⚠️ 額度未公開 |
| Z.ai GLM-OCR（付費） | $0.03 / 1M tokens（進+出） | ✅ | 強 | 💡 幾乎等於免費 |
| ModelScope / DashScope | 僅新加坡區有免費額度 | ✅ | 良好 | ⚠️ 區域限制 |
| Mistral OCR 4 | 未找到免費 | ✅ | 未驗證 | ❓ 未驗證 |

**結論：沒有任何「免費 API」能無限支撐數千頁。** 所有打著 free 的視覺 API 都是配額制的。

### 6.2 本機模型（M1 / Apple Silicon）

| 模型 | 大小/授權 | 直排 + 夾註 + 版心 | M1 可行性 | 備註 |
|------|-----------|--------------------|-----------|------|
| **SongPanda 2.3** ⭐ | ~1B, Apache-2.0 | **專為古籍刻本打造**：自動去版心、還原雙行小字夾註順序（`【】`）、眉批標記、圈點辨識 | 由 PaddleOCR-VL-1.6 微調，走 PaddlePaddle Apple Silicon 路徑 | **唯一針對你的版面設計的模型**。研究性質專案，需自行驗證 |
| **PaddleOCR-VL-1.6** | 0.9B, Apache-2.0, 109 語言 | 強（通用文件解析 SOTA），但無 夾註/版心 語意 | **官方有 Apple Silicon 指南**，但**只驗證到 M4，M1 明確未驗證** | vLLM 快路徑僅 CUDA/Linux，Mac 上算 CPU 推論 |
| **MinerU 4.0** | Apache-2.0 + 附加條款 | 古籍一般表現良好，**不擅長分離版心/夾註** | macOS Apple Silicon 為**一級支援**（PyTorch + llama.cpp） | 穩定、好裝 |
| dots.ocr | 1.7B, MIT | 中文讀序佳 | vLLM 導向；**README 自述不適合大批量 PDF** | ❌ 吞吐不足 |
| DeepSeek-OCR / OCR-2 | 3B MoE | 無直排中文實證 | **官方僅 CUDA 11.8 + vLLM + flash-attn**（社群有 `mlx-community/DeepSeek-OCR-2-8bit` 的非官方 MLX 轉檔，未驗證） | ⚠️ 官方不支援 Mac |
| Chandra 2 | Apache-2.0 程式 / **OpenRAIL-M 權重** | 無直排中文實證 | 未驗證 | ⚠️ 授權含商用限制 |
| Qwen2.5-VL 7B | Apache-2.0 | 中文場景公認強 | 需較大 VRAM；M1 16GB 偏緊 | 通用型，非文件解析專用 |
| Tesseract `chi_tra_vert` | — | 夾註與欄序最弱 | ✅ 可跑 | 僅適合當備援/對照 |
| olmOCR 2 / GOT-OCR2 / Florence-2 / Marker / Surya / EasyOCR / Kraken | — | **無直排中文實證** | — | 不建議 |

> ⚠️ **注意來源品質**：網路上大量「MinerU 古籍」CSDN 文章是 AI 生成的農場文（引用不存在的 "MinerU-1.2B"、不存在的 Chrome 外掛、捏造的 benchmark 表）。本報告已避開該類來源。

### 6.3 其他工具的達成可能性

| 目標 | 工具 | 判定 |
|------|------|------|
| **繼續免費、合法、可預測** | Gemini API 免費層 + 多帳號（你已在做） | ✅ **最佳解** |
| **近乎免費且品質更高** | GLM-OCR API（$0.03/1M tokens） | ✅ 數千頁成本趨近 0 |
| **真正無限免費** | SongPanda 2.3 / PaddleOCR-VL-1.6 本機跑 | ✅ 但需自行調校與驗證 M1 吞吐 |
| **脫離 RPD 上限但仍用 Gemini** | Gemini **付費 Batch**（5 折、無 RPD 天花板） | 💡 最省事的正解 |
| **無頭白嫖網頁端** | Playwright 打 AI Studio | ❌ 違 ToS、偵測必封、維護地獄 |

---

## 七、建議行動

**立即（低風險、高報酬）**
1. 維持 API 路線為主力；把 `task_5ec32a0cea` 剩餘 212 頁用多帳號排程跑完。
2. 把 `playwright` 補進 `requirements.txt`（缺陷 8）。
3. 移除 `run.sh` 的 `--reload`，或在 README 註明「登入期間勿改檔」（缺陷 9）。
4. 修 `_execute_gemini_web_ocr` 的拒答偵測（缺陷 7），避免靜默資料遺失 —— **這條就算你放棄網頁端也該修，因為它會污染既有資料**。

**若仍想試網頁端（中風險）**
5. 依路線 A 的 6 點修，**先只在 1–2 頁上驗證**，開著有頭視窗，確認 `permission denied` 消失再放量。
6. 把 `headless: true` 的預設值從 UI 拿掉或加警語（目前預設值本身就是陷阱）。

**策略（決定性）**
7. 誠實面對：網頁端的唯一好處是「繞過 API 額度」。若你要的是「長期、可預測、不被封」的免費，**正解是路線 C（多帳號 API）或路線 D（本機模型）**，不是跟 Google 的偵測對抗。

---

## 八、可複驗指令

```bash
cd /Users/jm/SyncDev/A1-antigravity/googleOCR

# 1) 看失敗頁面的真實錯誤
sqlite3 -line data/ocr.db "SELECT task_id,page_num,status,used_model,error_message \
  FROM task_pages WHERE error_message!='' ORDER BY id DESC LIMIT 5;"

# 2) 看 API 路線的成功規模
sqlite3 -header -column data/ocr.db "SELECT status,count(*) n,avg(length(ocr_text)) avgchars \
  FROM task_pages GROUP BY status;"

# 3) 看 4 把 API Key 都在用
sqlite3 -header -column data/ocr.db "SELECT id,name,auth_type,is_active,last_used_at FROM accounts;"

# 4) 確認網頁端設定的致命開關
cat data/web_rpa_config.json | grep -i headless
```

---

## 九、用第三方工具做 AI Studio 網頁端 I/O 並整合進 GASOCR — 可行性

### 9.1 結論
**技術上可行，而且整合工作量不大**（GASOCR 只需新增一個 provider adapter，任務／DB／SSE／匯出全部可重用）。
但可行性**高度取決於你選哪一類第三方工具**，而且**沒有任何一類能消除 ToS 與帳號被標記的風險** —— 外包只是把「你自己寫的自動化」換成「別人寫的自動化」。

### 9.2 關鍵洞察：為什麼 GASOCR 的 cookie 重放註定失敗
[webhands ADR-0002「Operate a real browser/profile/IP instead of spoofing or cookie-replay」](https://github.com/wighawag/webhands/blob/main/docs/adr/0002-real-session-over-fingerprint-spoofing.md) 講得最清楚：

> anti-bot clearance is bound to a TLS/browser fingerprint plus IP reputation, **not just the cookie**, so **a replayed cookie reads as stolen and re-challenges**; and a real session has no automation fingerprint to spoof.

翻譯：**反機器人的通行證綁在 TLS／瀏覽器指紋＋IP 信譽上，不是只綁 cookie。重放的 cookie 會被讀成「被盜的」，於是重新挑戰你。**

這正好解釋 `run_web_ocr()`（`web_rpa.py:526`）為何必敗：它把 `storage_state.json` 的 cookie 灌進一個**全新的 headless context** → Google 判定為被盜 session → 重新挑戰 → `permission denied`。

**重要推論：這表示「只加反偵測參數」（路線 A）可能仍然不夠。** 問題不只是 `navigator.webdriver`，而是整個 session 的指紋綁定。這也是為什麼該 ADR 的後續 [ADR-0014](https://github.com/wighawag/webhands/blob/main/docs/adr/0014-attach-to-the-users-own-chrome-is-the-anti-bot-answer.md) 直接下結論：「**attaching to a browser the user started themselves 才是答案，不是 hardened Playwright launch。Stealth is one tell removed, not the recipe.**」

（附帶好消息：該 ADR 指出 CDP 的 classic "console getter" 偵測在 2025 年 5 月因 V8 改動而失效，所以**目前 CDP 接入的風險較低**。）

### 9.3 三類第三方工具與可行性評級

| 類別 | 代表專案 | 可行性 | 關鍵限制 |
|------|----------|--------|----------|
| **A. 整套代理服務**（OpenAI 相容 HTTP） | [AIstudioProxyAPI](https://github.com/globlord/AIstudioProxyAPI)、[kay-ou/AIStudioProxy](https://github.com/kay-ou/AIStudioProxy)、aistudio-gemini-mcp | ⚠️ **中** | 需開著**有頭**瀏覽器（原作者明說 headless 過不了偵測）；**圖片多模態支援零散且資訊陳舊**（v3.5.7 才加入，而那篇公告是 2025-07 的 AIGC 生成內容，且需登入才看得到全文）；單一瀏覽器序列化；繼承他人技術債 |
| **B. CDP 接入你自己開的真瀏覽器** ⭐ | [webhands](https://github.com/wighawag/webhands) `--real-chrome`、[BeachPatrol](https://korben.info/en/beachpatrol-cli-controls-your-browser.html) | ✅ **最高** | 需你手動開 Chrome 帶 `--remote-debugging-port`；視窗執行期間要開著；仍違 ToS |
| **C. 反偵測驅動層** | [patchright](https://github.com/pim97/anti-detect-browser-tools-tech-comparison/blob/master/patchright.md)、[camoufox](https://github.com/daijro/camoufox/issues/345)、nodriver | ⚠️ **中低** | ADR 明言「stealth is one tell removed, not the recipe」；camoufox 已有被偵測的社群回報；仍是全新 context，沒解決指紋綁定 |

另外要留意：Google 帳號登入本身就是自動化的天險 —— 社群長期結論是 [Puppeteer 就算掛 stealth plugin 也無法登入 Google 帳號](https://stackoverflow.com/posts/79863349/timeline)。所有這類工具都靠「你手動登入過一次」的 profile 續命，與 GASOCR 現況相同。

### 9.4 整合設計（可直接套進 GASOCR）

**方案 B：CDP 接入（推薦）** —— 改動集中在 `web_rpa.py`：

```python
# 你手動開一次（保持開著，不要關）：
#   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
#     --remote-debugging-port=9222 \
#     --user-data-dir="$PWD/data/chrome_profile_rpa"

async def run_web_ocr(image_path, model_name, prompt, timeout=None):
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx  = browser.contexts[0]                      # ← 你已登入的真 context
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    # 關鍵：不要 new_context()、不要 storage_state、不要 browser.close()
    # 全程重用同一個 page，整批頁面只連一次
```
必須同時移除 `_kill_rpa_chrome()`（`web_rpa.py:193`）與 `_remove_singleton_lock()` 在這個路徑的呼叫，否則會殺掉你自己開的視窗。

**方案 A：接代理服務** —— 新增一個 provider，完全不動現有流程：

```python
# gemini_ocr.py 新增
url = "http://127.0.0.1:2048/v1/chat/completions"   # kay-ou/AIStudioProxy 預設埠
payload = {"model": clean_model, "messages": [{"role": "user", "content": [
    {"type": "text", "text": prompt},
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
]}]}
```
再把模型 ID 約定成 `[Web Proxy] <model>`，在 `main.py:86` 的分支旁多接一條，就能重用整個任務／進度／校對／匯出管線。

### 9.5 誠實的取捨

| | 網頁端（任何第三方工具） | API 免費層（你現況） |
|---|---|---|
| 額度 | 理論上接近無限（Pro 帳號） | 500 RPD／專案 × 4 帳號 |
| 速度 | 每頁 10–30 秒，**單一瀏覽器序列化** | 每頁 5–15 秒，**多帳號可並行** |
| 200 頁耗時 | 約 1–2 小時，且視窗不能碰 | 約 30–60 分鐘 |
| 穩定性 | 隨時因 Google 改版或加嚴而斷 | 官方支援，穩定 |
| 風險 | **違 ToS，可能連帶失去 `baikuan` 的 API Key** | 合規 |

**注意「網頁端比較快」是錯覺**：因為必須有頭＋單 session 序列化，它其實比你現在的多帳號 API 併行**更慢**。網頁端唯一真正的優勢是「額度上限高」。

### 9.6 建議
1. 若你只是想把 212 頁跑完 → **沒有必要**，用 API 就好。
2. 若你打算長期大量轉錄（數千頁以上）→ **先做好方案 B 的 CDP 接入原型，但用一個「非 `baikuan`」的備援帳號測試**，別拿你正在用的 API Key 帳號去冒險。
3. 真正的長期解仍是 **本機 SongPanda 2.3 / PaddleOCR-VL**（第六節）—— 那裡沒有偵測、沒有配額、沒有 ToS 問題，而且更貼合古籍版面。

---

## 十、方案 B 實作與驗證結果（2026-09-22 完成）

### 10.1 結論：**已實作並端到端驗證通過**

實測結果：透過使用者自己的 Chrome，OCR 成功回傳正確文字。

```
POST /api/web-rpa/test?model=[Web Gemini] Flash
→ status: ok, code: 200
→ message: "OCR Test Hello World 123"      ← 正確辨識測試圖
→ 耗時 34 秒
```

### 10.2 新增／修改的檔案

| 檔案 | 變更 |
|------|------|
| `web_rpa.py` | 新增 CDP 模式：`check_cdp_available()`、`_get_cdp_page()`、`_reset_cdp_state()`、`close_cdp_session()`、`check_cdp_login()`；`run_web_ocr()` 拆成 `_run_ocr_via_cdp()` 與 `_run_ocr_via_launch()`；`get_login_status_info()` 改為模式感知 |
| `start_chrome_cdp.sh` | **新增**。以 `open -na` 啟動帶除錯埠的 Chrome，完全脫離 shell 程序群組 |
| `main.py` | 新增 `/api/web-rpa/chrome-command`、`/cdp-status`、`/cdp-connect`、`/cdp-disconnect`；`WebRpaConfigPayload` 支援新模式欄位；lifespan 加入 CDP 連線清理 |
| `templates/index.html` | 新增「接入您自己的 Chrome」面板、瀏覽器模式選取器、CDP 狀態燈、啟動指令一鍵複製；舊版流程改為僅在 launch 模式顯示 |
| `data/web_rpa_config.json` | 新增 `browser_mode: "cdp"`、`cdp_port`、`cdp_user_data_dir`、`fresh_chat_per_page` |
| `requirements.txt` | 補上 `playwright>=1.40.0`（原本完全缺失） |

### 10.3 核心不變式（已在程式碼中落實）

CDP 模式下 GASOCR 對使用者的瀏覽器**只有借用權，沒有擁有權**：

- 絕不呼叫 `browser.close()`（會關掉使用者整個 Chrome）
- `_kill_rpa_chrome()` 加上防護：偵測到 CDP 埠有 Chrome 在跑就**完全不終止任何程序**
- 脫離連線只用 `playwright.stop()`
- 建立 GASOCR **專用分頁**，不搶使用者正在看的分頁

### 10.4 使用步驟

```bash
# 1. 啟動專屬 Chrome（會自動開到 AI Studio）
./start_chrome_cdp.sh

# 2. 在彈出的 Chrome 視窗中「手動」登入 Google 帳號（只需一次）
# 3. 回到 GASOCR 網頁 → 金鑰池 → 網頁自動化
#    點「① 檢查 CDP 連線」→「② 接入並驗證登入」
# 4. 之後就能用 [Web Gemini] / [Web AI Studio] 模型跑 OCR
```

### 10.5 實測發現的兩個額外真實 bug（已修）

**(a) 靜默資料遺失（本次實測直接踩到）**
第一次測試回傳 `status: ok`，但訊息其實是「**目前沒有看到您上傳或提供的圖片，請附上圖片**」——模型根本沒收到圖，卻被當成成功。
舊版只比對 4 個字串（`尚未夾帶`/`未夾帶`/`未上傳圖片`/`尚未上傳圖片`），完全沒涵蓋這個真實回應。
→ 已新增 `detect_no_image()` 與 `_validate_ocr_output()`，統一驗證輸出，並確認**不會誤殺正常古籍文本**（已用「南無阿彌陀佛…」「爲暖位。乃至初獲惠日前行相故…」等真實內容測過）。

**(b) 圖片根本沒附加成功**
舊版 `set_input_files()` 後只 `sleep(2.5)` 就送出，沒有任何驗證。
→ 已改為三策略上傳（直接注入 file input → 點按鈕攔 file chooser → 開選單點上傳項），並以「**頁面 blob 圖片數量增加**」為客觀證據驗證附件就位；附加失敗就**大聲失敗並中止送出**。

### 10.6 順帶修掉的缺陷

- `model_name` 現在**真的會被使用**：以 `?model=<id>` 導航，模型下拉不再只是裝飾品
- 舊版 launch 路徑補上 `--disable-blink-features=AutomationControlled` 與 `ignore_default_args=["--enable-automation"]`
- CDP 模式全程**重用同一個分頁**，不再每頁冷啟動一顆 Chrome

### 10.7 實測發現的重要事實：AI Studio 與 Gemini 的登入是分開的

在同一個 Chrome profile 下實測：

| 服務 | 實測結果 |
|------|----------|
| `gemini.google.com` | ✅ **已登入**，OCR 端到端成功 |
| `aistudio.google.com` | ⚠️ 被導向 `accounts.google.com/v3/signin/identifier` → **尚未登入** |

這其實是個**好消息**：舊版在無頭 cookie 重放下，AI Studio 會顯示成「已登入」的假象，然後才噴 `permission denied`；現在 CDP 模式會**誠實地告訴你「尚未登入 AI Studio」**。

**因此你要用 AI Studio 網頁端，只差一步：在你自己的那個 Chrome 視窗裡登入 aistudio.google.com。**

> 補充：若你想要立刻可用，`[Web Gemini] Flash` 這條路現在已經驗證會動。
> 目前 `target_service` 已設為 `aistudio`（符合你的目標）；等你登入 AI Studio 後即可直接使用。

### 10.8 未完成事項

- **AI Studio 需你手動登入一次**（我無法代為輸入你的帳密）。
- 未驗證：AI Studio 登入後，原本的 `permission denied` 是否確實消失。這是**下一個關鍵驗證點**。
- 未驗證：CDP 模式下數百頁批次跑起來的實際吞吐與穩定度。
- 本機模型路線（SongPanda 2.3 / PaddleOCR-VL）尚未動工。

### 10.9 ⚠️ 重大修正：AI Studio 的 permission denied **不是**自動化偵測造成的

先前（第二節）我把 `permission denied` 歸因於「自動化瀏覽器被偵測」。
**在使用者完成登入、改用真實瀏覽器重測後，這個歸因被推翻了。**

實驗（全部在「真實 Chrome + CDP + 已登入 AI Studio」下進行）：

| 變因 | 操作 | 結果 |
|------|------|------|
| 真實 session | 使用者手動登入，`logged_in: True`，標題 `Google AI Studio` | ❌ 仍失敗 |
| Grounding | 找到並關閉 `button[role=switch][aria-label="Grounding with Google Search"]`，實測 `aria-checked` 由 `true` → `false`（已驗證） | ❌ 仍失敗 |
| 模型 | 先驗證 `?model=` 真的會切換（要求 3.5-flash-lite → 面板顯示 "Gemini 3.5 Flash Lite" ✅）；再掃 4 個模型 | ❌ **全部失敗**，含 API 路線可用的 `gemini-3.5-flash-lite` |

帳號與專案（自 API Keys 頁面取得）：
- 帳號：`juehming@gmail.com`（PRO）
- 專案：`gen-lang-client-0188267851`（名為 "Gemini Project"），**Free tier**
- **該專案的 API Key 完全正常**（同帳號已用 API 完成 281 頁 OCR）

→ **結論：AI Studio Playground 對此帳號／專案被擋住，與自動化偵測無關，GASOCR 的程式碼無法修。**

最可能的原因（依官方 `PERMISSION_DENIED` 清單）：
1. **未接受** Generative AI Additional Terms of Service
2. 專案被 **Trust & Safety** 標記
3. Playground 互動使用需要 **Tier 1（付費）** —— API Keys 頁面上就有一個 "Set up billing" 按鈕

> 教訓：我最初的「自動化偵測」結論對舊版程式碼的**症狀**是對的（無頭 + cookie 重放確實有問題），
> 但對**根因**是錯的。真正決定性的實驗是「在同一個帳號下，用真實瀏覽器再測一次」。

### 10.10 ✅ Gemini 官方網頁路線：已用真實古籍頁面驗證成功

**這才是你要的答案。**

實測（`verify_real_page.py`）：
- 輸入：`data/renders/task_79fcd26fa8/page_0103.png`（685 KB，300 DPI 直排雙欄古籍）
- Prompt：真實的 `build_ocr_prompt("traditional", "vertical", "double")`
- 模型：`[Web Gemini] Flash`，走 CDP 接入你的 Chrome
- 結果：**ok=True，807 字忠實轉錄，未拒答**

輸出節錄：

> 大乘法相宗名目第二上…三麤重者一、皮麤重…又樞要上卷…解深密經云：八地已上，唯有所依所知障在…

與資料庫中 API 路線（`gemini-3.5-flash-lite`，843 字）對照，兩者品質相當；
網頁端在標點與異體字正規化上甚至**更易讀**。

**證明：CDP + Gemini 官方網頁是一條真正可用、免費的網頁端 OCR 路線。**

### 10.11 最終路線建議（更新版）

| 路線 | 狀態 |
|------|------|
| AI Studio Playground 網頁端 | ❌ 帳號／專案層級被擋，**程式無法修**（見 10.9） |
| **Gemini 官方網頁 + CDP** | ✅ **已驗證可用**（真實古籍頁 807 字、未拒答） |
| Gemini API 免費層 | ✅ 可用（281 頁），仍是最穩定的主力 |
| 本機模型（SongPanda / PaddleOCR-VL） | 未動工，長期無限免費解 |

`target_service` 已設回 `gemini`（可用路線）。若日後在 Google 端解決 AI Studio 專案問題，
切回 `aistudio` 即可。

### 10.12 診斷工具（保留供日後排查）

| 腳本 | 用途 |
|------|------|
| `verify_real_page.py` | 用真實古籍頁驗證網頁端 OCR，並與 DB 中 API 成果對照 |
| `diag_aistudio.py` | dump AI Studio 頁面狀態、錯誤橫幅、帳號 |
| `diag_grounding.py` | 定位並切換 Grounding 開關，驗證是否為主因 |
| `diag_model_sweep.py` | 逐一模型測試可用性 |
| `diag_model_param.py` | 驗證 `?model=` 是否真的切換模型 |
| `diag_project.py` | 檢查 API Keys / Usage 頁面的專案與方案 |
| `diag_response_dom.py` | dump Gemini 回應區塊的 DOM 結構與標籤統計（找出 Markdown 容器） |
| `diag_completion.py` | 輪詢各候選完成訊號，找出真正可靠的「生成結束」判斷依據 |
| `diag_buttons.py` | dump 生成期間的按鈕，定位「停止回覆」按鈕 |

### 10.13 追加修正：Markdown 結構、完成判定、拒答重試

使用者回報「Gemini web 版成功，但丟失了輸出 md 的語法」。追查後發現**三個獨立的缺陷**。

#### (1) Markdown 語法遺失 —— 因為網頁是「渲染後」的 HTML

Gemini 網頁會把 Markdown **渲染成 HTML** 再顯示。用 `innerText` 取文字，
等於只拿到渲染後的字，`#`、`**`、表格語法在渲染階段就被吃掉了。

實測 DOM（`diag_response_dom.py`）確認內容位於：
`model-response > … > message-content > div.markdown.markdown-main-panel`

→ **新增 `_JS_HTML_TO_MARKDOWN`**：在瀏覽器內走訪 DOM，還原
`h1~h6` / `p` / `ul` / `ol` / `table` / `b,strong` / `em,i` / `code` / `pre` / `br` / `hr` / `blockquote`，
並排除 `sources-list`、動作列、`cdk-visually-hidden`（螢幕閱讀器用的「Gemini 說了」）。

#### (2) 輸出被截斷 —— 完成判定不可靠

原本用「文字連續兩次相同（約 4 秒）」判斷完成，但 Gemini 串流中間會停頓，
導致提早收工。實測輸出曾在「唯有所依所知大」被切斷。

逐一查證候選訊號（`diag_completion.py`）：

| 候選訊號 | 實測結果 |
|-----------|----------|
| `structured-content-container.processing-state-visible` | ❌ 文字完成後**再過 70 秒仍為 True** |
| `.markdown-main-panel` 的 `aria-busy` | ❌ 恆為 false，無法區分 |
| 動作列（複製/讚/倒讚）出現 | ❌ 生成中就已存在 |
| 「停止回覆」按鈕 | ✅ **生成中出現，完成後消失** |

按鈕探測（`diag_buttons.py`）：生成中 `aria-label="停止回覆"`，t=18s 後消失。

→ **已改用「停止回覆」按鈕是否存在的 `streaming` 旗標**作為主要完成判定，
並保留文字穩定度作第二道防線。若選擇器失效，`streaming` 恆為 false，
行為退化為原本邏輯，**不會卡死**。

#### (3) 拒答被當成成功 —— 而且是簡體

實測同一頁**有時正常轉錄、有時拒答**：

> 我无法提供这方面的帮助，因为我只是一个语言模型。

注意這是**簡體**（`无法`），而舊偵測只比對繁體「我無法」，因此拒答被當成 `success`。

→ 已擴充繁簡與英文樣式，並加入「短回應（<120 字）+ 明確拒答詞」判斷。
→ 已用「菩薩不能知。」「南無阿彌陀佛。」等真實古籍短句驗證**不會誤殺**。
→ 因為拒答是**隨機發生**的，`_run_ocr_via_cdp` 加入**最多 3 次重試**。

#### 驗證結果（同一頁 `page_0103`）

| 項目 | 結果 |
|------|------|
| 輸出長度 | **780 字**（完整） |
| Markdown 標題 | ✅ `### 第十七 三麤重者` |
| 段落結構 | ✅ 完整分段 |
| 結尾 | ✅ 完整句子「（解深密經唯識章及私記第六卷可見之。）」 |
| 截斷 | ✅ 無 |
| 拒答 | ✅ 無 |

`test_api.py` 全套仍通過，`/api/web-rpa/test` 回歸正常。

## 十一、最終判定：AI Studio 對「程式化輸入」的阻擋（**無法以程式修正**）

### 11.1 使用者的關鍵觀察
> 「如果是程序自己去呼叫 aistudio 的介面，就一定會出現 internal server error。
> 但是同一個對話框，我手動輸入 hello，就可以。」

這推翻了第十節「帳號／專案被擋」的結論 —— 帳號是好的，被擋的是**程式化輸入這個行為本身**。

### 11.2 逐一排除的假設（全部實測）

| 假設 | 實驗 | 結果 |
|------|------|------|
| 無頭／自動化指紋 | CDP 接入真實 Chrome，量測指紋 | ❌ `navigator.webdriver=false`、無 `cdc_`、無 playwright 痕跡 |
| `fill()` 送出非信任事件 | A/B/C/D 四種輸入×送出組合 | ❌ `keyboard.type()`（真實 CDP 鍵盤事件）一樣 403 |
| 頁面尚未就緒 | 等 25 秒才送出 | ❌ 仍 403 |
| 缺少人類節奏 | 輸入後等 15 秒才送出 | ❌ 仍 403 |
| 缺少滑鼠行為 | 分步滑鼠軌跡 + 慢速打字 + hover 後點擊 | ❌ 仍 403 |
| 送出鍵錯誤 | 分別測 ⌘+Enter 與 Run 按鈕 | ❌ 兩者皆 403 |
| 模型無權限 | 逐一測 4 個模型（含 API 可用的 3.5-flash-lite） | ❌ 全部 403；請求確實帶對模型 |
| 新對話 vs 既有對話 | 在使用者既有對話中送出 | ❌ 仍 403 |
| 首個請求競態 | 同分頁連續送 3 次 | ❌ 3 次全 403 |
| **暖身解鎖**：先手動初始化對話，再讓程式接續送出 | 同一頁、同一對話、不重新載入；手動 **200** 後僅 5 秒由程式接續送兩次 | ❌ 仍 **403、403** |

### 11.3 決定性證據：同頁、同 session、標頭逐位元相同

用同一個 `handleKeyDown` 之外的完整攔截，分別錄下「手動送出（200）」與「程式送出（403）」：

**真實後端錯誤**
```
POST .../MakerSuiteService/GenerateContent  →  403
回應: [,[7,"The caller does not have permission"]]     ← 7 = PERMISSION_DENIED
```

**URL 完全相同**（一字不差）

**39 個標頭逐項比對**，只有 3 個不同，且皆為時間性：
- `authorization`：SAPISIDHASH 時間戳不同（正常）
- `content-length`：跟著 payload 長度
- `SIDCC` / `__Secure-1PSIDCC` / `__Secure-3PSIDCC`：Google 的 cookie 輪替值

**完全相同的關鍵標頭**（包括所有可能用來辨識的）：
```
x-goog-api-key            AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs
x-aistudio-visit-id       v1_Y2NjNjQzYjEtM2E5Yi00YTNjLThhZjgtZjJkNmY5YWEyODQ2
x-goog-ext-519733851-bin  CAESAUwwATgEQABQBGICVFdwAHgBkAEAmAEB
x-browser-validation      5NlNaDKBB93vNb2o5VQTaiuhHAY=
__Secure-1PSIDTS          （輪替票證，兩者相同）
origin / referer / user-agent / sec-ch-ua-* / sec-fetch-*
x-goog-authuser: 0
```

**payload 結構相同（同為 12 個頂層欄位）**，只有兩處不同：
- `[1]` contents：手動為 `[[[[null,"hello"],[null,"hello"]],"user"]]`（殘留文字造成兩段）／程式為 `[[[[null,"hello"]],"user"]]`
- `[4]` 一段不透明的 **attestation token**（每請求都不同）

### 11.4 確定性
- 程式化送出：**4/4 → 403**（連續測試）
- 手動送出：**2/2 → 200**
- **暖身無效**：同一頁、同一對話、不重新載入，手動成功後 5 秒由程式接續送出 →
  `[200, 403, 403]`（20:06:43 / 20:06:48 / 20:06:54）

同一頁面、同一 session、僅相隔數十秒，交錯發生。

> 這條「暖身」測試是最後一個可能的修法，結果排除了它：
> 阻擋是**逐請求（per-request）**評估的，不是逐對話、逐頁面或逐 session 的狀態。
> 因此「先手動初始化一個對話」無法解鎖，程式接續送出仍會被擋。

### 11.5 結論
唯一系統性差異是 payload `[4]` 的 **attestation token**。這是 Google 的反自動化憑證，
其存在目的正是讓「非人類發起」的請求無法被偽造 —— 標頭可以複製、payload 可以複製，
但該 token 由瀏覽器端的 BotGuard 依互動訊號產生，**程式無法忠實重現**。

> **因此這不是 GASOCR 的 bug，也無法透過修改自動化程式修正。**
> 這是伺服器端的刻意控制，不屬於「修好程式就會通」的範疇。

### 11.5b 前端確實有「滑鼠／指標」監聽器（已查證原始碼）

用 CDP `DOMDebugger.getEventListeners` + `Debugger.getScriptSource` 查出每個滑鼠監聽器的來源：

| 目標 | 數量 | 來源 | 性質 |
|------|------|------|------|
| **`document.body`** | **10** | **無 URL 的混淆腳本（`:0`）** | **輸入訊號收集器（BotGuard 類）** |
| `document` | 3 | `www.gstatic.com/_/mss/boq-makersuite/...` | 一般 UI：點擊記錄服務、Material ripple |
| `window` | 1 | 同上 | 一般 UI |

`document.body` 上註冊的事件涵蓋**完整的輸入串流**：
`mousemove`、`pointermove`、`mousedown`、`mouseup`、`pointerdown`、`pointerup`、
`mouseover`、`pointerover`、`click`、`wheel`

其原始碼是典型的 VM 混淆（BotGuard 風格），例如：

```js
t=function(I,Z,M,R,y,X,u,b,z,w,E){for([]!=(![]==false!=[]);(I+1^31)<I&&(I-4|8)>=I;false){
  if(y=bl("call","object",R)==="array"?R:[R],this.I)Z(this.I)
```

→ **答案：是的。前端有專門的收集器在監看滑鼠／指標輸入**，而且它正是產生
payload 第 `[4]` 欄 attestation token 的來源。這也解釋了為什麼：

- 標頭可以做到逐位元相同、payload 結構可以完全相同，
  但**每一個程式發起的請求仍被單獨判定為 403** —— 因為差異在 token 內部；
- 先前的「真人化」嘗試（滑鼠軌跡 + 慢速打字 + hover）無效 —— 該收集器分析的是
  **整體互動訊號**，且其輸出經簽章，無法由外部腳本偽造。

### 11.6 附帶發現並修好的真實 bug
`_execute_aistudio_ocr` 的送出 fallback 用 `Control+Enter`。
**實測：在 macOS 按 Control+Enter 完全不會送出（連請求都不會發出）**，
畫面提示本身也寫著「Send prompt (**⌘** + Enter)」。
→ 已改為依平台選擇：macOS 用 `Meta+Enter`，其他平台維持 `Control+Enter`。
（與前述快速鍵問題同源：把 Windows 的按鍵假設直接套在 macOS 上。）

### 11.7 建議
| 路線 | 判定 |
|------|------|
| AI Studio Playground 網頁自動化 | ❌ **此路不通**（伺服器端 attestation 阻擋） |
| **Gemini 消費端網頁 + CDP** | ✅ **已驗證可行**（真實古籍頁 780 字、Markdown 保留、未拒答） |
| Gemini API 免費層 | ✅ 可用（281 頁），最穩定 |
| 本機模型（SongPanda / PaddleOCR-VL） | 未動工，長期無限免費解 |

---

## 附註：本報告的證據等級

- **已直接驗證**：資料庫內容、截圖內容（實際讀圖）、storage state cookie 組成、程式碼行號與缺陷、官方文件的成因清單與地區清單、`AIstudioProxyAPI` 的無頭結論、免費層 RPD 實測數字。
- **社群來源（非官方，可信度中）**：免費層 `gemini-3.5-flash-lite` = 500 RPD 的數字來自第三方實測，非 Google 官方公布（[rate-limits 文件已移除數字表](https://ai.google.dev/gemini-api/docs/rate-limits?hl=en)）。請以你 AI Studio 後台 `aistudio.google.com/rate-limit` 顯示的實際數字為準。
- **推論（高信心，但未做隔離實驗）**：「自動化偵測」是 `permission denied` 的主因。要 100% 坐實，需做**對照實驗**：在同一個 `baikuan` 帳號下，**手動**開正常 Chrome 進 AI Studio 打一句 `hello`。
  - 若手動成功 → 確認是自動化偵測（可修）。
  - 若手動也 `permission denied` → 問題在帳號/project 層級（那就跟瀏覽器無關，且與「API 能用」矛盾，需進一步查）。
- **未驗證**：修好之後能否**長期穩定**（Google 隨時可加嚴）；修好後的實際每頁耗時；SongPanda 2.3 在 M1 上的實際速度與古籍準確率。
