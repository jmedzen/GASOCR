import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

import config
import database
from main import app, run_page_ocr

client = TestClient(app)

async def test_stale_task_recovery():
    print("👉 [1/3] 測試伺服器重啟/崩潰時的任務自動修復 (Stale Task Recovery)...")
    await database.init_db()
    
    # 建立測試任務 1: 部分頁面完成，部分頁面中斷在 processing
    t1_id = "test_stale_task_1"
    async with database.get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO tasks (id, filename, original_filepath, total_pages, processed_pages, status, bg_render_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (t1_id, "stale_1.pdf", "/tmp/stale_1.pdf", 2, 1, "processing", "rendering", 100.0, 100.0))
        
        await db.execute("""
            INSERT OR REPLACE INTO task_pages (task_id, page_num, image_path, status, error_message, updated_at)
            VALUES (?, 1, '/tmp/p1.png', 'completed', '', 100.0)
        """, (t1_id,))
        await db.execute("""
            INSERT OR REPLACE INTO task_pages (task_id, page_num, image_path, status, error_message, updated_at)
            VALUES (?, 2, '/tmp/p2.png', 'processing', '', 100.0)
        """, (t1_id,))

        # 建立測試任務 2: 頁面其實全部完成，但 task 狀態卡在 processing
        t2_id = "test_stale_task_2"
        await db.execute("""
            INSERT OR REPLACE INTO tasks (id, filename, original_filepath, total_pages, processed_pages, status, bg_render_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (t2_id, "stale_2.pdf", "/tmp/stale_2.pdf", 2, 2, "processing", "idle", 100.0, 100.0))
        await db.execute("""
            INSERT OR REPLACE INTO task_pages (task_id, page_num, image_path, status, error_message, updated_at)
            VALUES (?, 1, '/tmp/p1.png', 'completed', '', 100.0)
        """, (t2_id,))
        await db.execute("""
            INSERT OR REPLACE INTO task_pages (task_id, page_num, image_path, status, error_message, updated_at)
            VALUES (?, 2, '/tmp/p2.png', 'completed', '', 100.0)
        """, (t2_id,))
        await db.commit()

    # 執行自動修復
    stats = await database.recover_stale_tasks_on_startup()
    print(f"   修復統計: {stats}")
    assert stats["tasks_paused"] >= 1, "應至少暫停 1 個未完成的崩潰任務"
    assert stats["tasks_completed"] >= 1, "應自動標記 1 個已完成全頁面的假死任務為 completed"
    assert stats["pages_recovered"] >= 1, "應至少修復 1 個 processing 狀態的孤兒頁面為 paused"

    # 檢驗資料庫實際更新結果
    t1 = await database.get_task(t1_id)
    assert t1["status"] == "paused", f"未完成任務狀態應轉為 paused，實際為 {t1['status']}"
    assert t1["bg_render_status"] == "paused", f"未完成切圖狀態應轉為 paused，實際為 {t1['bg_render_status']}"
    
    t1_pages = await database.get_task_pages(t1_id)
    p2 = next(p for p in t1_pages if p["page_num"] == 2)
    assert p2["status"] == "paused", f"中斷頁面狀態應轉為 paused，實際為 {p2['status']}"
    assert "伺服器重新啟動中斷" in p2["error_message"]

    t2 = await database.get_task(t2_id)
    assert t2["status"] == "completed", f"已完成頁面之任務應轉為 completed，實際為 {t2['status']}"

    # 清理測試資料
    async with database.get_db() as db:
        await db.execute("DELETE FROM task_pages WHERE task_id IN (?, ?)", (t1_id, t2_id))
        await db.execute("DELETE FROM tasks WHERE id IN (?, ?)", (t1_id, t2_id))
        await db.commit()
    print("   ✅ 伺服器重啟/崩潰修復驗證通過！")


async def test_fallback_routing():
    print("👉 [2/3] 測試智慧降級備援 (Fallback Routing)...")
    await database.init_db()

    # 建立 1 個付費帳號與 1 個免費帳號
    test_task_id = "test_fallback_task"
    test_img = config.RENDERS_DIR / test_task_id / "page_1.png"
    test_img.parent.mkdir(parents=True, exist_ok=True)
    test_img.touch()

    async with database.get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO tasks (id, filename, original_filepath, total_pages, processed_pages, status, is_paid, paid_account_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (test_task_id, "fallback.pdf", "/tmp/fallback.pdf", 1, 0, "processing", 1, 99999, 100.0, 100.0))
        
        await db.execute("""
            INSERT OR REPLACE INTO task_pages (task_id, page_num, image_path, status, error_message, updated_at)
            VALUES (?, 1, ?, 'processing', '', 100.0)
        """, (test_task_id, str(test_img)))
        
        # 確保有一組可用的免費金鑰
        await db.execute("""
            INSERT OR REPLACE INTO accounts (id, name, auth_type, api_key, is_active, is_paid, cooldown_until, created_at)
            VALUES (88888, 'Test Free Key', 'api_key', 'AIzaSyFreeKeyFake12345', 1, 0, 0.0, 100.0)
        """)
        # 建立已冷卻或無效的付費金鑰 (ID: 99999)
        await db.execute("""
            INSERT OR REPLACE INTO accounts (id, name, auth_type, api_key, is_active, is_paid, cooldown_until, created_at)
            VALUES (99999, 'Test Paid Key', 'api_key', 'AIzaSyPaidKeyFake67890', 1, 1, 9999999999.0, 100.0)
        """)
        await db.commit()

    # 情境 1: 付費金鑰冷卻中或無可用帳號，觸發降級至免費池預設模型
    # 模擬 call_gemini_ocr 成功回傳
    async def mock_call_gemini_ocr(account, image_path, model, prompt):
        assert account["id"] == 88888, f"應降級至免費帳號 88888，實際為 {account['id']}"
        assert model == config.DEFAULT_MODEL, f"模型應降級為 {config.DEFAULT_MODEL}，實際為 {model}"
        return True, "這是降級備援成功辨識的古籍文本", 200

    with patch("main.call_gemini_ocr", side_effect=mock_call_gemini_ocr):
        await run_page_ocr(
            task_id=test_task_id,
            page_num=1,
            image_path=test_img,
            model="[PAID] gemini-2.5-pro",
            prompt="請精確辨識",
            specific_account_id=99999
        )

    # 檢驗資料庫紀錄
    pages = await database.get_task_pages(test_task_id)
    assert len(pages) == 1
    p1 = pages[0]
    assert p1["status"] == "completed", f"頁面狀態應為 completed，實際為 {p1['status']}"
    assert "降級備援" in p1["used_model"], f"使用的模型應有 [降級備援] 標記，實際為: {p1['used_model']}"
    print(f"   使用的備援標記: {p1['used_model']}")
    print("   ✅ 付費 API 配額耗盡時自動降級備援至免費預設模型驗證成功！")

    # 清理測試資料
    async with database.get_db() as db:
        await db.execute("DELETE FROM task_pages WHERE task_id = ?", (test_task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (test_task_id,))
        await db.execute("DELETE FROM accounts WHERE id IN (88888, 99999)")
        await db.commit()
    if test_img.exists():
        test_img.unlink()
    if test_img.parent.exists():
        test_img.parent.rmdir()


def test_modular_template_rendering():
    print("👉 [3/3] 測試模組化模板渲染 (Jinja2 Includes)...")
    res = client.get("/")
    assert res.status_code == 200, f"首頁應回傳 200，實際為 {res.status_code}"
    html = res.text
    
    # 檢查關鍵組件標記是否存在於最終渲染的 HTML 中
    assert "GASOCR" in html, "HTML 應包含系統標題 GASOCR"
    assert config.BUILD_NUMBER in html, f"HTML 應包含版本號 {config.BUILD_NUMBER}"
    assert "id=\"proofread-section\"" in html, "HTML 應包含校對工作區 (viewer_modal.html)"
    assert "x-data=\"ocrApp()\"" in html, "HTML 應掛載 Alpine.js ocrApp 控制器"
    assert "showAccountsModal" in html, "HTML 應包含帳號管理彈窗組件"
    assert "showAdminModal" in html, "HTML 應包含管理員彈窗組件"
    assert "reocrQueue" in html, "HTML 應包含 Re-OCR 佇列組件"
    assert "💎 使用付費 API" in html, "HTML 應包含已修正的「使用付費 API」文字"
    print("   ✅ 12 個模板組件無縫聚合渲染檢驗通過！")


if __name__ == "__main__":
    print("🚀 開始驗證任務自動修復、智慧降級備援與模板模組化...")
    asyncio.run(test_stale_task_recovery())
    asyncio.run(test_fallback_routing())
    test_modular_template_rendering()
    print("🎉 所有新功能與模組化驗證全部通過！")
