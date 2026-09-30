import asyncio
import time
from pathlib import Path
import httpx

import database
import config
from main import app

async def run_manual_pass_tests():
    print("==================================================")
    print("🧪 開始手動標記通過 (Pass OCR) 與任務自動完成狀態切換測試")
    print("==================================================")

    # 1. 建立測試任務 (3 頁)
    test_task_id = f"test_pass_{int(time.time())}"
    await database.create_task(
        task_id=test_task_id,
        filename="test_manual_pass.pdf",
        filepath="/tmp/test_manual_pass.pdf",
        status="paused", # 模擬剩餘頁面失敗導致暫停的情境
        model="gemini-2.5-flash",
        lang="traditional",
        direction="auto",
        column="auto",
        custom_prompt="",
        start_page=1,
        end_page=3,
        pdf_total_pages=3
    )

    # 2. 建立 3 頁：第 1 頁已完成，第 2 頁失敗，第 3 頁暫停
    pages_data = [
        {"task_id": test_task_id, "page_num": 1, "image_path": "/tmp/p1.png", "status": "completed"},
        {"task_id": test_task_id, "page_num": 2, "image_path": "/tmp/p2.png", "status": "failed"},
        {"task_id": test_task_id, "page_num": 3, "image_path": "/tmp/p3.png", "status": "paused"},
    ]
    await database.create_task_pages(pages_data)
    await database.update_page_result(test_task_id, 1, "completed", ocr_text="第 1 頁辨識內容")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 驗證初始狀態：任務為 paused，未完成 2 頁
        res = await client.get(f"/api/tasks/{test_task_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["task"]["status"] == "paused"
        assert data["task"]["uncompleted_pages_count"] == 2
        assert data["task"]["uncompleted_pages"] == [2, 3]
        print("   ✅ [1] 初始狀態驗證通過: 任務 paused, 2 頁未完成 (P.2, P.3)")

        # 3. 測試單頁手動通過端點: /api/tasks/{id}/pages/2/pass
        res = await client.post(f"/api/tasks/{test_task_id}/pages/2/pass", json={"text": "手動校對通過之內容"})
        assert res.status_code == 200
        pass_res = res.json()
        assert pass_res["status"] == "ok"
        assert pass_res["updated_pages"] == [2]
        assert pass_res["is_all_completed"] is False
        assert pass_res["completed_count"] == 2
        assert pass_res["task_status"] == "paused" # 尚有一頁未完成
        print("   ✅ [2] 第 2 頁單頁手動通過成功: 狀態轉為 completed (綠色), 任務維持 paused")

        # 驗證第 2 頁資料庫狀態與文字
        res = await client.get(f"/api/tasks/{test_task_id}")
        data = res.json()
        p2 = next(p for p in data["pages"] if p["page_num"] == 2)
        assert p2["status"] == "completed"
        assert p2["ocr_text"] == "手動校對通過之內容"
        assert p2["error_message"] == ""
        assert data["task"]["uncompleted_pages_count"] == 1
        assert data["task"]["uncompleted_pages"] == [3]
        print("   ✅ [3] 第 2 頁狀態確認為 completed，錯誤訊息清空，剩餘未完成頁: [3]")

        # 4. 測試手動通過最後一頁 (P.3)，且不填入自訂文字 (驗證自動補填預設標記，避免空值未完成)
        # 並且驗證: 當 100% 完成時，自動檢查並將任務狀態切換為 completed (已完成)！
        res = await client.post(f"/api/tasks/{test_task_id}/pages/3/pass")
        assert res.status_code == 200
        pass_res3 = res.json()
        assert pass_res3["status"] == "ok"
        assert pass_res3["updated_pages"] == [3]
        assert pass_res3["is_all_completed"] is True
        assert pass_res3["completed_count"] == 3
        assert pass_res3["total_pages"] == 3
        assert pass_res3["task_status"] == "completed"
        print("   ✅ [4] 第 3 頁手動通過成功: 觸發 100% 完成自動檢查，任務自動移動至 completed (已完成)！")

        # 驗證資料庫中的任務狀態已變為 completed，processed_pages = 3
        res = await client.get(f"/api/tasks/{test_task_id}")
        data = res.json()
        assert data["task"]["status"] == "completed"
        assert data["task"]["processed_pages"] == 3
        assert data["task"]["uncompleted_pages_count"] == 0
        assert data["task"]["uncompleted_pages"] == []
        p3 = next(p for p in data["pages"] if p["page_num"] == 3)
        assert p3["status"] == "completed"
        assert p3["ocr_text"] == "[人工確認通過]"
        print("   ✅ [5] 詳情端點驗證通過: 任務 status='completed', processed_pages=3, 未完成數=0 (全數通過)")

        # 5. 測試批次通過端點 /api/tasks/{id}/pass-pages (mode="custom" 與 mode="all_uncompleted")
        test_task_2 = f"test_pass_batch_{int(time.time())}"
        await database.create_task(
            task_id=test_task_2,
            filename="test_batch.pdf",
            filepath="/tmp/test_batch.pdf",
            status="processing",
            model="gemini-2.5-flash",
            lang="traditional",
            direction="auto",
            column="auto",
            start_page=1,
            end_page=5,
            pdf_total_pages=5
        )
        p_data = [{"task_id": test_task_2, "page_num": i, "image_path": f"/tmp/{i}.png", "status": "failed"} for i in range(1, 6)]
        await database.create_task_pages(p_data)

        # 批次指定範圍通過 (1, 3-4)
        res = await client.post(f"/api/tasks/{test_task_2}/pass-pages", json={
            "mode": "custom",
            "page_range": "1, 3-4",
            "text": "自訂批次通過"
        })
        assert res.status_code == 200
        b_res = res.json()
        assert b_res["updated_pages"] == [1, 3, 4]
        assert b_res["is_all_completed"] is False
        print("   ✅ [6] 批次指定範圍 [1, 3, 4] 通過驗證成功")

        # 一鍵通過剩餘所有未完成頁 (mode="all_uncompleted")
        res = await client.post(f"/api/tasks/{test_task_2}/pass-pages", json={
            "mode": "all_uncompleted"
        })
        assert res.status_code == 200
        b_res2 = res.json()
        assert b_res2["updated_pages"] == [2, 5]
        assert b_res2["is_all_completed"] is True
        assert b_res2["task_status"] == "completed"
        print("   ✅ [7] 一鍵所有未完成頁 [2, 5] 通過成功，任務 100% 完成自動轉移至 completed！")

        # 清理測試任務
        await database.delete_task(test_task_id)
        await database.delete_task(test_task_2)
        print("   ✅ 測試任務資源清理完成")

    print("==================================================")
    print("🎉 手動設定通過 (Pass OCR) 所有自動化測試全數通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_manual_pass_tests())
