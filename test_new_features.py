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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 驗證 Version 端點
        res = await client.get("/api/version")
        assert res.status_code == 200
        data = res.json()
        print(f"   ✅ 版本號確認: {data['build']} ({data['version']})")
        assert data['build'] == "Build 011"

        # 2. 測試檔案 Hash 比對端點 /api/files/check-hash
        # 建立一個測試用的虛構 pdf 檔案
        test_pdf = config.UPLOADS_DIR / "test_hash_check.pdf"
        dummy_content = b"%PDF-1.4 Dummy PDF Content for Hash Check Testing"
        test_pdf.write_bytes(dummy_content)
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
        res = await client.post(f"/api/tasks/{task_id}/pages/1/retry", json={
            "model": "[PAID] gemini-2.5-pro"
        })
        assert res.status_code == 200
        data = res.json()
        print(f"   ✅ 成功調用 [PAID] 升級模型重辨: {data['model']}")

        # 5. 測試 SSE 串流富資訊欄位
        res = await client.get(f"/api/tasks/{task_id}/events")
        assert res.status_code == 200
        # 讀取首個 SSE 事件
        async for line in res.aiter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                assert "rendered_pages" in payload
                assert "avg_speed" in payload
                assert "eta_seconds" in payload
                assert "pdf_total_pages" in payload
                assert "bg_render_status" in payload
                print(f"   ✅ SSE 富資訊與預處理進度驗證通過: total={payload['total_pages']}, rendered={payload['rendered_pages']}, speed={payload['avg_speed']}, eta={payload['eta_seconds']}s")
                break

        # 6. 測試圖片預處理切圖進度即時回呼 (render_pdf_to_images_async)
        from pdf_engine import render_pdf_to_images_async
        sample_pdf = config.BASE_DIR / "sets" / "大日本佛敎全書.第082冊-大乗法相宗名目.pdf"
        if sample_pdf.exists():
            render_progress_events = []
            async def track_render(p_num, total_target, out_path):
                render_progress_events.append((p_num, total_target))
            
            await render_pdf_to_images_async(
                sample_pdf, 
                "test_render_track", 
                start_page=1, 
                end_page=3, 
                on_page_rendered=track_render
            )
            assert len(render_progress_events) == 3
            assert render_progress_events[-1] == (3, 3)
            print(f"   ✅ 圖片預處理 300 DPI 逐頁切圖進度回呼驗證通過: {len(render_progress_events)} 頁全部成功觸發進度事件")

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
