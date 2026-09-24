import asyncio
import os
import shutil
from pathlib import Path
import config
import database
import main

async def run_tests():
    print("==================================================")
    print("🧪 驗證任務點擊繼續時切圖狀態與 threads 滿載排隊邏輯")
    print("   - 切圖未完成 + threads 滿載 -> 顯示「等待中」(pending)")
    print("   - 切圖未完成 + threads 未滿 -> 顯示「切圖中」(rendering)")
    print("   - 切圖已完成 -> 進入 OCR (processing / OCR pending)")
    print("==================================================")
    
    await database.init_db()

    task_id_1 = "test_res_t1"
    task_id_2 = "test_res_t2"
    task_id_3 = "test_res_t3"

    # 清理 DB
    async with database.get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id IN (?, ?, ?)", (task_id_1, task_id_2, task_id_3))
        await db.execute("DELETE FROM task_pages WHERE task_id IN (?, ?, ?)", (task_id_1, task_id_2, task_id_3))
        await db.commit()

    # 1. 建立未完成切圖的任務 t1
    await database.create_task(
        task_id=task_id_1,
        filename="test1.pdf",
        filepath="/tmp/test1.pdf",
        model="gemini-3.5-flash-lite",
        lang="traditional",
        direction="auto",
        column="auto",
        start_page=1,
        end_page=5,
        pdf_total_pages=5
    )
    await database.update_task_status(task_id_1, status="paused", total_pages=5, rendered_pages=2)

    # 驗證 is_task_rendering_completed(t1) 應為 False
    t1_data = await database.get_task(task_id_1)
    is_done_t1 = main.is_task_rendering_completed(t1_data)
    assert is_done_t1 is False, f"預期 False，但得到 {is_done_t1}"
    print("👉 [1/4] 測試 is_task_rendering_completed 判斷切圖未完成... ✅ 正確回傳 False")

    # 2. 模擬 threads 滿載 (鎖定 RENDER_SEMAPHORE) 下點擊繼續
    # 先把 RENDER_SEMAPHORE 的 permits 耗盡
    sem_permits = []
    while main.RENDER_SEMAPHORE._value > 0:
        await main.RENDER_SEMAPHORE.acquire()
        sem_permits.append(True)

    try:
        assert main.RENDER_SEMAPHORE.locked() is True
        print(f"   ℹ️ 已模擬 RENDER_SEMAPHORE 滿載 (剩餘槽位: {main.RENDER_SEMAPHORE._value})")
        
        # 調用 resume_task
        res = await main.resume_task(task_id_1)
        print(f"   ▶️ 滿載下 resume_task 回傳: {res}")
        assert res["task_status"] == "pending", f"預期 task_status 為 pending，但得到 {res['task_status']}"
        
        t1_after = await database.get_task(task_id_1)
        assert t1_after["status"] == "pending", f"預期 DB 狀態為 pending，但得到 {t1_after['status']}"
        print("👉 [2/4] 測試切圖未完成 + threads 滿載 -> 成功設定並顯示為「等待中」(pending) ✅ 通過")
        main.cancel_task_active_jobs(task_id_1)
    finally:
        # 釋放 permits
        for _ in sem_permits:
            main.RENDER_SEMAPHORE.release()

    # 3. 測試 threads 未滿載下點擊繼續
    # 將 t1 設回 paused
    await database.update_task_status(task_id_1, status="paused")
    assert main.RENDER_SEMAPHORE.locked() is False
    res2 = await main.resume_task(task_id_1)
    print(f"   ▶️ 未滿載下 resume_task 回傳: {res2}")
    assert res2["task_status"] == "rendering", f"預期 task_status 為 rendering，但得到 {res2['task_status']}"
    print("👉 [3/4] 測試切圖未完成 + threads 未滿 -> 正確設定為「切圖中」(rendering) ✅ 通過")
    main.cancel_task_active_jobs(task_id_1)

    # 4. 測試切圖已完成的任務點擊繼續 (建立 t2 並偽造完整圖片)
    t2_render_dir = config.RENDERS_DIR / task_id_2
    t2_render_dir.mkdir(parents=True, exist_ok=True)
    for p in range(1, 4):
        (t2_render_dir / f"page_{p:04d}.png").write_bytes(b"dummy png bytes")

    await database.create_task(
        task_id=task_id_2,
        filename="test2.pdf",
        filepath="/tmp/test2.pdf",
        model="gemini-3.5-flash-lite",
        lang="traditional",
        direction="auto",
        column="auto",
        start_page=1,
        end_page=3,
        pdf_total_pages=3
    )
    await database.update_task_status(task_id_2, status="paused", total_pages=3, rendered_pages=3)
    t2_data = await database.get_task(task_id_2)
    is_done_t2 = main.is_task_rendering_completed(t2_data)
    assert is_done_t2 is True, f"預期 True，但得到 {is_done_t2}"

    # 即使 RENDER_SEMAPHORE 滿載，切圖已完成的任務依然直接進入 OCR
    sem_permits = []
    while main.RENDER_SEMAPHORE._value > 0:
        await main.RENDER_SEMAPHORE.acquire()
        sem_permits.append(True)

    try:
        res3 = await main.resume_task(task_id_2)
        print(f"   ▶️ 切圖已完成任務 resume_task 回傳: {res3}")
        assert res3["task_status"] in ["processing", "pending"]
        print("👉 [4/4] 測試切圖已完成之任務 -> 不受切圖 threads 滿載阻擋，直接進入 OCR 階段 ✅ 通過")
        main.cancel_task_active_jobs(task_id_2)
    finally:
        for _ in sem_permits:
            main.RENDER_SEMAPHORE.release()
        shutil.rmtree(t2_render_dir, ignore_errors=True)

    # 5. 測試全局/批次繼續 (resume_all_tasks) 之槽位分配
    print("👉 [5/5] 測試 resume_all_tasks 批次分配槽位與滿載排隊...")
    main.cancel_task_active_jobs(task_id_1)
    main.cancel_task_active_jobs(task_id_2)
    await asyncio.sleep(0.2)

    task_id_4 = "test_res_t4"
    task_id_5 = "test_res_t5"
    async with database.get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id IN (?, ?)", (task_id_4, task_id_5))
        await db.execute("DELETE FROM task_pages WHERE task_id IN (?, ?)", (task_id_4, task_id_5))
        await db.commit()
    await database.create_task(task_id_4, "test4.pdf", "/tmp/test4.pdf", "gemini-3.5-flash-lite", "traditional", "auto", "auto", start_page=1, end_page=5, pdf_total_pages=5)
    await database.create_task(task_id_5, "test5.pdf", "/tmp/test5.pdf", "gemini-3.5-flash-lite", "traditional", "auto", "auto", start_page=1, end_page=5, pdf_total_pages=5)
    await database.create_task_pages([{"task_id": task_id_4, "page_num": i, "image_path": f"/tmp/p4_{i}.png", "status": "pending"} for i in range(1, 6)])
    await database.create_task_pages([{"task_id": task_id_5, "page_num": i, "image_path": f"/tmp/p5_{i}.png", "status": "pending"} for i in range(1, 6)])
    await database.update_task_status(task_id_4, status="paused", total_pages=5, rendered_pages=1)
    await database.update_task_status(task_id_5, status="paused", total_pages=5, rendered_pages=1)

    # 模擬只留 1 個槽位可用
    sem_permits = []
    while main.RENDER_SEMAPHORE._value > 1:
        await main.RENDER_SEMAPHORE.acquire()
        sem_permits.append(True)

    orig_spawn = main.spawn_background
    def dummy_spawn(coro):
        try:
            coro.close()
        except Exception:
            pass
    main.spawn_background = dummy_spawn

    try:
        assert main.RENDER_SEMAPHORE._value == 1
        res_batch = await main.resume_all_tasks(main.BatchTaskActionRequest(task_ids=[task_id_4, task_id_5]))
        print(f"   ℹ️ res_batch: {res_batch}")
        assert res_batch["resumed_count"] == 2
        
        t4_after = await database.get_task(task_id_4)
        t5_after = await database.get_task(task_id_5)
        print(f"   ℹ️ 批次接續結果: t4={t4_after['status']}, t5={t5_after['status']}")
        statuses = [t4_after['status'], t5_after['status']]
        assert "rendering" in statuses and "pending" in statuses, f"預期一為 rendering 一為 pending，但得到 {statuses}"
        print("   ✅ 批次繼續槽位滿載排隊行為驗證通過！")
        main.cancel_task_active_jobs(task_id_4)
        main.cancel_task_active_jobs(task_id_5)
    finally:
        main.spawn_background = orig_spawn
        for _ in sem_permits:
            main.RENDER_SEMAPHORE.release()
        async with database.get_db() as db:
            await db.execute("DELETE FROM tasks WHERE id IN (?, ?)", (task_id_4, task_id_5))
            await db.execute("DELETE FROM task_pages WHERE task_id IN (?, ?)", (task_id_4, task_id_5))
            await db.commit()

    # 清理 DB
    async with database.get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id IN (?, ?, ?)", (task_id_1, task_id_2, task_id_3))
        await db.execute("DELETE FROM task_pages WHERE task_id IN (?, ?, ?)", (task_id_1, task_id_2, task_id_3))
        await db.commit()

    print("==================================================")
    print("🎉 切圖檢查與 threads 滿載等待中狀態驗證全數通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_tests())
