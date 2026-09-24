import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport

import config
import database
import scheduler
from scheduler import AccountScheduler, calculate_free_quota_reset_info
import main

class TestQuotaExhaustionAlert(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_data_dir = config.DATA_DIR
        self.orig_db_path = database.DB_PATH
        config.DATA_DIR = Path(self.test_dir)
        database.DB_PATH = Path(self.test_dir) / "test_ocr.db"
        await database.init_db()
        await database.init_admin_password("admin123")

    async def asyncTearDown(self):
        config.DATA_DIR = self.orig_data_dir
        database.DB_PATH = self.orig_db_path
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_calculate_free_quota_reset_info(self):
        info = calculate_free_quota_reset_info()
        self.assertIn("remaining_seconds", info)
        self.assertIn("reset_time_pt", info)
        self.assertIn("reset_time_local", info)
        self.assertIn("reset_time_display", info)
        self.assertIn("remaining_formatted", info)
        self.assertGreater(info["remaining_seconds"], 0)
        self.assertLessEqual(info["remaining_seconds"], 86400)
        print(f"✅ Quota reset info verified: {info['reset_time_display']}, 剩餘: {info['remaining_formatted']}")

    async def test_consecutive_quota_exhaustion_multi_keys(self):
        # 建立兩個免費金鑰帳號與一個付費金鑰帳號
        id1 = await database.add_api_key_account("Free Key 1", "KEY_FREE_1", is_paid=0)
        id2 = await database.add_api_key_account("Free Key 2", "KEY_FREE_2", is_paid=0)
        id_paid = await database.add_api_key_account("Paid Key 1", "KEY_PAID_1", is_paid=1)

        sched = AccountScheduler()

        # 初始狀態：皆未耗盡
        self.assertFalse(await sched.is_all_free_quota_exhausted())

        # Account 1 遭遇第 1 次 429
        self.assertFalse(await sched.report_quota_exhausted(id1))
        # Account 1 遭遇第 2 次 429
        self.assertFalse(await sched.report_quota_exhausted(id1))
        # Account 1 遭遇第 3 次 429 (但 Account 2 尚未達 3 次)
        self.assertFalse(await sched.report_quota_exhausted(id1))
        self.assertFalse(await sched.is_all_free_quota_exhausted())

        # Account 2 遭遇第 1 次 429
        self.assertFalse(await sched.report_quota_exhausted(id2))
        # Account 2 遭遇第 2 次 429
        self.assertFalse(await sched.report_quota_exhausted(id2))
        # Account 2 遭遇第 3 次 429 ->此時免費池所有帳號 (id1, id2) 皆已連續 3 次額度用盡！
        all_exhausted = await sched.report_quota_exhausted(id2)
        self.assertTrue(all_exhausted, "所有免費金鑰皆連續 3 次 429，應回傳 True")
        self.assertTrue(await sched.is_all_free_quota_exhausted())

        # 查詢全域狀態摘要
        status = await sched.get_quota_status()
        self.assertTrue(status["all_free_exhausted"])
        self.assertEqual(status["free_accounts_count"], 2)
        self.assertEqual(status["consecutive_counts"][str(id1)], 3)
        self.assertEqual(status["consecutive_counts"][str(id2)], 3)
        self.assertIn("reset_info", status)

        # 若其中一組金鑰呼叫成功 (report_success)，重置該金鑰連續錯誤次數
        await sched.report_success(id1)
        self.assertFalse(await sched.is_all_free_quota_exhausted(), "Account 1 成功後不再是全部耗盡")

        # 再次測試 reset_quota_tracking
        await sched.report_quota_exhausted(id1)
        await sched.report_quota_exhausted(id1)
        await sched.report_quota_exhausted(id1)
        self.assertTrue(await sched.is_all_free_quota_exhausted())

        await sched.reset_quota_tracking()
        self.assertFalse(await sched.is_all_free_quota_exhausted())

    async def test_quota_api_endpoints(self):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 建立一個免費帳號
            acc_id = await database.add_api_key_account("Test Free", "FREE_KEY", is_paid=0)

            # 初始查詢
            resp = await ac.get("/api/quota/status")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["all_free_exhausted"])

            # 模擬連續觸發 3 次
            await main.scheduler.report_quota_exhausted(acc_id)
            await main.scheduler.report_quota_exhausted(acc_id)
            await main.scheduler.report_quota_exhausted(acc_id)

            resp2 = await ac.get("/api/quota/status")
            data2 = resp2.json()
            self.assertTrue(data2["all_free_exhausted"])
            self.assertIn("reset_time_display", data2["reset_info"])

            # 呼叫重置 API
            reset_resp = await ac.post("/api/quota/reset-tracking")
            self.assertEqual(reset_resp.status_code, 200)

            resp3 = await ac.get("/api/quota/status")
            data3 = resp3.json()
            self.assertFalse(data3["all_free_exhausted"])

    async def test_run_page_ocr_auto_pauses_when_all_free_exhausted(self):
        # 建立免費帳號與任務
        acc_id = await database.add_api_key_account("Free Acc", "FREE_KEY_OCR", is_paid=0)
        task_id = "test_quota_task"
        await database.create_task(
            task_id=task_id,
            filename="sample.pdf",
            filepath="/tmp/sample.pdf",
            model="gemini-3.5-flash-lite",
            lang="traditional",
            direction="auto",
            column="auto",
            custom_prompt="",
            is_paid=0
        )
        img_path = Path(self.test_dir) / "p1.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

        await database.create_task_pages([{"task_id": task_id, "page_num": 1, "image_path": str(img_path)}])

        # 重置排程器追蹤
        await main.scheduler.reset_quota_tracking()

        # 模擬呼叫 Gemini OCR 回傳 429
        mock_ocr = AsyncMock(return_value=(False, "RESOURCE_EXHAUSTED: Quota exceeded for quota metric", 429))

        with patch("main.call_gemini_ocr", mock_ocr):
            # 前兩次重試
            await main.scheduler.report_quota_exhausted(acc_id)
            await main.scheduler.report_quota_exhausted(acc_id)

            # 第 3 次由 run_page_ocr 觸發
            await main.run_page_ocr(
                task_id=task_id,
                page_num=1,
                image_path=img_path,
                model="gemini-3.5-flash-lite",
                prompt="OCR"
            )

        # 驗證任務與頁面是否被自動標記為 paused，且錯誤訊息包含配額重置提示
        task = await database.get_task(task_id)
        pages = await database.get_task_pages(task_id)
        p1 = pages[0]

        self.assertEqual(task["status"], "paused", "全域免費配額耗盡時任務應自動標記為 paused")
        self.assertEqual(p1["status"], "paused", "頁面應被安全暫停")
        self.assertIn("免費配額耗盡", p1["error_message"])
        self.assertIn("重置", p1["error_message"])
        print(f"✅ Auto-pause verified. Page message: {p1['error_message']}")

if __name__ == "__main__":
    unittest.main()
