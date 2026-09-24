import asyncio
import os
import shutil
import time
from pathlib import Path
from httpx import AsyncClient, ASGITransport
import pypdfium2 as pdfium

import config
import database
import main
from main import app, process_task_pipeline, active_pipeline_tasks, active_page_tasks

async def test_rendering_and_ocr_pause():
    print("⏸️ 開始驗證切圖階段與 OCR 階段任務暫停功能...")
    await database.init_db()
    
    # 建立一個包含 5 頁的測試 PDF 檔案
    test_pdf = config.UPLOADS_DIR / "test_pause_multi_page.pdf"
    doc = pdfium.PdfDocument.new()
    for _ in range(5):
        doc.new_page(width=200, height=200)
    doc.save(str(test_pdf))
    doc.close()

    task_id = "test_pause_task_" + str(int(time.time()))
    now = time.time()
    async with database.get_db() as db:
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, total_pages, processed_pages, rendered_pages, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (task_id, test_pdf.name, str(test_pdf), 5, 0, 0, now, now))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 測試切圖階段即時暫停
        print("👉 [1/3] 測試切圖階段即時暫停...")
        
        # 啟動流水線任務
        pipe_task = asyncio.create_task(process_task_pipeline(task_id))
        
        # 等待切圖開始 (進入 rendering)
        for _ in range(30):
            await asyncio.sleep(0.05)
            t = await database.get_task(task_id)
            if t and t["status"] == "rendering":
                break
                
        print(f"   目前任務狀態: {t['status']}, 已切圖: {t.get('rendered_pages', 0)}")
        
        # 發送暫停指令
        pause_res = await client.post(f"/api/tasks/{task_id}/pause")
        assert pause_res.status_code == 200
        assert pause_res.json()["task_status"] == "paused"
        
        # 等待協程響應取消
        await asyncio.sleep(0.2)
        
        t_after = await database.get_task(task_id)
        assert t_after["status"] == "paused"
        print(f"   ✅ 切圖階段暫停成功: 狀態={t_after['status']}, 已切圖={t_after.get('rendered_pages')}/5")
        
        # 2. 測試切圖接續 (Resume)
        print("👉 [2/3] 測試自暫停切圖階段接續執行...")
        resume_res = await client.post(f"/api/tasks/{task_id}/resume")
        assert resume_res.status_code == 200
        assert resume_res.json()["task_status"] == "processing"
        
        # 模擬 OCR 函式避免真正呼叫 Google API 消耗金鑰
        ocr_called_pages = []
        async def mock_run_page_ocr(tid, p_num, img_path, model, prompt, **kwargs):
            ocr_called_pages.append(p_num)
            await asyncio.sleep(0.05)
            await database.update_page_result(tid, p_num, "completed", ocr_text=f"Text page {p_num}")
            await database.update_task_status(tid, processed_pages=len(ocr_called_pages))
            
        main.run_page_ocr = mock_run_page_ocr
        
        # 等待切圖全數完成並進入 OCR
        for _ in range(50):
            await asyncio.sleep(0.1)
            t_curr = await database.get_task(task_id)
            if t_curr and t_curr.get("rendered_pages") == 5:
                break
                
        t_rendered = await database.get_task(task_id)
        assert t_rendered.get("rendered_pages") == 5, f"切圖未全數補齊: {t_rendered}"
        print(f"   ✅ 切圖接續驗證通過: 5 頁全部切圖完畢 (rendered_pages={t_rendered['rendered_pages']})")
        
        # 3. 測試 OCR 階段即時暫停
        print("👉 [3/3] 測試 OCR 階段即時暫停...")
        # 再次發送暫停
        pause_ocr_res = await client.post(f"/api/tasks/{task_id}/pause")
        assert pause_ocr_res.status_code == 200
        await asyncio.sleep(0.2)
        
        t_paused_ocr = await database.get_task(task_id)
        assert t_paused_ocr["status"] == "paused"
        print(f"   ✅ OCR 階段暫停成功: 狀態={t_paused_ocr['status']}, 已處理頁數={t_paused_ocr.get('processed_pages')}/5")

        # 清理測試資料
        await client.delete(f"/api/tasks/{task_id}")
        if test_pdf.exists():
            test_pdf.unlink()
        print("   ✅ 測試資源清理完成")

    print("\n==================================================")
    print("🎉 切圖與 OCR 雙階段暫停/接續驗證全部通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_rendering_and_ocr_pause())
