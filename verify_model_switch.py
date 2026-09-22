import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import config
import database
import main

async def run_verification():
    print("==================================================================")
    print("🔬 開始驗證：OCR 任務暫停後更換模型，並確認後續頁面成功切換新模型")
    print("==================================================================")

    # 1. 初始化資料庫
    await database.init_db()

    # 2. 準備測試 PDF (使用 sample_test.pdf)
    sample_pdf = config.DATA_DIR / "sample_test.pdf"
    assert sample_pdf.exists(), f"測試 PDF 不存在: {sample_pdf}"

    task_id = "test_model_switch_task"
    # 清理舊測試資料
    await database.delete_task(task_id)

    # 3. 建立測試任務，初始模型為 gemini-2.5-flash
    initial_model = "gemini-2.5-flash"
    switched_model = "gemini-2.0-flash"

    print(f"\n[步驟 1] 建立測試任務 {task_id}，初始模型設定為: 【{initial_model}】")
    await database.create_task(
        task_id=task_id,
        filename="verify_doc.pdf",
        filepath=str(sample_pdf),
        model=initial_model,
        lang="traditional",
        direction="auto",
        column="auto",
        start_page=1,
        end_page=2
    )

    # 渲染第 1 與第 2 頁圖片
    from pdf_engine import render_pdf_to_images
    rendered = render_pdf_to_images(sample_pdf, task_id, start_page=1, end_page=2)
    pages_data = [
        {"task_id": task_id, "page_num": p_num, "image_path": str(img_path)}
        for p_num, img_path in rendered
    ]
    await database.create_task_pages(pages_data)
    await database.update_task_status(task_id, "processing", total_pages=2, processed_pages=0)

    # 4. 追蹤每次 call_gemini_ocr 呼叫時傳入的模型名稱
    called_models_per_page = {}

    async def mock_call_gemini_ocr(account, image_path, model, prompt):
        # 從 image_path 判斷頁碼
        p_name = Path(image_path).name
        called_models_per_page[p_name] = model
        print(f"   📡 [API 呼叫模擬] 正在對 {p_name} 發送請求，使用模型: 【{model}】")
        return True, f"這是使用模型 {model} 辨識出來的文字內容。", 200

    # 5. 執行第 1 頁 OCR（模擬正常初次執行）
    print(f"\n[步驟 2] 開始執行第 1 頁 OCR...")
    with patch("main.call_gemini_ocr", side_effect=mock_call_gemini_ocr):
        pages = await database.get_task_pages(task_id)
        p1 = pages[0]
        current_task = await database.get_task(task_id)
        model_to_use = current_task.get("model")
        prompt = main.build_ocr_prompt("traditional", "auto", "auto", "")
        await main.run_page_ocr(task_id, p1["page_num"], Path(p1["image_path"]), model_to_use, prompt)

    # 驗證第 1 頁辨識紀錄
    pages = await database.get_task_pages(task_id)
    assert pages[0]["status"] == "completed"
    assert pages[0]["used_model"] == initial_model, f"第 1 頁模型應為 {initial_model}，實際為 {pages[0]['used_model']}"
    print(f"   ✅ 第 1 頁轉譯完成！資料庫紀錄 used_model = 【{pages[0]['used_model']}】")

    # 6. 使用者點擊「暫停」
    print(f"\n[步驟 3] 使用者按下「暫停轉譯」")
    await database.update_task_status(task_id, "paused")
    task_after_pause = await database.get_task(task_id)
    assert task_after_pause["status"] == "paused"
    print(f"   ✅ 任務成功進入暫停狀態: status = 【{task_after_pause['status']}】")

    # 7. 使用者在暫停狀態下更換模型為 gemini-2.0-flash
    print(f"\n[步驟 4] 使用者在介面更換模型為: 【{switched_model}】")
    await database.update_task_model(task_id, switched_model)
    task_after_model_change = await database.get_task(task_id)
    assert task_after_model_change["model"] == switched_model, f"任務模型應變更為 {switched_model}"
    print(f"   ✅ 資料庫中任務模型已更新為: model = 【{task_after_model_change['model']}】")

    # 8. 使用者點擊「繼續」續傳
    print(f"\n[步驟 5] 使用者按下「繼續轉譯」，觸發流水線續跑...")
    # 呼叫 process_task_pipeline (模擬 resume 後的執行)
    with patch("main.call_gemini_ocr", side_effect=mock_call_gemini_ocr):
        await main.process_task_pipeline(task_id)

    # 9. 驗證所有頁面結果
    print(f"\n[步驟 6] 嚴格檢驗每頁轉譯所使用的模型...")
    final_task = await database.get_task(task_id)
    final_pages = await database.get_task_pages(task_id)

    print(f"   • 任務最終狀態: {final_task['status']}")
    print(f"   • 任務最終模型: {final_task['model']}")
    print(f"   • 第 1 頁使用模型: {final_pages[0]['used_model']}")
    print(f"   • 第 2 頁使用模型: {final_pages[1]['used_model']}")

    # 斷言檢驗
    assert final_task["status"] == "completed", "任務應順暢接續完成"
    assert final_task["model"] == switched_model, f"任務模型應為 {switched_model}"
    assert final_pages[0]["used_model"] == initial_model, f"第 1 頁應保持初次執行的 {initial_model}"
    assert final_pages[1]["used_model"] == switched_model, f"第 2 頁必須使用切換後的全新模型 {switched_model}"
    assert final_pages[0]["used_model"] != final_pages[1]["used_model"], "第 1 頁與第 2 頁的模型必須不同！"

    # 清理測試資料
    await database.delete_task(task_id)

    print("\n==================================================================")
    print("🎉 驗證完全成功！")
    print(f"   1. 第 1 頁使用模型: {initial_model}")
    print(f"   2. 暫停並成功更換為: {switched_model}")
    print(f"   3. 第 2 頁成功使用新模型: {switched_model} 進行轉譯！")
    print("==================================================================")

if __name__ == "__main__":
    asyncio.run(run_verification())
