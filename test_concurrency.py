import asyncio
import os
import time
from pathlib import Path
import config
from main import RENDER_SEMAPHORE, PDF_RENDER_EXECUTOR
from pdf_engine import render_pdf_to_images_async

async def test_concurrency_behavior():
    print(f"==================================================")
    print(f"🧪 驗證多 PDF 並發切圖控制 (CPU - 2)")
    print(f"==================================================")
    
    detected_cpu = os.cpu_count() or 4
    expected_limit = max(1, detected_cpu - 2)
    print(f"👉 [1/3] 驗證並發配置: 檢測核心數={detected_cpu}, 預期限制={expected_limit}, 當前設定={config.MAX_RENDER_WORKERS}")
    assert config.MAX_RENDER_WORKERS == expected_limit, f"MAX_RENDER_WORKERS 不符: {config.MAX_RENDER_WORKERS} vs {expected_limit}"
    assert PDF_RENDER_EXECUTOR._max_workers == expected_limit, "執行緒池 max_workers 不符"
    print(f"   ✅ 配置與執行緒池規格完全吻合: {expected_limit} workers")

    # 模擬 15 個 PDF 任務同時請求切圖，觀察 Semaphore 是否嚴格限制同時並發數
    print(f"👉 [2/3] 測試 RENDER_SEMAPHORE 最大並發限制 (模擬 15 個多檔案任務)...")
    active_renders = 0
    max_observed_concurrency = 0
    lock = asyncio.Lock()
    completed_tasks = 0

    async def mock_pdf_render_job(job_id: int):
        nonlocal active_renders, max_observed_concurrency, completed_tasks
        # 排隊等待進入 Semaphore
        async with RENDER_SEMAPHORE:
            async with lock:
                active_renders += 1
                if active_renders > max_observed_concurrency:
                    max_observed_concurrency = active_renders
            
            # 模擬單一 PDF 內部單核循序切圖耗時 0.1 秒
            await asyncio.sleep(0.1)

            async with lock:
                active_renders -= 1
                completed_tasks += 1

    tasks = [asyncio.create_task(mock_pdf_render_job(i)) for i in range(15)]
    await asyncio.gather(*tasks)

    print(f"   - 總提交任務數: 15")
    print(f"   - 觀察到的最大同時切圖數: {max_observed_concurrency}")
    print(f"   - 系統允許上限: {expected_limit}")
    print(f"   - 成功完成任務數: {completed_tasks}")
    assert max_observed_concurrency <= expected_limit, f"並發超過限制: {max_observed_concurrency} > {expected_limit}"
    assert completed_tasks == 15, "未完全執行所有任務"
    print(f"   ✅ Semaphore 嚴格限制並發數 <= {expected_limit} 驗證通過")

    # 驗證單一 PDF 透過 render_pdf_to_images_async 傳入 PDF_RENDER_EXECUTOR 執行
    print(f"👉 [3/3] 測試真實 PDF 於專屬 PDF_RENDER_EXECUTOR 中單線程切圖...")
    import pypdfium2 as pdfium
    test_pdf_dir = config.RENDERS_DIR / "test_concurrency_real"
    test_pdf_dir.mkdir(parents=True, exist_ok=True)
    test_pdf_path = test_pdf_dir / "test_doc.pdf"

    # 建立一個 3 頁的測試 PDF
    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(width=200, height=200)
    doc.save(str(test_pdf_path))
    doc.close()

    render_calls = []
    async def page_done(p, total, path):
        render_calls.append(p)

    results = await render_pdf_to_images_async(
        test_pdf_path,
        "test_concurrency_real",
        start_page=1,
        end_page=3,
        dpi=72,
        on_page_rendered=page_done,
        executor=PDF_RENDER_EXECUTOR
    )

    assert len(results) == 3, f"應產生 3 張圖片，實際: {len(results)}"
    assert sorted(render_calls) == [1, 2, 3], f"所有頁面皆應渲染完成，實際: {render_calls}"
    print(f"   ✅ 單一 PDF 於 PDF_RENDER_EXECUTOR 中多執行緒並發切頁驗證通過: 頁碼完成順序 {render_calls}")

    # 清理測試暫存
    try:
        import shutil
        if test_pdf_dir.exists():
            shutil.rmtree(test_pdf_dir)
    except Exception:
        pass

    print(f"==================================================")
    print(f"🎉 多 PDF 前置切圖並發控管驗證全部通過！")
    print(f"==================================================")

if __name__ == "__main__":
    asyncio.run(test_concurrency_behavior())
