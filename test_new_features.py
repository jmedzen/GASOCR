import asyncio
import hashlib
import json
from pathlib import Path
from httpx import AsyncClient, ASGITransport

import config
import database
from main import app

async def test_all():
    print("👉 開始驗證 5 大核心新功能...")
    await database.init_db()
    if not await database.is_admin_initialized():
        await database.init_admin_password("adminSecret123")
    await database.set_access_gate(False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 驗證 Version 端點
        res = await client.get("/api/version")
        assert res.status_code == 200
        data = res.json()
        print(f"   ✅ 版本號確認: {data['build']} ({data['version']})")
        assert data['build'] == "Build 028"

        # 2. 測試檔案 Hash 比對端點 /api/files/check-hash
        # 建立一個測試用的有效 1 頁 PDF 檔案
        import pypdfium2 as pdfium
        test_pdf = config.UPLOADS_DIR / "test_hash_check.pdf"
        doc = pdfium.PdfDocument.new()
        doc.new_page(width=100, height=100)
        doc.save(str(test_pdf))
        doc.close()
        dummy_content = test_pdf.read_bytes()
        file_hash = hashlib.sha256(dummy_content).hexdigest()

        # 寫入 database 快取
        await database.save_file_hash(file_hash, "test_hash_check.pdf", str(test_pdf), len(dummy_content))

        # 查詢 Hash
        res = await client.post("/api/files/check-hash", json={"hash": file_hash, "filename": "test.pdf", "size": len(dummy_content)})
        assert res.status_code == 200
        data = res.json()
        assert data["exists"] is True
        assert data["hash"] == file_hash
        print("   ✅ Hash 比對端點 /api/files/check-hash 命中快取成功")

        # 3. 測試秒傳 (免上傳二進位檔案，直接使用 existing_files 建立任務)
        import main
        orig_pipeline = main.process_task_pipeline
        async def mock_pipeline(tid):
            pass
        main.process_task_pipeline = mock_pipeline

        res = await client.post("/api/tasks", data={
            "existing_files": json.dumps([{
                "hash": file_hash,
                "filename": "test_hash_check.pdf",
                "filepath": str(test_pdf)
            }]),
            "model": "gemini-2.5-flash",
            "start_page": "1",
            "end_page": "1"
        })
        main.process_task_pipeline = orig_pipeline
        assert res.status_code == 200
        data = res.json()
        task_id = data["task_id"]
        print(f"   ✅ 秒傳免上傳 (existing_files) 任務建立成功: {task_id}")

        # 檢查 task 是否有正確保存 file_hash
        task = await database.get_task(task_id)
        assert task["file_hash"] == file_hash
        print(f"   ✅ 任務資料表 file_hash 驗證通過: {task['file_hash'][:12]}...")

        # 4. 測試 [PAID] 升級模型單頁重辨與路由
        # 新增一組付費測試金鑰
        paid_acc_id = await database.add_api_key_account("test_paid_key", "AIzaSyDummyPaidKeyForTesting", is_paid=1)
        
        # 建立一個測試 page
        page_data = [{"task_id": task_id, "page_num": 1, "image_path": str(test_pdf)}]
        await database.create_task_pages(page_data)

        # 呼叫 retry 端點，使用 [PAID] gemini-2.5-pro
        original_run_page_ocr = main.run_page_ocr
        async def mock_run_page_ocr(**kwargs):
            pass
        main.run_page_ocr = mock_run_page_ocr

        res = await client.post(f"/api/tasks/{task_id}/pages/1/retry", json={
            "model": "[PAID] gemini-2.5-pro"
        })
        assert res.status_code == 200
        data = res.json()
        print(f"   ✅ 成功調用 [PAID] 升級模型重辨: {data['model']}")
        main.run_page_ocr = original_run_page_ocr

        # 5. 測試 SSE 串流富資訊欄位
        from main import sse_task_events
        sse_resp = await sse_task_events(task_id)
        assert sse_resp.media_type == "text/event-stream"
        first_event = await sse_resp.body_iterator.__anext__()
        assert first_event.startswith("data: ")
        payload = json.loads(first_event[6:].strip())
        assert "rendered_pages" in payload
        assert "avg_speed" in payload
        assert "eta_seconds" in payload
        assert "pdf_total_pages" in payload
        assert "bg_render_status" in payload
        print(f"   ✅ SSE 富資訊與預處理進度驗證通過: total={payload['total_pages']}, rendered={payload['rendered_pages']}, speed={payload['avg_speed']}, eta={payload['eta_seconds']}s")

        # 6. 測試圖片預處理切圖進度即時回呼 (render_pdf_to_images_async)
        from pdf_engine import render_pdf_to_images_async
        render_progress_events = []
        async def track_render(p_num, total_target, out_path):
            render_progress_events.append((p_num, total_target))
        
        await render_pdf_to_images_async(
            test_pdf, 
            "test_render_track", 
            start_page=1, 
            end_page=1, 
            dpi=72,
            on_page_rendered=track_render
        )
        assert len(render_progress_events) == 1
        assert render_progress_events[-1] == (1, 1)
        print(f"   ✅ 圖片預處理切圖進度回呼驗證通過: {len(render_progress_events)} 頁全部成功觸發進度事件")

        # 7. 測試模型清單動態抓取與刷新 (/api/models?refresh=true)
        res = await client.get("/api/models?refresh=true")
        assert res.status_code == 200
        m_data = res.json()
        assert "models" in m_data and m_data["count"] >= 10
        assert m_data["updated"] is True
        print(f"   ✅ 模型動態抓取與刷新驗證通過: 成功獲取 {m_data['count']} 個模型 (updated={m_data['updated']})")

        # 清理測試資料
        await database.delete_task(task_id)
        await database.delete_account(paid_acc_id)
        if test_pdf.exists():
            test_pdf.unlink(missing_ok=True)
        print("   ✅ 測試資源清理完成")

    print("\n==================================================")
    print("🎉 5 大核心新功能自動化驗證全部通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_all())
