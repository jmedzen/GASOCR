import asyncio
import os
import time
import uuid
import shutil
from pathlib import Path
import pypdfium2 as pdfium
import config
import database
from main import RENDER_SEMAPHORE, PDF_RENDER_EXECUTOR, process_task_pipeline, app
from httpx import AsyncClient, ASGITransport

async def test_pending_queue_status():
    print("==================================================")
    print("🧪 驗證大量 PDF 上傳時排隊等待切圖之「等待中」狀態")
    print("==================================================")
    await database.init_db()

    # 1. 建立測試用 3 頁 PDF 檔案
    test_pdf_dir = config.RENDERS_DIR / f"test_pending_status_{uuid.uuid4().hex[:6]}"
    test_pdf_dir.mkdir(parents=True, exist_ok=True)
    test_pdf_path = test_pdf_dir / "test_pending.pdf"

    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(width=100, height=100)
    doc.save(test_pdf_path)
    doc.close()

    task_id_1 = f"test_task_wait_{uuid.uuid4().hex[:8]}"
    task_render_dir = config.RENDERS_DIR / task_id_1
    pipe_task = None

    try:
        # 2. 測試 create_task 時已正確預填 total_pages 與 status='pending'
        await database.create_task(
            task_id=task_id_1,
            filename="test_wait_1.pdf",
            filepath=str(test_pdf_path),
            model="gemini-3.5-flash-lite",
            lang="traditional",
            direction="auto",
            column="auto",
            start_page=1,
            end_page=3,
            pdf_total_pages=3
        )

        t1 = await database.get_task(task_id_1)
        assert t1 is not None
        assert t1["status"] == "pending", f"初始狀態應為 pending，實際: {t1['status']}"
        assert t1["total_pages"] == 3, f"初始 total_pages 應為 3，實際: {t1['total_pages']}"
        assert t1["processed_pages"] == 0
        assert t1["rendered_pages"] == 0
        print(f"👉 [1/3] 驗證 create_task 初始狀態與頁數：status={t1['status']}, total_pages={t1['total_pages']} ✅ 通過")

        # 3. 測試在 Semaphore 佔滿時，後續任務排隊等待切圖時保持 status='pending'
        print(f"👉 [2/3] 測試排隊等待 RENDER_SEMAPHORE 時狀態維持 pending (等待中)...")
        
        # 佔用所有 RENDER_SEMAPHORE 槽位
        acquired_count = 0
        for _ in range(config.MAX_CONCURRENT_RENDERS):
            await RENDER_SEMAPHORE.acquire()
            acquired_count += 1

        try:
            # 在信號量已被佔滿的情況下啟動任務流水線
            pipe_task = asyncio.create_task(process_task_pipeline(task_id_1))
            # 稍等讓 process_task_pipeline 執行到 RENDER_SEMAPHORE 前排隊
            await asyncio.sleep(0.3)

            t1_queued = await database.get_task(task_id_1)
            assert t1_queued["status"] == "pending", f"排隊等待切圖槽位時狀態應為 pending，實際: {t1_queued['status']}"
            print(f"   ✅ 任務排隊等待切圖時，資料庫狀態保持: {t1_queued['status']} (等待中)")

            # 4. 驗證 /api/tasks 端點回傳 status='pending'
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.get("/api/tasks")
                assert res.status_code == 200
                tasks_data = res.json()["tasks"]
                target_t = next((t for t in tasks_data if t["id"] == task_id_1), None)
                assert target_t is not None
                assert target_t["status"] == "pending", f"/api/tasks 回傳狀態應為 pending，實際: {target_t['status']}"
                print(f"   ✅ /api/tasks 回傳等待切圖任務狀態為: {target_t['status']} (等待中)")
        finally:
            # 釋放先前佔用的槽位
            for _ in range(acquired_count):
                RENDER_SEMAPHORE.release()

        # 5. 槽位釋放後，等待任務獲取槽位並進入切圖 (rendering) 或完成
        print(f"👉 [3/3] 槽位釋放後，任務獲取槽位並成功推進切圖...")
        # 等待一段時間讓 pipeline 執行切圖
        await asyncio.sleep(0.6)
        t1_after = await database.get_task(task_id_1)
        assert t1_after["status"] in ["rendering", "processing", "completed"], f"槽位釋放後狀態應推進，實際: {t1_after['status']}"
        print(f"   ✅ 任務成功獲得切圖槽位，狀態推進至: {t1_after['status']}")

    finally:
        # 清理測試任務與目錄
        if pipe_task and not pipe_task.done():
            pipe_task.cancel()
        await database.delete_task(task_id_1)
        if test_pdf_path.exists():
            test_pdf_path.unlink(missing_ok=True)
        if test_pdf_dir.exists():
            shutil.rmtree(test_pdf_dir, ignore_errors=True)
        if task_render_dir.exists():
            shutil.rmtree(task_render_dir, ignore_errors=True)

    print("==================================================")
    print("🎉 大量 PDF 上傳排隊等待切圖「等待中」狀態驗證全部通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_pending_queue_status())
