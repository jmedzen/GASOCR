"""
Automated Test Suite for Batch Tasks Multi-Select Deletion
Tests:
1. Batch deletion of multiple tasks simultaneously
2. Cascade deletion of task_pages and render caches
3. Independent task preservation (tasks not in batch delete remain intact)
4. Empty and invalid batch delete payloads handling
"""

import asyncio
import os
import shutil
from pathlib import Path
from fastapi.testclient import TestClient

import config
import database
from main import app

def run_batch_delete_tests():
    print("🗑️ 開始驗證批次任務多選刪除功能 (Batch Tasks Multi-Select Deletion)...")
    client = TestClient(app)

    # 1. 準備 3 個測試任務
    task_id_1 = "test_batch_del_1"
    task_id_2 = "test_batch_del_2"
    task_id_3 = "test_batch_del_3"

    # 建立測試切圖目錄
    for tid in [task_id_1, task_id_2, task_id_3]:
        rdir = config.RENDERS_DIR / tid
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "page_1.png").write_text("dummy")

    async def setup_tasks():
        await database.init_db()
        await database.delete_tasks([task_id_1, task_id_2, task_id_3])
        for i, tid in enumerate([task_id_1, task_id_2, task_id_3], start=1):
            await database.create_task(
                task_id=tid,
                filename=f"test_doc_{i}.pdf",
                filepath=f"/tmp/test_doc_{i}.pdf",
                model=config.DEFAULT_MODEL,
                lang="traditional",
                direction="auto",
                column="auto",
                custom_prompt="",
                start_page=1,
                end_page=1,
                is_paid=0,
                paid_account_id=None,
                pdf_total_pages=1,
                file_hash=f"hash_{tid}"
            )
            # 建立對應頁面
            await database.create_task_pages([{
                "task_id": tid,
                "page_num": 1,
                "image_path": str(config.RENDERS_DIR / tid / "page_1.png")
            }])

    asyncio.run(setup_tasks())
    print("   ✅ 成功建立 3 個測試任務與對應切圖資源")

    # 2. 測試批次刪除空列表
    resp_empty = client.post("/api/tasks/batch-delete", json={"task_ids": []})
    assert resp_empty.status_code == 200
    assert resp_empty.json()["deleted_count"] == 0
    print("   ✅ 空列表防呆校驗通過")

    # 3. 執行批次刪除 (task_1 與 task_2)
    resp_batch = client.post("/api/tasks/batch-delete", json={"task_ids": [task_id_1, task_id_2]})
    assert resp_batch.status_code == 200, f"批次刪除失敗: {resp_batch.text}"
    data = resp_batch.json()
    assert data["deleted_count"] == 2, f"應刪除 2 個任務，實際: {data['deleted_count']}"
    assert task_id_1 in data["deleted_ids"]
    assert task_id_2 in data["deleted_ids"]
    print("   ✅ POST /api/tasks/batch-delete 成功回傳刪除 2 筆任務")

    # 4. 驗證資料庫與磁碟狀態
    async def verify_deleted():
        t1 = await database.get_task(task_id_1)
        t2 = await database.get_task(task_id_2)
        t3 = await database.get_task(task_id_3)
        assert t1 is None, f"{task_id_1} 應已自資料庫刪除"
        assert t2 is None, f"{task_id_2} 應已自資料庫刪除"
        assert t3 is not None, f"{task_id_3} 應維持存在於資料庫"

        p1 = await database.get_task_pages(task_id_1)
        p2 = await database.get_task_pages(task_id_2)
        p3 = await database.get_task_pages(task_id_3)
        assert len(p1) == 0, f"{task_id_1} 頁面應全數級聯刪除"
        assert len(p2) == 0, f"{task_id_2} 頁面應全數級聯刪除"
        assert len(p3) == 1, f"{task_id_3} 頁面應保留"

    asyncio.run(verify_deleted())

    # 驗證切圖目錄清理
    assert not (config.RENDERS_DIR / task_id_1).exists(), f"{task_id_1} 切圖目錄應已清除"
    assert not (config.RENDERS_DIR / task_id_2).exists(), f"{task_id_2} 切圖目錄應已清除"
    assert (config.RENDERS_DIR / task_id_3).exists(), f"{task_id_3} 切圖目錄應仍存在"
    print("   ✅ 資料庫級聯刪除與磁碟快取目錄清理驗證通過")

    # 5. 清理第 3 個任務
    resp_clean = client.post("/api/tasks/batch-delete", json={"task_ids": [task_id_3]})
    assert resp_clean.status_code == 200
    assert resp_clean.json()["deleted_count"] == 1
    assert not (config.RENDERS_DIR / task_id_3).exists()
    print("   ✅ 第 3 個任務清理完成")

    print("\n🎉 批次任務多選刪除功能自動化驗證全部通過！\n")

if __name__ == "__main__":
    run_batch_delete_tests()
