import asyncio
import os
import shutil
from pathlib import Path
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

import config
import database
from scheduler import scheduler
import gemini_ocr
import exporters

async def test_database_and_scheduler():
    print("👉 [1/4] 測試資料庫初始化與帳號調度...")
    await database.init_db()
    
    # 測試新增 API Key 帳號
    acc_id_1 = await database.add_api_key_account("測試帳號 1", "AIzaSyFakeKey1", rpm_limit=15)
    acc_id_2 = await database.add_api_key_account("測試帳號 2", "AIzaSyFakeKey2", rpm_limit=15)
    
    accounts = await database.get_accounts()
    assert len(accounts) >= 2, "帳號數量應至少有 2 個"
    print(f"   ✅ 帳號資料庫 CRUD 成功，目前共有 {len(accounts)} 組帳號")
    
    # 測試排程器取得可用帳號
    chosen_1 = await scheduler.get_next_available_account()
    assert chosen_1 is not None, "應能取得可用帳號"
    print(f"   ✅ 排程器成功選中帳號: {chosen_1['name']}")
    
    # 測試 429 冷卻機制
    await scheduler.report_rate_limited(chosen_1["id"], cooldown_seconds=2.0)
    
    # 下一次應該自動切換到另一個帳號
    chosen_2 = await scheduler.get_next_available_account()
    assert chosen_2 is not None and chosen_2["id"] != chosen_1["id"], "429 後應自動避讓並切換至不同帳號"
    print(f"   ✅ 429 避讓切換成功，順暢切換至: {chosen_2['name']}")
    
    # 清理測試帳號
    await database.delete_account(acc_id_1)
    await database.delete_account(acc_id_2)
    print("   ✅ 測試帳號清理完畢")

def create_sample_pdf(pdf_path: Path):
    """使用 Pillow 生成兩頁圖片並儲存為 PDF 作為測試文件"""
    images = []
    for i in range(1, 3):
        img = Image.new("RGB", (600, 800), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 580, 780], outline=(0, 0, 0), width=2)
        draw.text((50, 50), f"Sample Document Page {i}", fill=(0, 0, 0))
        draw.text((50, 100), "Google AI Studio OCR Test", fill=(0, 0, 200))
        images.append(img)
    images[0].save(pdf_path, "PDF", resolution=100.0, save_all=True, append_images=images[1:])

def test_pdf_engine():
    print("👉 [2/4] 測試 PDF 逐頁渲染引擎 (pypdfium2)...")
    from pdf_engine import render_pdf_to_images, get_pdf_page_count
    
    test_pdf_path = config.DATA_DIR / "sample_test.pdf"
    create_sample_pdf(test_pdf_path)
    
    page_count = get_pdf_page_count(test_pdf_path)
    assert page_count == 2, f"預期 2 頁，實際 {page_count} 頁"
    
    rendered = render_pdf_to_images(test_pdf_path, "test_task", dpi=150)
    assert len(rendered) == 2, f"預設全本渲染輸出應為 2 頁，實際 {len(rendered)} 頁"
    for p_num, img_path in rendered:
        assert img_path.exists(), f"渲染圖片 {img_path} 應存在"
        print(f"   ✅ 全本第 {p_num} 頁渲染成功: {img_path.name}")
        
    # 測試頁碼範圍限定 (僅第 1 頁)
    rendered_range = render_pdf_to_images(test_pdf_path, "test_task_range", start_page=1, end_page=1, dpi=150)
    assert len(rendered_range) == 1, f"限定範圍渲染應為 1 頁，實際 {len(rendered_range)} 頁"
    print(f"   ✅ 頁碼範圍限定 (start=1, end=1) 渲染測試通過")

def test_prompt_builder():
    print("👉 [3/4] 測試 OCR 排版提示詞生成...")
    prompt_tc_vert_double = gemini_ocr.build_ocr_prompt(
        lang="traditional", direction="vertical", column="double", custom_prompt="請忽略頁眉"
    )
    assert "繁體中文" in prompt_tc_vert_double
    assert "直排/豎排" in prompt_tc_vert_double
    assert "雙欄" in prompt_tc_vert_double
    assert "請忽略頁眉" in prompt_tc_vert_double
    print("   ✅ 排版提示詞動態注入驗證通過")

def test_exporters():
    print("👉 [4/4] 測試 Markdown, TXT, Word (.docx) 與 ZIP 打包匯出...")
    mock_task = {
        "id": "task_demo_12345",
        "filename": "demo_document.pdf",
        "model": "gemini-2.5-flash",
        "lang_pref": "traditional",
        "direction_pref": "horizontal",
        "column_pref": "double"
    }
    mock_pages = [
        {"page_num": 1, "ocr_text": "# 標題\n第一頁內文段落測試。\n\n| 項目 | 數量 |\n| --- | --- |\n| 蘋果 | 5 |", "image_path": str(config.DATA_DIR / "sample_test.pdf")},
        {"page_num": 2, "ocr_text": "第二頁內文段落測試，這是結尾。", "image_path": str(config.DATA_DIR / "sample_test.pdf")}
    ]
    
    md_file = exporters.export_markdown(mock_task, mock_pages)
    txt_file = exporters.export_txt(mock_task, mock_pages)
    docx_file = exporters.export_docx(mock_task, mock_pages)
    zip_file = exporters.export_zip_bundle(mock_task, mock_pages)
    
    assert md_file.exists(), "Markdown 檔案應產生"
    assert txt_file.exists(), "TXT 檔案應產生"
    assert docx_file.exists(), "Word 檔案應產生"
    assert zip_file.exists(), "ZIP 檔案應產生"
    
    print(f"   ✅ Markdown 產生成功: {md_file.name}")
    print(f"   ✅ TXT 產生成功: {txt_file.name}")
    print(f"   ✅ Word docx 產生成功: {docx_file.name}")
    print(f"   ✅ ZIP 壓縮包打包成功: {zip_file.name} (大小: {zip_file.stat().st_size} bytes)")

async def main():
    print("==================================================")
    print("🧪 開始進行 Google AI Studio OCR 系統自動化測試")
    print("==================================================")
    await test_database_and_scheduler()
    test_pdf_engine()
    test_prompt_builder()
    test_exporters()
    print("==================================================")
    print("🎉 所有單元與整合測試全數通過！")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(main())
