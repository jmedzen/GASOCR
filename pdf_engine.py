import pypdfium2 as pdfium
from pathlib import Path
from typing import List, Tuple
from config import RENDERS_DIR

def get_pdf_page_count(pdf_path: Path) -> int:
    """獲取 PDF 總頁數"""
    pdf = pdfium.PdfDocument(str(pdf_path))
    count = len(pdf)
    pdf.close()
    return count

def render_pdf_to_images(
    pdf_path: Path, 
    task_id: str, 
    start_page: int = 1, 
    end_page: int = 0, 
    dpi: int = 200
) -> List[Tuple[int, Path]]:
    """
    將 PDF 指定範圍頁面逐頁渲染為高品質 PNG 圖片
    scale = dpi / 72. (標準 PDF 基礎是 72 DPI，200 DPI scale 大約 2.77)
    """
    scale = dpi / 72.0
    task_render_dir = RENDERS_DIR / task_id
    task_render_dir.mkdir(parents=True, exist_ok=True)
    
    pdf = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(pdf)
    
    s_page = max(1, start_page)
    e_page = min(total_pages, end_page) if (end_page and end_page > 0) else total_pages
    if s_page > e_page:
        s_page = 1
        e_page = total_pages

    results = []
    try:
        for p_num in range(s_page, e_page + 1):
            idx = p_num - 1
            page = pdf[idx]
            # 渲染為 PIL Image
            image = page.render(scale=scale).to_pil()
            
            output_path = task_render_dir / f"page_{p_num:04d}.png"
            image.save(output_path, "PNG", optimize=True)
            results.append((p_num, output_path))
    finally:
        pdf.close()
        
    return results

def render_single_page(pdf_path: Path, page_num: int, output_path: Path, dpi: int = 200) -> Path:
    """單獨渲染指定某一頁（1-indexed）"""
    scale = dpi / 72.0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        idx = page_num - 1
        if 0 <= idx < len(pdf):
            page = pdf[idx]
            image = page.render(scale=scale).to_pil()
            image.save(output_path, "PNG", optimize=True)
        else:
            raise IndexError(f"Page {page_num} out of bounds (1-{len(pdf)})")
    finally:
        pdf.close()
        
    return output_path
