import asyncio
import json
from pathlib import Path
from httpx import AsyncClient, ASGITransport
import pypdfium2 as pdfium

import config
import database
from main import app
import main

async def test_queue_api_error_handling():
    print("=" * 60)
    print("🧪 驗證重辨佇列 (Re-OCR Queue) API 異常與錯誤回應處理")
    print("=" * 60)

    await database.init_db()
    if not await database.is_admin_initialized():
        await database.init_admin_password("adminSecret123")
    await database.set_access_gate(False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 測試不存在任務的單頁重試 (404)
        print("👉 [1/6] 測試不存在任務重試: POST /api/tasks/invalid_tid/pages/1/retry -> 404 Task not found...")
        res = await client.post("/api/tasks/invalid_tid/pages/1/retry", json={"model": "gemini-2.5-flash"})
        assert res.status_code == 404
        data = res.json()
        assert data.get("detail") == "Task not found"
        print("   ✅ 404 Task not found 格式正確")

        # 2. 測試不存在任務的單頁暫停 (404)
        print("👉 [2/6] 測試不存在任務暫停: POST /api/tasks/invalid_tid/pages/1/pause -> 404 Task not found...")
        res = await client.post("/api/tasks/invalid_tid/pages/1/pause")
        assert res.status_code == 404
        data = res.json()
        assert data.get("detail") == "Task not found"
        print("   ✅ 404 Task not found 格式正確")

        # 3. 測試不存在任務的單頁繼續 (404)
        print("👉 [3/6] 測試不存在任務繼續: POST /api/tasks/invalid_tid/pages/1/resume -> 404 Task not found...")
        res = await client.post("/api/tasks/invalid_tid/pages/1/resume", json={"model": "gemini-2.5-flash"})
        assert res.status_code == 404
        data = res.json()
        assert data.get("detail") == "Task not found"
        print("   ✅ 404 Task not found 格式正確")

        # 4. 測試不存在任務的詳情查詢 (404)
        print("👉 [4/6] 測試不存在任務詳情: GET /api/tasks/invalid_tid -> 404 Task not found...")
        res = await client.get("/api/tasks/invalid_tid")
        assert res.status_code == 404
        data = res.json()
        assert data.get("detail") == "Task not found"
        print("   ✅ 404 Task not found 格式正確")

        # 5. 建立真實任務，測試頁碼不存在的單頁重試 (404 Page not found)
        print("👉 [5/6] 建立測試任務，測試不存在頁碼: POST /api/tasks/{tid}/pages/999/retry -> 404 Page not found...")
        test_pdf = config.UPLOADS_DIR / "test_queue_err.pdf"
        doc = pdfium.PdfDocument.new()
        doc.new_page(width=100, height=100)
        doc.save(str(test_pdf))
        doc.close()

        task_id = "test_queue_task_001"
        await database.create_task(
            task_id=task_id,
            filename="test_queue_err.pdf",
            filepath=str(test_pdf),
            model="gemini-2.5-flash",
            lang="traditional",
            direction="auto",
            column="auto",
            start_page=1,
            end_page=1,
            pdf_total_pages=1
        )
        await database.create_task_pages([{
            "task_id": task_id,
            "page_num": 1,
            "image_path": str(test_pdf),
            "status": "pending"
        }])

        res = await client.post(f"/api/tasks/{task_id}/pages/999/retry", json={"model": "gemini-2.5-flash"})
        assert res.status_code == 404
        data = res.json()
        assert data.get("detail") == "Page not found"
        print("   ✅ 404 Page not found 格式正確")

        # 6. 測試正常頁面的暫停與重試，並驗證狀態正確寫入資料庫
        print("👉 [6/6] 測試正常頁面 pause -> 狀態 paused，以及 resume -> 啟動成功...")
        res = await client.post(f"/api/tasks/{task_id}/pages/1/pause")
        assert res.status_code == 200
        p_data = res.json()
        assert p_data["status"] == "ok"

        pages = await database.get_task_pages(task_id)
        assert pages[0]["status"] == "paused"
        assert pages[0]["error_message"] == "已手動暫停"
        print("   ✅ 單頁暫停成功，資料庫 status=paused, error_message='已手動暫停'")

        # Mock run_page_ocr to test resume
        orig_run_ocr = main.run_page_ocr
        async def mock_ocr(**kwargs):
            pass
        main.run_page_ocr = mock_ocr

        res = await client.post(f"/api/tasks/{task_id}/pages/1/resume", json={"model": "gemini-2.5-flash"})
        assert res.status_code == 200
        r_data = res.json()
        assert r_data["status"] == "ok"
        print("   ✅ 單頁繼續成功啟動")

        main.run_page_ocr = orig_run_ocr

        # 清理測試任務
        await database.delete_task(task_id)
        if test_pdf.exists():
            test_pdf.unlink()

    print("\n🎉 重辨佇列 API 異常與錯誤回應驗證全部通過！\n")

if __name__ == "__main__":
    asyncio.run(test_queue_api_error_handling())
