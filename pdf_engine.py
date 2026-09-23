import pypdfium2 as pdfium
from pathlib import Path
from typing import List, Tuple, Set, Optional, Callable, Any
import asyncio
from config import RENDERS_DIR, DEFAULT_RENDER_DPI

def get_pdf_page_count(pdf_path: Path) -> int:
    """獲取 PDF 總頁數"""
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
        count = len(pdf)
        pdf.close()
        return count
    except Exception:
        return 0

def render_pdf_to_images(
    pdf_path: Path, 
    task_id: str, 
    start_page: int = 1, 
    end_page: int = 0, 
    dpi: int = DEFAULT_RENDER_DPI,
    on_page_rendered: Optional[Callable[[int, int, Path], Any]] = None
) -> List[Tuple[int, Path]]:
    """
    將 PDF 指定範圍頁面逐頁渲染為高品質 PNG 圖片
    scale = dpi / 72. (標準 PDF 基礎是 72 DPI，300 DPI scale 大約 4.167)
    支援 on_page_rendered(p_num, total_target, output_path) 即時進度回呼
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

    total_target = max(1, e_page - s_page + 1)
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
            if on_page_rendered:
                try:
                    on_page_rendered(p_num, total_target, output_path)
                except Exception as cb_err:
                    print(f"on_page_rendered error: {cb_err}")
    finally:
        pdf.close()
        
    return results

async def render_pdf_to_images_async(
    pdf_path: Path, 
    task_id: str, 
    start_page: int = 1, 
    end_page: int = 0, 
    dpi: int = DEFAULT_RENDER_DPI,
    on_page_rendered: Optional[Callable[[int, int, Path], Any]] = None
) -> List[Tuple[int, Path]]:
    """
    非阻塞逐頁渲染 PDF 為高品質 PNG 圖片，每切完一頁即時呼叫非同步回呼以即時更新進度條
    """
    scale = dpi / 72.0
    task_render_dir = RENDERS_DIR / task_id
    task_render_dir.mkdir(parents=True, exist_ok=True)
    
    pdf = await asyncio.to_thread(pdfium.PdfDocument, str(pdf_path))
    total_pages = len(pdf)
    
    s_page = max(1, start_page)
    e_page = min(total_pages, end_page) if (end_page and end_page > 0) else total_pages
    if s_page > e_page:
        s_page = 1
        e_page = total_pages

    total_target = max(1, e_page - s_page + 1)
    results = []
    try:
        for p_num in range(s_page, e_page + 1):
            idx = p_num - 1
            page = pdf[idx]
            
            def _render_and_save():
                img = page.render(scale=scale).to_pil()
                out_path = task_render_dir / f"page_{p_num:04d}.png"
                img.save(out_path, "PNG", optimize=True)
                return out_path

            output_path = await asyncio.to_thread(_render_and_save)
            results.append((p_num, output_path))
            
            if on_page_rendered:
                try:
                    if asyncio.iscoroutinefunction(on_page_rendered):
                        await on_page_rendered(p_num, total_target, output_path)
                    else:
                        on_page_rendered(p_num, total_target, output_path)
                except Exception as cb_err:
                    print(f"on_page_rendered async error: {cb_err}")
    finally:
        await asyncio.to_thread(pdf.close)
        
    return results

def render_single_page(pdf_path: Path, page_num: int, output_path: Path, dpi: int = DEFAULT_RENDER_DPI) -> Path:
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

def render_remaining_pdf_pages(
    pdf_path: Path, 
    task_id: str, 
    exclude_pages: Set[int], 
    dpi: int = DEFAULT_RENDER_DPI
) -> List[Tuple[int, Path]]:
    """
    在背景將 PDF 中尚未渲染的其餘頁面渲染為高品質 PNG 圖片
    """
    scale = dpi / 72.0
    task_render_dir = RENDERS_DIR / task_id
    task_render_dir.mkdir(parents=True, exist_ok=True)
    
    pdf = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(pdf)
    results = []
    try:
        for p_num in range(1, total_pages + 1):
            if p_num in exclude_pages:
                continue
            idx = p_num - 1
            page = pdf[idx]
            image = page.render(scale=scale).to_pil()
            output_path = task_render_dir / f"page_{p_num:04d}.png"
            image.save(output_path, "PNG", optimize=True)
            results.append((p_num, output_path))
    finally:
        pdf.close()
        
    return results
