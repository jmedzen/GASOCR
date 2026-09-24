import asyncio
import os
import shutil
import time
from pathlib import Path
from httpx import AsyncClient, ASGITransport

os.environ["DATA_DIR"] = "/tmp/test_gasocr_global_pr"
import config
config.DATA_DIR = Path("/tmp/test_gasocr_global_pr")
config.RENDERS_DIR = config.DATA_DIR / "renders"
config.UPLOADS_DIR = config.DATA_DIR / "uploads"
config.DB_PATH = config.DATA_DIR / "ocr_system.db"
config.MIN_FREE_DISK_MB = 10

import database
import main
from main import app

async def run_tests():
    print("🚀 開始驗證任務全局全部暫停與全部繼續 (Global Pause All & Resume All)...")
    if config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    
    await database.init_db()
    await database.init_admin_password("TestAdmin123")
    await database.set_access_gate(enabled=False)
    
    now = time.time()
    t1 = "test_gpr_t1"
    t2 = "test_gpr_t2"
    t3 = "test_gpr_t3"

    async with database.get_db() as db:
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, total_pages, status, created_at, updated_at)
            VALUES (?, 'book1.pdf', '/tmp/b1.pdf', 5, 'processing', ?, ?)
        """, (t1, now, now))
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, total_pages, status, created_at, updated_at)
            VALUES (?, 'book2.pdf', '/tmp/b2.pdf', 10, 'rendering', ?, ?)
        """, (t2, now, now))
        await db.execute("""
            INSERT INTO tasks (id, filename, original_filepath, total_pages, status, created_at, updated_at)
            VALUES (?, 'book3.pdf', '/tmp/b3.pdf', 3, 'completed', ?, ?)
        """, (t3, now, now))
        
        # 建立 t1 與 t2 的頁面
        await db.execute("""
            INSERT INTO task_pages (task_id, page_num, image_path, status, updated_at)
            VALUES (?, 1, '/tmp/p1.png', 'processing', ?)
        """, (t1, now))
        await db.execute("""
            INSERT INTO task_pages (task_id, page_num, image_path, status, updated_at)
            VALUES (?, 1, '/tmp/p2.png', 'rendering', ?)
        """, (t2, now))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 測試全局全部暫停 (POST /api/tasks/pause-all)
        print("👉 [1/4] 測試全局全部暫停 (pause-all)...")
        res = await client.post("/api/tasks/pause-all", json={})
        assert res.status_code == 200, f"pause-all 應回傳 200, 實際 {res.status_code}"
        data = res.json()
        assert data["paused_count"] == 2, f"應暫停 2 個執行中任務 (t1, t2), 實際: {data['paused_count']}"
        assert set(data["task_ids"]) == {t1, t2}
        
        task1 = await database.get_task(t1)
        task2 = await database.get_task(t2)
        task3 = await database.get_task(t3)
        assert task1["status"] == "paused"
        assert task2["status"] == "paused"
        assert task3["status"] == "completed", "已完成之 t3 不應受影響"
        print("   ✅ 全局全部暫停成功：t1, t2 均轉為 paused，t3 保持 completed")

        # 2. 測試全局全部繼續 (POST /api/tasks/resume-all)
        print("👉 [2/4] 測試全局全部繼續 (resume-all)...")
        # 使用 patch 攔截 process_task_pipeline 啟動
        from unittest.mock import patch, AsyncMock
        with patch("main.process_task_pipeline", new_callable=AsyncMock) as mock_pipe:
            res = await client.post("/api/tasks/resume-all", json={})
            assert res.status_code == 200
            data = res.json()
            assert data["resumed_count"] == 2
            assert set(data["task_ids"]) == {t1, t2}
            assert mock_pipe.call_count == 2
            
            task1 = await database.get_task(t1)
            task2 = await database.get_task(t2)
            assert task1["status"] == "processing"
            assert task2["status"] == "processing"
            print("   ✅ 全局全部繼續成功：t1, t2 均已重設為 processing 並重啟流水線")

        # 3. 測試指定任務批次暫停 (POST /api/tasks/pause-all with task_ids=[t1])
        print("👉 [3/4] 測試選取任務批次暫停 (pause-all with task_ids)...")
        res = await client.post("/api/tasks/pause-all", json={"task_ids": [t1]})
        assert res.status_code == 200
        data = res.json()
        assert data["paused_count"] == 1
        assert data["task_ids"] == [t1]
        
        task1 = await database.get_task(t1)
        task2 = await database.get_task(t2)
        assert task1["status"] == "paused"
        assert task2["status"] == "processing", "未選取的 t2 應保持 processing"
        print("   ✅ 選取任務批次暫停成功：僅 t1 暫停，t2 持續運行")

        # 4. 測試指定任務批次繼續 (POST /api/tasks/resume-all with task_ids=[t1])
        print("👉 [4/4] 測試選取任務批次繼續 (resume-all with task_ids)...")
        with patch("main.process_task_pipeline", new_callable=AsyncMock) as mock_pipe:
            res = await client.post("/api/tasks/resume-all", json={"task_ids": [t1]})
            assert res.status_code == 200
            data = res.json()
            assert data["resumed_count"] == 1
            assert data["task_ids"] == [t1]
            assert mock_pipe.call_count == 1
            
            task1 = await database.get_task(t1)
            assert task1["status"] == "processing"
            print("   ✅ 選取任務批次繼續成功：t1 接續執行")

    # 清理測試環境
    if config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    print("\n🎉 任務全局/批次暫停與繼續自動化驗證全數通過！")

if __name__ == "__main__":
    asyncio.run(run_tests())
