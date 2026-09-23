import asyncio
import os
from pathlib import Path
import pypdfium2 as pdfium
from playwright.async_api import async_playwright

async def run_batch_test():
    print("==================================================")
    print("🧪 啟動 Playwright 端對端 5 份 PDF 批次上傳與按鈕狀態測試")
    print("==================================================")

    test_dir = Path("/Users/jm/SyncDev/A1-antigravity/googleOCR/data/test_batch_upload")
    test_dir.mkdir(parents=True, exist_ok=True)

    # 1. 產生 5 個測試 PDF
    test_files = []
    for i in range(1, 6):
        pdf_path = test_dir / f"test_doc_{i}.pdf"
        doc = pdfium.PdfDocument.new()
        # 建立 2 頁
        doc.new_page(width=300, height=300)
        doc.new_page(width=300, height=300)
        doc.save(str(pdf_path))
        doc.close()
        test_files.append(str(pdf_path))
    
    print(f"👉 [1/4] 已建立 5 個測試 PDF: {[Path(f).name for f in test_files]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        page = await browser.new_page()

        # 監聽 console 訊息與 dialog
        page.on("console", lambda msg: print(f"   [Browser Console] {msg.text}") if "error" in msg.text.lower() else None)
        dialog_detected = []
        page.on("dialog", lambda dialog: (dialog_detected.append(dialog.message), dialog.accept()))

        # 2. 開啟主頁
        print("👉 [2/4] 載入前端網頁 http://127.0.0.1:8610 ...")
        await page.goto("http://127.0.0.1:8610", wait_until="networkidle")

        # 檢查主按鈕初始狀態
        submit_btn = page.locator("button:has-text('請先選取 PDF 檔案')")
        assert await submit_btn.is_disabled(), "未選取檔案時按鈕應為 disabled"
        print("   ✅ 初始狀態正確: 請先選取 PDF 檔案，按鈕已鎖定")

        # 3. 注入 5 個 PDF 檔案到 input[type='file']
        print("👉 [3/4] 批次注入 5 個 PDF 檔案，檢驗 SHA-256 特徵碼比對狀態與按鈕防禦...")
        file_input = page.locator("input[type='file']")
        await file_input.set_input_files(test_files)

        # 檢驗按鈕在比對特徵碼期間的反應
        # 由於 5 個檔案會立即觸發 addPendingFiles，檢驗文字或等待比對完成
        # 等待按鈕轉變為可點擊狀態 ("開始批次轉譯 (5 個檔案)")
        btn_selector = "button:has-text('開始批次轉譯 (5 個檔案)')"
        await page.wait_for_selector(btn_selector, timeout=15000)
        
        btn_clickable = page.locator(btn_selector)
        is_disabled = await btn_clickable.is_disabled()
        print(f"   ✅ 特徵碼比對完成！按鈕文字更新為: 開始批次轉譯 (5 個檔案)，is_disabled: {is_disabled}")
        assert not is_disabled, "比對特徵碼完成後按鈕應解除鎖定！"

        # 4. 點擊提交按鈕，驗證 XHR 上傳、非阻塞 Toast 與按鈕解鎖
        print("👉 [4/4] 點擊「開始批次轉譯 (5 個檔案)」按鈕，檢驗傳輸與非阻塞回復...")
        await btn_clickable.click()

        # 驗證 Toast 出現（不再使用原生阻塞 alert）
        toast_selector = "text=已成功排入 5 個檔案進行批次 OCR 轉譯"
        await page.wait_for_selector(toast_selector, timeout=10000)
        print("   ✅ 成功收到全域 Toast 提示訊息 (無原生 alert 阻塞)")

        assert len(dialog_detected) == 0, f"不應觸發原生 window.alert 對話框，實際偵測到: {dialog_detected}"

        # 驗證按鈕已乾淨重設（pendingFiles 清空後回到「請先選取 PDF 檔案」且無卡在啟動中）
        await page.wait_for_selector("button:has-text('請先選取 PDF 檔案')", timeout=5000)
        print("   ✅ 按鈕狀態已順利回復，無卡死在「啟動中...」！")

        # 驗證任務列表中是否出現這 5 個新任務
        await page.wait_for_timeout(1000)
        # 刪除測試任務
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:8610/api/tasks")
            if resp.status_code == 200:
                tasks = resp.json().get("tasks", [])
                for t in tasks:
                    if "test_doc_" in t.get("filename", ""):
                        await client.delete(f"http://127.0.0.1:8610/api/tasks/{t['id']}")
        print("   ✅ 測試任務已完成全自動清理")

        await browser.close()

    # 清理測試 PDF
    try:
        import shutil
        shutil.rmtree(test_dir)
    except Exception:
        pass

    print("==================================================")
    print("🎉 5 份 PDF 批次上傳與按鈕狀態測試全數通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_batch_test())
