import asyncio
import httpx
import config
import database
from main import app, create_session_token

async def test_fastapi_endpoints():
    print("👉 [5/5] 測試 FastAPI API 與 Web 端點 (ASGI)...")
    secret = await database.get_session_secret()
    admin_token = create_session_token("admin", secret)
    headers = {"X-Admin-Token": admin_token, "Authorization": f"Bearer {admin_token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
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
        
        # 3. 測試新增免費與付費 API Key
        resp = await client.post("/api/accounts/api-key", json={
            "name": "測試單元金鑰 (Free)",
            "api_key": "AIzaSyTestIntegrationKey_12345678",
            "rpm_limit": 15,
            "is_paid": False
        })
        assert resp.status_code == 200
        acc_id = resp.json()["account_id"]
        print(f"   ✅ POST /api/accounts/api-key 新增免費金鑰成功 (Account ID: {acc_id})")

        # 3.1 測試新增付費 API Key
        resp_paid = await client.post("/api/accounts/api-key", json={
            "name": "測試付費金鑰 (Paid)",
            "api_key": "AIzaSyTestPaidKey_87654321",
            "is_paid": True
        })
        assert resp_paid.status_code == 200
        paid_acc_id = resp_paid.json()["account_id"]
        print(f"   ✅ POST /api/accounts/api-key 新增付費金鑰成功 (Account ID: {paid_acc_id})")
        
        # 4. 驗證金鑰脫敏與付費標記
        resp = await client.get("/api/accounts")
        accounts = resp.json()["accounts"]
        target = next((a for a in accounts if a["id"] == acc_id), None)
        target_paid = next((a for a in accounts if a["id"] == paid_acc_id), None)
        assert target is not None and target["masked_key"] == "AIza....5678" and target.get("is_paid", 0) == 0
        assert target_paid is not None and target_paid["masked_key"] == "AIza....4321" and target_paid.get("is_paid") == 1
        assert target_paid.get("rpm_limit") == 1000, f"付費金鑰 RPM Limit 應為 1000，實際為 {target_paid.get('rpm_limit')}"
        print("   ✅ 金鑰脫敏防外洩與付費通道標記 (is_paid=1, RPM=1000) 驗證通過")
        
        # 5. 測試切換開關
        resp = await client.post(f"/api/accounts/{acc_id}/toggle")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        print("   ✅ POST /api/accounts/{id}/toggle 停用開關切換成功")
        
        # 6. 刪除測試帳號
        resp = await client.delete(f"/api/accounts/{acc_id}")
        assert resp.status_code == 200
        print("   ✅ DELETE /api/accounts/{id} 成功清理免費測試帳號")

        # 7. 測試模型清單自動獲取與刷新 API (已完全移除 AI Studio)
        resp = await client.get("/api/models")
        assert resp.status_code == 200
        models_data = resp.json()
        assert "models" in models_data and models_data["count"] > 0
        web_models = [m for m in models_data["models"] if m.get("is_web", False)]
        assert len(web_models) >= 2, "應包含至少 2 個網頁自動化模型"
        # 驗證所有模型清單中已完全移除 AI Studio 選項
        for m in models_data["models"]:
            assert "AI Studio" not in m.get("name", "") and "AI Studio" not in m.get("id", ""), \
                f"模型選單中不應包含 AI Studio: {m}"
        print(f"   ✅ GET /api/models 獲取成功 (共有 {models_data['count']} 個模型，包含 {len(web_models)} 個網頁模型，無 AI Studio 選項)")

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

        # 10. 測試付費模式批次任務建立 (use_paid_model=True, paid_account_id)
        files_paid = [
            ("files", ("paid_batch_doc.pdf", test_pdf_content, "application/pdf")),
        ]
        data_paid = {
            "model": "gemini-3.8-flash",
            "lang": "traditional",
            "direction": "auto",
            "column": "auto",
            "custom_prompt": "測試付費批次",
            "start_page": 1,
            "end_page": 1,
            "use_paid_model": "true",
            "paid_account_id": paid_acc_id
        }
        resp = await client.post("/api/tasks", files=files_paid, data=data_paid)
        assert resp.status_code == 200
        paid_task_id = resp.json()["task_id"]
        
        # 驗證資料庫中付費標記與專屬金鑰 ID
        resp = await client.get(f"/api/tasks/{paid_task_id}")
        assert resp.status_code == 200
        task_data = resp.json()["task"]
        assert task_data.get("is_paid") == 1, "任務應標記為 is_paid == 1"
        assert task_data.get("paid_account_id") == paid_acc_id, f"任務 paid_account_id 應為 {paid_acc_id}"
        print(f"   ✅ POST /api/tasks 付費批次任務建立成功 (is_paid=1, paid_account_id={paid_acc_id})")

        # 11. 驗證排程器隔離性 (Scheduler Strict Isolation)
        from scheduler import scheduler
        # 專屬付費模式：索取指定付費帳號
        chosen_paid = await scheduler.get_next_available_account(specific_account_id=paid_acc_id)
        assert chosen_paid is not None and chosen_paid["id"] == paid_acc_id, "排程器應正確鎖定並指派指定之付費帳號"
        print("   ✅ 排程器專用模式成功鎖定指定付費帳號")

        # 免費輪詢模式：絕對不可分派付費帳號
        chosen_free = await scheduler.get_next_available_account(specific_account_id=None)
        if chosen_free is not None:
            assert chosen_free.get("is_paid", 0) == 0, "免費輪詢模式絕不可分派付費帳號！"
        print("   ✅ 排程器免費輪詢模式隔離驗證通過 (嚴禁調用付費金鑰)")

        # 清理付費任務與測試帳號
        await client.delete(f"/api/tasks/{paid_task_id}")
        await client.delete(f"/api/accounts/{paid_acc_id}")
        print("   ✅ 清理付費測試任務與付費金鑰完畢")
        
    print("==================================================")
    print("🎉 FastAPI Web API 端點全數驗證通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_fastapi_endpoints())

    # ⚠️ 必須在直譯器結束前安全關閉 PDF 渲染執行緒池。
    #
    # ThreadPoolExecutor 會註冊 atexit 處理器，在直譯器收尾時 join 它的執行緒；
    # 若此時 PDFium 仍在渲染，C++ 端會存取已釋放的記憶體 → SIGSEGV
    # （本測試先前就是這樣以 exit code 139 崩潰的）。
    # 這同時也是伺服器端「python 當機」的同一根因，main.py 的 lifespan 已同步修正。
    try:
        import main
        main.shutdown_render_executor(wait=True)
        print("🧹 已安全關閉 PDF 渲染執行緒池")
    except Exception as e:
        print(f"⚠️ 關閉渲染執行緒池失敗: {e}")
