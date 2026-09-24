import asyncio
import os
import time
import uuid
import shutil
from pathlib import Path
from unittest.mock import patch
import pypdfium2 as pdfium
import config
import database
from scheduler import free_ocr_task_manager, scheduler
from main import process_task_pipeline, app
from httpx import AsyncClient, ASGITransport

async def test_free_ocr_task_concurrency():
    print("==================================================")
    print("🧪 驗證免費 API 依金鑰數量動態控管 OCR 任務並發數")
    print("   - pool keys <= 2: 最多 1 個 OCR 任務")
    print("   - pool keys >= 3: 最多 2 個 OCR 任務")
    print("   - paid tasks: 不受此限")
    print("==================================================")
    await database.init_db()

    # 清空測試暫存帳號
    test_acc_ids = []

    try:
        # 1. 測試 get_max_concurrent() 演算法
        print("👉 [1/5] 測試金鑰數量對應之並發上限演算法...")
        
        # 模擬 1 把金鑰 -> 應回傳 1
        with patch.object(free_ocr_task_manager, "get_free_keys_count", return_value=1):
            assert await free_ocr_task_manager.get_max_concurrent() == 1
        # 模擬 2 把金鑰 -> 應回傳 1
        with patch.object(free_ocr_task_manager, "get_free_keys_count", return_value=2):
            assert await free_ocr_task_manager.get_max_concurrent() == 1
        # 模擬 3 把金鑰 -> 應回傳 2
        with patch.object(free_ocr_task_manager, "get_free_keys_count", return_value=3):
            assert await free_ocr_task_manager.get_max_concurrent() == 2
        # 模擬 5 把金鑰 -> 應回傳 2
        with patch.object(free_ocr_task_manager, "get_free_keys_count", return_value=5):
            assert await free_ocr_task_manager.get_max_concurrent() == 2
        print("   ✅ 演算法驗證通過：<= 2 keys -> 1 task, >= 3 keys -> 2 tasks")

        # 2. 測試當金鑰數 <= 2 時，最多僅能 1 個免費 OCR 任務獲取槽位
        print("👉 [2/5] 測試金鑰數 <= 2 時之槽位排隊互斥行為...")
        with patch.object(free_ocr_task_manager, "get_max_concurrent", return_value=1):
            task_a = f"task_free_a_{uuid.uuid4().hex[:6]}"
            task_b = f"task_free_b_{uuid.uuid4().hex[:6]}"

            acquired_a = False
            acquired_b = False

            async def job_a():
                nonlocal acquired_a
                async with free_ocr_task_manager.limit(task_a, is_paid=False):
                    acquired_a = True
                    await asyncio.sleep(0.3)

            async def job_b():
                nonlocal acquired_b
                async with free_ocr_task_manager.limit(task_b, is_paid=False):
                    acquired_b = True

            t_a = asyncio.create_task(job_a())
            # 等待 A 取得槽位
            await asyncio.sleep(0.05)
            assert acquired_a is True
            assert task_a in free_ocr_task_manager.active_tasks
            print("   ✅ Task A 已成功佔有唯一槽位 (1/1)")

            t_b = asyncio.create_task(job_b())
            # 稍候檢查 B 是否在等待
            await asyncio.sleep(0.05)
            assert acquired_b is False, "Task B 不應在 Task A 執行時取得槽位"
            assert task_b not in free_ocr_task_manager.active_tasks
            print("   ✅ Task B 成功於佇列中排隊等待 Task A 釋放")

            # 等待 A 完成
            await t_a
            # A 結束後，B 應隨即取得槽位並完成
            await asyncio.sleep(0.05)
            assert acquired_b is True
            await t_b
            print("   ✅ Task A 釋放後，Task B 成功取得槽位並執行完成")

        # 3. 測試當金鑰數 >= 3 時，最多能 2 個免費 OCR 任務並發執行
        print("👉 [3/5] 測試金鑰數 >= 3 時最多允許 2 個任務並發...")
        with patch.object(free_ocr_task_manager, "get_max_concurrent", return_value=2):
            task_1 = f"task_free_1_{uuid.uuid4().hex[:6]}"
            task_2 = f"task_free_2_{uuid.uuid4().hex[:6]}"
            task_3 = f"task_free_3_{uuid.uuid4().hex[:6]}"

            t1_run = False
            t2_run = False
            t3_run = False

            async def job_1():
                nonlocal t1_run
                async with free_ocr_task_manager.limit(task_1, is_paid=False):
                    t1_run = True
                    await asyncio.sleep(0.3)

            async def job_2():
                nonlocal t2_run
                async with free_ocr_task_manager.limit(task_2, is_paid=False):
                    t2_run = True
                    await asyncio.sleep(0.3)

            async def job_3():
                nonlocal t3_run
                async with free_ocr_task_manager.limit(task_3, is_paid=False):
                    t3_run = True

            c1 = asyncio.create_task(job_1())
            c2 = asyncio.create_task(job_2())
            await asyncio.sleep(0.05)
            assert t1_run is True and t2_run is True
            assert len(free_ocr_task_manager.active_tasks) == 2
            print("   ✅ Task 1 與 Task 2 同時取得免費槽位並發執行中 (2/2)")

            c3 = asyncio.create_task(job_3())
            await asyncio.sleep(0.05)
            assert t3_run is False
            assert task_3 not in free_ocr_task_manager.active_tasks
            print("   ✅ 槽位滿載 (2/2)，Task 3 成功於佇列中排隊")

            await asyncio.gather(c1, c2)
            await asyncio.sleep(0.05)
            assert t3_run is True
            await c3
            print("   ✅ 槽位釋出後，Task 3 成功接續執行完成")

        # 4. 測試付費任務 (is_paid=True) 不受免費限制
        print("👉 [4/5] 測試付費任務 (is_paid=True) 完全不受免費槽位限制...")
        with patch.object(free_ocr_task_manager, "get_max_concurrent", return_value=1):
            task_free = f"task_free_{uuid.uuid4().hex[:6]}"
            task_paid = f"task_paid_{uuid.uuid4().hex[:6]}"
            free_running = False
            paid_running = False

            async def free_worker():
                nonlocal free_running
                async with free_ocr_task_manager.limit(task_free, is_paid=False):
                    free_running = True
                    await asyncio.sleep(0.3)

            async def paid_worker():
                nonlocal paid_running
                async with free_ocr_task_manager.limit(task_paid, is_paid=True):
                    paid_running = True

            wf = asyncio.create_task(free_worker())
            await asyncio.sleep(0.05)
            assert free_running is True

            # 即使免費槽位已滿 (1/1)，付費任務也能立刻執行
            wp = asyncio.create_task(paid_worker())
            await asyncio.sleep(0.05)
            assert paid_running is True
            print("   ✅ 付費任務立即取得執行通道，不受免費金鑰槽位阻擋")

            await wf
            await wp

        # 5. 測試動態金鑰增減喚醒 (動態擴容)
        print("👉 [5/5] 測試排隊中動態新增金鑰觸發槽位擴充喚醒...")
        current_mock_keys = 2
        def dynamic_keys():
            return current_mock_keys

        with patch.object(free_ocr_task_manager, "get_free_keys_count", side_effect=dynamic_keys):
            t_hold = f"task_dyn_1_{uuid.uuid4().hex[:6]}"
            t_wait = f"task_dyn_2_{uuid.uuid4().hex[:6]}"
            wait_acquired = False

            async def hold_worker():
                async with free_ocr_task_manager.limit(t_hold, is_paid=False):
                    await asyncio.sleep(0.5)

            async def wait_worker():
                nonlocal wait_acquired
                async with free_ocr_task_manager.limit(t_wait, is_paid=False):
                    wait_acquired = True

            th = asyncio.create_task(hold_worker())
            await asyncio.sleep(0.05)

            tw = asyncio.create_task(wait_worker())
            await asyncio.sleep(0.05)
            assert wait_acquired is False
            print("   ✅ 目前 2 把金鑰：第 2 個任務正等待中...")

            # 模擬使用者在 UI 新增第 3 把金鑰
            current_mock_keys = 3
            await free_ocr_task_manager.notify_account_change()
            await asyncio.sleep(0.05)
            # wait_worker 應立即被喚醒並取得第 2 個槽位！
            assert wait_acquired is True
            print("   ✅ 金鑰擴充為 3 把後，等待中的任務立即被喚醒並並發執行！")

            await th
            await tw

    finally:
        pass

    print("==================================================")
    print("🎉 免費 API 金鑰池並發控管驗證全數通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(test_free_ocr_task_concurrency())
