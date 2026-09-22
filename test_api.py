import asyncio
import httpx
import config
from main import app

async def test_fastapi_endpoints():
    print("👉 [5/5] 測試 FastAPI API 與 Web 端點 (ASGI)...")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 測試首頁 HTML
        resp = await client.get("/")
        assert resp.status_code == 200, f"首頁應回傳 200，但回傳 {resp.status_code}"
        assert "Gemini PDF OCR 轉譯系統" in resp.text, "首頁應包含系統標題"
        print("   ✅ 首頁 GET / 渲染成功 (200 OK)")
        
        # 2. 測試帳號列表 API
        resp = await client.get("/api/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert "accounts" in data
        print(f"   ✅ GET /api/accounts 成功 (目前 {len(data['accounts'])} 組帳號)")
        
        # 3. 測試新增 API Key
        resp = await client.post("/api/accounts/api-key", json={
            "name": "測試單元金鑰",
            "api_key": "AIzaSyTestIntegrationKey_12345678",
            "rpm_limit": 15
        })
        assert resp.status_code == 200
        acc_id = resp.json()["account_id"]
        print(f"   ✅ POST /api/accounts/api-key 成功 (Account ID: {acc_id})")
        
        # 4. 驗證金鑰有被正確遮罩 (Masked)
        resp = await client.get("/api/accounts")
        accounts = resp.json()["accounts"]
        target = next((a for a in accounts if a["id"] == acc_id), None)
        assert target is not None
        assert target["masked_key"] == "AIza....5678", f"金鑰應被安全遮罩，實際為 {target.get('masked_key')}"
        print("   ✅ 金鑰脫敏防外洩驗證通過")
        
        # 5. 測試切換開關
        resp = await client.post(f"/api/accounts/{acc_id}/toggle")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        print("   ✅ POST /api/accounts/{id}/toggle 停用開關切換成功")
        
        # 6. 刪除測試帳號
        resp = await client.delete(f"/api/accounts/{acc_id}")
        assert resp.status_code == 200
        print("   ✅ DELETE /api/accounts/{id} 成功清理")

        # 7. 測試模型清單自動獲取與刷新 API (包含 [Web] 前綴模型)
        resp = await client.get("/api/models")
        assert resp.status_code == 200
        models_data = resp.json()
        assert "models" in models_data and models_data["count"] > 0
        web_models = [m for m in models_data["models"] if m.get("is_web", False)]
        assert len(web_models) >= 5, "應包含至少 5 個網頁自動化模型"
        print(f"   ✅ GET /api/models 獲取成功 (共有 {models_data['count']} 個模型，包含 {len(web_models)} 個網頁自動化模型)")

        resp = await client.get("/api/models?refresh=true")
        assert resp.status_code == 200
        assert "models" in resp.json()
        print("   ✅ GET /api/models?refresh=true 手動刷新端點響應正常")

        # 7.1 測試 Web RPA 模組 API (GET/POST config, status)
        resp = await client.get("/api/web-rpa/config")
        assert resp.status_code == 200
        assert "config" in resp.json() and "models" in resp.json()
        print("   ✅ GET /api/web-rpa/config 成功讀取網頁自動化配置")

        resp = await client.post("/api/web-rpa/config", json={"timeout_seconds": 65, "headless": True})
        assert resp.status_code == 200
        assert resp.json()["config"]["timeout_seconds"] == 65
        print("   ✅ POST /api/web-rpa/config 成功更新配置")

        resp = await client.get("/api/web-rpa/status")
        assert resp.status_code == 200
        assert "service" in resp.json()
        print(f"   ✅ GET /api/web-rpa/status 狀態檢測端點響應正常 (狀態: {resp.json().get('message')})")

        # 8. 測試批次上傳多個 PDF 與頁面範圍 (start_page / end_page)
        test_pdf_content = (config.DATA_DIR / "sample_test.pdf").read_bytes()
        files = [
            ("files", ("batch_doc_1.pdf", test_pdf_content, "application/pdf")),
            ("files", ("batch_doc_2.pdf", test_pdf_content, "application/pdf")),
        ]
        data = {
            "model": "gemini-3.5-flash",
            "lang": "traditional",
            "direction": "auto",
            "column": "auto",
            "custom_prompt": "測試批次",
            "start_page": 1,
            "end_page": 2
        }
        resp = await client.post("/api/tasks", files=files, data=data)
        assert resp.status_code == 200, f"批次上傳應回傳 200，但回傳 {resp.status_code}: {resp.text}"
        batch_res = resp.json()
        assert batch_res["status"] == "ok"
        assert batch_res["count"] == 2
        print(f"   ✅ POST /api/tasks 批次上傳 2 個檔案成功 (任務 ID: {batch_res['task_ids']})")

        # 驗證批次任務列表
        resp = await client.get("/api/tasks")
        tasks_list = resp.json()["tasks"]
        t_ids = [t["id"] for t in tasks_list]
        for tid in batch_res["task_ids"]:
            assert tid in t_ids, f"任務 {tid} 應存在於 tasks 列表中"

        # 9. 測試任務暫停、換模型與繼續 (Pause, Change Model & Resume)
        test_tid = batch_res["task_ids"][0]
        resp = await client.post(f"/api/tasks/{test_tid}/pause")
        assert resp.status_code == 200
        assert resp.json()["task_status"] == "paused"
        print(f"   ✅ POST /api/tasks/{test_tid}/pause 成功暫停任務")

        # 9.1 測試在暫停時更換模型 (PATCH /api/tasks/{id}/model)
        resp = await client.patch(f"/api/tasks/{test_tid}/model", json={"model": "gemini-2.0-flash-exp"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "gemini-2.0-flash-exp"
        print(f"   ✅ PATCH /api/tasks/{test_tid}/model 成功於暫停中換模型為 gemini-2.0-flash-exp")

        # 9.2 測試續傳時攜帶新模型繼續執行 (POST /api/tasks/{id}/resume with model)
        resp = await client.post(f"/api/tasks/{test_tid}/resume", json={"model": "gemini-2.0-flash"})
        assert resp.status_code == 200
        assert resp.json()["task_status"] == "processing"
        assert resp.json()["model"] == "gemini-2.0-flash"
        print(f"   ✅ POST /api/tasks/{test_tid}/resume 成功以新模型 gemini-2.0-flash 繼續執行")

        # 驗證資料庫中任務的模型已被更新
        resp = await client.get(f"/api/tasks/{test_tid}")
        assert resp.status_code == 200
        assert resp.json()["task"]["model"] == "gemini-2.0-flash"
        print("   ✅ 資料庫中任務模型確認已同步為新模型")

        # 9.3 測試單頁升級模型重新辨識 (POST /api/tasks/{id}/pages/{page_num}/retry with upgrademodel)
        resp = await client.post(f"/api/tasks/{test_tid}/pages/1/retry", json={"model": "gemini-2.5-pro"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "gemini-2.5-pro"
        print("   ✅ POST /api/tasks/{id}/pages/1/retry 成功接收升級模型 gemini-2.5-pro 重新辨識")

        for tid in batch_res["task_ids"]:
            # 清理該任務
            await client.delete(f"/api/tasks/{tid}")
        print("   ✅ 批次任務列表驗證與清理完畢")
        
    print("==================================================")
    print("🎉 FastAPI Web API 端點全數驗證通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_fastapi_endpoints())
