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
        assert data['build'] == config.BUILD_NUMBER
        assert data['version'] == config.APP_VERSION

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

        # 4. 測試 [PAID] 升級模型單頁重辨與路由 (即使原本任務為免費 is_paid=0，升級重辨也能套用付費通道)
        paid_acc_id = await database.add_api_key_account("test_paid_key_v40", "AIzaSyDummyPaidKeyV40", is_paid=1)
        
        # 建立測試頁面
        page_data = [{"task_id": task_id, "page_num": 1, "image_path": str(test_pdf)}]
        await database.create_task_pages(page_data)

        gemini_calls = []
        async def mock_gemini_call(account, img, model, prompt):
            gemini_calls.append({"account": account, "model": model})
            return True, "這是付費通道升級轉譯後的結果", 200

        from unittest.mock import patch
        with patch("main.call_gemini_ocr", side_effect=mock_gemini_call):
            # 4a. 測試傳入 [PAID] gemini-2.5-pro
            res = await client.post(f"/api/tasks/{task_id}/pages/1/retry", json={
                "model": "[PAID] gemini-2.5-pro"
            })
            assert res.status_code == 200
            data = res.json()
            assert data["is_paid"] is True
            await asyncio.sleep(0.25)
            assert len(gemini_calls) == 1
            assert gemini_calls[0]["account"]["is_paid"] == 1
            assert gemini_calls[0]["model"] == "gemini-2.5-pro"
            
            p_check = (await database.get_task_pages(task_id))[0]
            assert "💎" in p_check["used_model"]
            print(f"   ✅ [4a] 成功調用 [PAID] 升級模型重辨至付費金鑰: {gemini_calls[0]['account']['name']}, 標記: {p_check['used_model']}")

            # 4b. 測試顯式傳入 is_paid=True 與指定 paid_account_id
            gemini_calls.clear()
            res = await client.post(f"/api/tasks/{task_id}/pages/1/retry", json={
                "model": "gemini-2.5-pro",
                "is_paid": True,
                "paid_account_id": paid_acc_id
            })
            assert res.status_code == 200
            await asyncio.sleep(0.25)
            assert len(gemini_calls) == 1
            assert gemini_calls[0]["account"]["id"] == paid_acc_id
            assert gemini_calls[0]["model"] == "gemini-2.5-pro"
            print(f"   ✅ [4b] 成功指定 paid_account_id ({paid_acc_id}) 並套用付費通道")

            # 4c. 測試以原帶有 💎 標記之 used_model 重試時，自動保留付費通道並清除 💎
            gemini_calls.clear()
            res = await client.post(f"/api/tasks/{task_id}/pages/1/retry", json={})
            assert res.status_code == 200
            await asyncio.sleep(0.25)
            assert len(gemini_calls) == 1
            assert gemini_calls[0]["account"]["is_paid"] == 1
            assert gemini_calls[0]["model"] == "gemini-2.5-pro"
            print(f"   ✅ [4c] 成功自 💎 used_model 延續付費重辨，API 接收模型乾淨無雜質")

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

        # 8. 測試任務工具箱：指定頁數重新切圖 (PDF Re-slice) 與 Cache Buster
        from main import parse_page_selection
        # 8a. 驗證頁碼解析邏輯
        assert parse_page_selection("1, 3, 5-8", 10) == [1, 3, 5, 6, 7, 8]
        assert parse_page_selection("1~3, 5, 8-12", 10) == [1, 2, 3, 5, 8, 9, 10]
        assert parse_page_selection("1，3，5－7", 10) == [1, 3, 5, 6, 7]
        assert parse_page_selection("", 10) == []
        print("   ✅ [8a] 頁碼範圍字串解析函式 parse_page_selection 驗證通過")

        # 8b. 建立多頁 PDF 進行重新切圖測試
        reslice_pdf = config.UPLOADS_DIR / "test_reslice.pdf"
        r_doc = pdfium.PdfDocument.new()
        for _ in range(4):
            r_doc.new_page(width=100, height=100)
        r_doc.save(str(reslice_pdf))
        r_doc.close()

        r_bytes = reslice_pdf.read_bytes()
        r_hash = hashlib.sha256(r_bytes).hexdigest()
        await database.save_file_hash(r_hash, "test_reslice.pdf", str(reslice_pdf), len(r_bytes))

        res = await client.post("/api/tasks", data={
            "existing_files": json.dumps([{
                "hash": r_hash,
                "filename": "test_reslice.pdf",
                "filepath": str(reslice_pdf)
            }]),
            "model": "gemini-2.5-flash",
            "lang": "traditional",
            "start_page": "1",
            "end_page": "4"
        })
        assert res.status_code == 200, f"Task creation failed: {res.text}"
        reslice_task_id = res.json()["task_id"]

        # 呼叫 /api/tasks/{task_id}/reslice 指定第 1, 3 頁重新切圖
        res = await client.post(f"/api/tasks/{reslice_task_id}/reslice", json={
            "mode": "custom",
            "page_range": "1, 3",
            "dpi": 150,
            "reocr": False
        })
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["status"] == "ok"
        assert res_data["pages"] == [1, 3]
        assert (config.RENDERS_DIR / reslice_task_id / "page_0001.png").exists()
        assert (config.RENDERS_DIR / reslice_task_id / "page_0003.png").exists()
        print("   ✅ [8b] 任務工具箱自訂頁碼重新切圖成功: 頁碼 [1, 3] (150 DPI)")

        # 8c. 測試全頁重新切圖 (mode="all")
        res = await client.post(f"/api/tasks/{reslice_task_id}/reslice", json={
            "mode": "all",
            "dpi": 150,
            "reocr": False
        })
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["status"] == "ok"
        assert res_data["pages"] == [1, 2, 3, 4]
        print("   ✅ [8c] 任務工具箱全頁重新切圖成功: 頁碼 [1, 2, 3, 4]")

        # 8d. 驗證 get_task_detail 返回的 image_url 包含 ?v= 快取破壞參數與未完成頁統計欄位
        res = await client.get(f"/api/tasks/{reslice_task_id}")
        assert res.status_code == 200
        detail = res.json()
        pages = detail["pages"]
        assert len(pages) >= 4
        assert "?v=" in pages[0]["image_url"]
        assert "uncompleted_pages_count" in detail["task"]
        assert "uncompleted_pages" in detail["task"]
        print(f"   ✅ [8d] 校對 UI 圖片 URL 快取破壞參數與未完成頁數欄位驗證通過: {detail['task']['uncompleted_pages_count']} 頁未完成")

        # 8e. 測試 reocr=True 觸發自動 OCR
        res = await client.post(f"/api/tasks/{reslice_task_id}/reslice", json={
            "mode": "custom",
            "page_range": "2",
            "dpi": 150,
            "reocr": True
        })
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["reocr"] is True
        assert res_data["pages"] == [2]
        print("   ✅ [8e] 重新切圖自動排入 OCR 重新辨識驗證通過")

        # 清理測試資料
        await database.delete_task(task_id)
        await database.delete_task(reslice_task_id)
        await database.delete_account(paid_acc_id)
        if test_pdf.exists():
            test_pdf.unlink(missing_ok=True)
        if reslice_pdf.exists():
            reslice_pdf.unlink(missing_ok=True)
        print("   ✅ 測試資源清理完成")

    print("\n==================================================")
    print(f"🎉 全部核心新功能與工具箱自動化驗證全部通過！({config.BUILD_NUMBER})")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_all())
