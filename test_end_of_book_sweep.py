import asyncio
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock
import pypdfium2 as pdfium

os.environ["DATA_DIR"] = "/tmp/test_gasocr_sweep"
import config
config.DATA_DIR = Path("/tmp/test_gasocr_sweep")
config.RENDERS_DIR = config.DATA_DIR / "renders"
config.UPLOADS_DIR = config.DATA_DIR / "uploads"
config.DB_PATH = config.DATA_DIR / "ocr_system.db"
config.MIN_FREE_DISK_MB = 10
config.MIN_REQUEST_INTERVAL_SECONDS = 0.001
config.DEFAULT_COOLDOWN_SECONDS = 0.05

import database
import main

def create_dummy_pdf(path: Path, num_pages: int = 5):
    doc = pdfium.PdfDocument.new()
    for _ in range(num_pages):
        doc.new_page(width=200, height=200)
    doc.save(str(path))
    doc.close()

real_sleep = asyncio.sleep

async def fast_sleep(sec):
    # 將所有等待縮短為 0.001 秒，加速測試且避免無限遞迴
    await real_sleep(0.001)

async def run_tests():
    print("🚀 開始驗證每本書最後一頁完畢後的無結果頁面自動補檢複查 (End-of-Book Sweep)...")
    if config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    
    await database.init_db()
    
    # 建立測試帳號
    async with database.get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO accounts (id, name, auth_type, api_key, is_active, is_paid, cooldown_until, created_at)
            VALUES (77777, 'Test Sweep Account', 'api_key', 'AIzaSyTestSweep123', 1, 0, 0.0, 100.0)
        """)
        await db.commit()
    
    # 建立 5 頁測試 PDF
    pdf_file = config.UPLOADS_DIR / "test_book.pdf"
    create_dummy_pdf(pdf_file, 5)
    
    # ----------------------------------------------------
    # 測試情境 1：第 3 頁在第一輪模擬 500 失敗，整本書 1~5 頁初次跑完後，結尾自動補檢成功完成
    # ----------------------------------------------------
    print("\n👉 [1/2] 測試情境 1：全書跑完後，第 3 頁在結尾自動補檢複查中成功轉譯...")
    task_id_1 = "test_sweep_task_1"
    now = time.time()
    async with database.get_db() as db:
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, total_pages, status, created_at, updated_at, start_page, end_page, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id_1, "test_book.pdf", str(pdf_file), 5, "pending", now, now, 1, 5, "gemini-3.5-flash-lite"))
        await db.commit()

    first_pass_p5_reached = False
    sweep_called_for_p3 = False

    async def mock_call_gemini_ocr_case1(account, image_path, model, prompt):
        nonlocal first_pass_p5_reached, sweep_called_for_p3
        p_name = image_path.stem
        p_num = int(p_name.split("_")[-1])
        
        if p_num == 5 and not first_pass_p5_reached:
            first_pass_p5_reached = True
            return True, f"第 5 頁正常轉譯內文", 200

        if p_num == 3:
            if not first_pass_p5_reached:
                # 第一輪初次跑全書時第 3 頁失敗
                return False, "500 模擬伺服器暫時性異常", 500
            else:
                # 跑到全書第 5 頁之後的結尾自動補檢複查
                sweep_called_for_p3 = True
                return True, "第 3 頁在全書結尾自動補檢中成功轉譯之內文", 200

        return True, f"第 {p_num} 頁正常轉譯內文", 200

    with patch("asyncio.sleep", side_effect=fast_sleep):
        with patch("main.call_gemini_ocr", side_effect=mock_call_gemini_ocr_case1):
            await main.process_task_pipeline(task_id_1)

    t1 = await database.get_task(task_id_1)
    pages1 = await database.get_task_pages(task_id_1)
    
    assert sweep_called_for_p3, "預期在第 5 頁完成後，觸發結尾自動補檢並重新辨識第 3 頁"
    assert t1["status"] == "completed", f"任務狀態應為 completed，實際: {t1['status']}"
    assert t1["processed_pages"] == 5, f"完成頁數應為 5，實際: {t1['processed_pages']}"
    p3 = next(p for p in pages1 if p["page_num"] == 3)
    assert p3["status"] == "completed", f"第 3 頁狀態應為 completed，實際: {p3['status']}"
    assert "成功轉譯" in p3["ocr_text"]
    print("   ✅ 情境 1 通過：第 3 頁在全書第 5 頁跑完後，順利觸發自動補檢並成功完成！")

    # ----------------------------------------------------
    # 測試情境 2：第 3 頁持續配額耗盡/失敗，結尾補檢後仍無結果 -> 明確標記 failed，任務轉為 paused
    # ----------------------------------------------------
    print("\n👉 [2/2] 測試情境 2：第 3 頁持續失敗，結尾補檢後仍無結果 -> 頁面標記 failed，任務轉為 paused...")
    task_id_2 = "test_sweep_task_2"
    async with database.get_db() as db:
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, total_pages, status, created_at, updated_at, start_page, end_page, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id_2, "test_book.pdf", str(pdf_file), 5, "pending", now, now, 1, 5, "gemini-3.5-flash-lite"))
        await db.commit()

    case2_p3_calls = 0

    async def mock_call_gemini_ocr_case2(account, image_path, model, prompt):
        nonlocal case2_p3_calls
        p_name = image_path.stem
        p_num = int(p_name.split("_")[-1])
        if p_num == 3:
            case2_p3_calls += 1
            return False, "429 模擬配額完全耗盡", 429
        return True, f"第 {p_num} 頁正常轉譯內文", 200

    async def fast_report_rate_limited(acc_id, cooldown=0.01):
        await database.set_account_cooldown(acc_id, 0.01)

    with patch("asyncio.sleep", side_effect=fast_sleep):
        with patch.object(main.scheduler, "report_rate_limited", side_effect=fast_report_rate_limited):
            with patch("main.call_gemini_ocr", side_effect=mock_call_gemini_ocr_case2):
                await main.process_task_pipeline(task_id_2)

    t2 = await database.get_task(task_id_2)
    pages2 = await database.get_task_pages(task_id_2)
    
    assert case2_p3_calls >= 2, f"第 3 頁在第一輪與結尾補檢中皆應有嘗試，實際嘗試次數: {case2_p3_calls}"
    p3_case2 = next(p for p in pages2 if p["page_num"] == 3)
    assert p3_case2["status"] == "failed", f"第 3 頁狀態應明確為 failed，不可卡死在 processing，實際: {p3_case2['status']}"
    assert t2["status"] == "paused", f"部分失敗任務狀態應為 paused (以便使用者按繼續)，不可卡死在 processing，實際: {t2['status']}"
    assert t2["processed_pages"] == 4, f"完成頁數應為 4，實際: {t2['processed_pages']}"
    print("   ✅ 情境 2 通過：持續失敗頁面明確轉為 failed，任務轉為 paused，完全解決殘留 processing ⚡ 卡死問題！")

    # 清理測試環境
    if config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    print("\n🎉 所有全書結尾自動補檢複查測試全數通過！")

if __name__ == "__main__":
    asyncio.run(run_tests())
