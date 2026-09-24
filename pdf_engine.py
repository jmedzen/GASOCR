import pypdfium2 as pdfium
from pathlib import Path
from typing import List, Tuple, Set, Optional, Callable, Any
import asyncio
import threading
import concurrent.futures
from config import RENDERS_DIR, DEFAULT_RENDER_DPI, MAX_RENDER_WORKERS

# ══════════════════════════════════════════════════════════════
#  PDFium 全域鎖
#
#  ⚠️ 這是修掉「python 當機」的核心。
#
#  實測（A/B 各 4 次）：
#     渲染並發 = 1  → 4/4 正常結束
#     渲染並發 = 2  → 4/4 SIGSEGV
#
#  faulthandler 追蹤顯示兩個執行緒同時在跑 PDFium：
#     thread pdf_render__0: PIL _save          （存 PNG）
#     thread pdf_render__1: pypdfium2 page.render ★ 在此崩潰
#     C 堆疊: CPDF_RenderStatus::ProcessClipPath → FPDF_RenderPageBitmap
#
#  PDFium（C++）並非執行緒安全；同時間有多個執行緒呼叫它就會隨機 SIGSEGV，
#  整個 Python 行程直接消失 —— 使用者看到的「有時候 python 會當機」、
#  以及任務卡在 rendering 且 rendered_pages 停在 0~1，都是同一個原因。
#
#  因此所有 PDFium 呼叫都必須經過這把鎖。鎖只包住 PDFium 的部分，
#  PNG 編碼（PIL）留在鎖外，仍可與其他執行緒重疊以保留部分吞吐。
# ══════════════════════════════════════════════════════════════
PDFIUM_LOCK = threading.RLock()


def _open_document(pdf_path: Path):
    """開啟 PDF（受 PDFium 鎖保護）"""
    with PDFIUM_LOCK:
        return pdfium.PdfDocument(str(pdf_path))


def _page_count(pdf) -> int:
    with PDFIUM_LOCK:
        return len(pdf)


def _render_page_to_pil(pdf, idx: int, scale: float):
    """把指定頁渲染成 PIL Image（受 PDFium 鎖保護）"""
    with PDFIUM_LOCK:
        page = pdf[idx]
        return page.render(scale=scale).to_pil()


def _close_document(pdf) -> None:
    with PDFIUM_LOCK:
        try:
            pdf.close()
        except Exception:
            pass


def get_pdf_page_count(pdf_path: Path) -> int:
    """獲取 PDF 總頁數"""
    try:
        pdf = _open_document(pdf_path)
        try:
            return _page_count(pdf)
        finally:
            _close_document(pdf)
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
    
    pdf = _open_document(pdf_path)
    total_pages = _page_count(pdf)
    
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
            # PDFium 渲染在鎖內；PNG 編碼在鎖外
            image = _render_page_to_pil(pdf, idx, scale)
            
            output_path = task_render_dir / f"page_{p_num:04d}.png"
            image.save(output_path, "PNG", optimize=True)
            results.append((p_num, output_path))
            if on_page_rendered:
                try:
                    on_page_rendered(p_num, total_target, output_path)
                except Exception as cb_err:
                    print(f"on_page_rendered error: {cb_err}")
    finally:
        _close_document(pdf)
        
    return results


import concurrent.futures

async def render_pdf_to_images_async(
    pdf_path: Path, 
    task_id: str, 
    start_page: int = 1, 
    end_page: int = 0, 
    dpi: int = DEFAULT_RENDER_DPI,
    on_page_rendered: Optional[Callable[[int, int, Path], Any]] = None,
    executor: Optional[concurrent.futures.Executor] = None,
    pages_to_render: Optional[List[int]] = None
) -> List[Tuple[int, Path]]:
    """
    非阻塞逐頁渲染 PDF 為高品質 PNG 圖片，每切完一頁即時呼叫非同步回呼以即時更新進度條。
    支援 pages_to_render 參數（用於暫停後接續切圖剩餘頁面），並具備協程取消與暫停感知。
    """
    scale = dpi / 72.0
    task_render_dir = RENDERS_DIR / task_id
    task_render_dir.mkdir(parents=True, exist_ok=True)
    
    loop = asyncio.get_running_loop()
    pdf = await loop.run_in_executor(executor, _open_document, pdf_path)
    total_pages = await loop.run_in_executor(executor, _page_count, pdf)
    
    if pages_to_render is not None:
        target_list = [p for p in pages_to_render if 1 <= p <= total_pages]
        total_target = max(1, end_page - start_page + 1) if (end_page and end_page >= start_page) else len(target_list)
    else:
        s_page = max(1, start_page)
        e_page = min(total_pages, end_page) if (end_page and end_page > 0) else total_pages
        if s_page > e_page:
            s_page = 1
            e_page = total_pages
        target_list = list(range(s_page, e_page + 1))
        total_target = max(1, e_page - s_page + 1)

    results = []
    # 開放切圖執行緒數量為 CPU cores - 2 (透過 MAX_RENDER_WORKERS 控制同時並發)
    render_sem = asyncio.Semaphore(MAX_RENDER_WORKERS)

    async def _render_page_task(p_num: int):
        async with render_sem:
            idx = p_num - 1
            out_path = task_render_dir / f"page_{p_num:04d}.png"

            def _render_and_save():
                # ★ PDFium 呼叫在鎖內（序列化，避免 SIGSEGV）
                image = _render_page_to_pil(pdf, idx, scale)
                # PNG 編碼在鎖外，可與其他執行緒的 PDFium 工作重疊（充分利用多核心）
                image.save(out_path, "PNG", optimize=True)
                return out_path

            output_path = await loop.run_in_executor(executor, _render_and_save)
            
            if on_page_rendered:
                try:
                    if asyncio.iscoroutinefunction(on_page_rendered):
                        await on_page_rendered(p_num, total_target, output_path)
                    else:
                        on_page_rendered(p_num, total_target, output_path)
                except asyncio.CancelledError:
                    raise
                except Exception as cb_err:
                    print(f"on_page_rendered async error: {cb_err}")

            return (p_num, output_path)

    page_tasks = [asyncio.create_task(_render_page_task(p)) for p in target_list]
    try:
        results = await asyncio.gather(*page_tasks)
        results.sort(key=lambda x: x[0])
    except asyncio.CancelledError:
        for t in page_tasks:
            if not t.done():
                t.cancel()
        raise
    finally:
        await loop.run_in_executor(executor, _close_document, pdf)
        
    return results

def render_single_page(pdf_path: Path, page_num: int, output_path: Path, dpi: int = DEFAULT_RENDER_DPI) -> Path:
    """單獨渲染指定某一頁（1-indexed）"""
    scale = dpi / 72.0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    pdf = _open_document(pdf_path)
    try:
        total = _page_count(pdf)
        idx = page_num - 1
        if 0 <= idx < total:
            image = _render_page_to_pil(pdf, idx, scale)
            image.save(output_path, "PNG", optimize=True)
        else:
            raise IndexError(f"Page {page_num} out of bounds (1-{total})")
    finally:
        _close_document(pdf)
        
    return output_path

def render_remaining_pdf_pages(
    pdf_path: Path, 
    task_id: str, 
    exclude_pages: Set[int], 
    dpi: int = DEFAULT_RENDER_DPI
) -> List[Tuple[int, Path]]:
    """
    在背景將 PDF 中尚未渲染的其餘頁面渲染為高品質 PNG 圖片 (支援 CPU cores - 2 多執行緒並發)
    """
    scale = dpi / 72.0
    task_render_dir = RENDERS_DIR / task_id
    task_render_dir.mkdir(parents=True, exist_ok=True)
    
    pdf = _open_document(pdf_path)
    total_pages = _page_count(pdf)
    target_pages = [p for p in range(1, total_pages + 1) if p not in exclude_pages]
    results = []

    def _render_one(p_num: int):
        idx = p_num - 1
        image = _render_page_to_pil(pdf, idx, scale)
        output_path = task_render_dir / f"page_{p_num:04d}.png"
        image.save(output_path, "PNG", optimize=True)
        return (p_num, output_path)

    try:
        if target_pages:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_RENDER_WORKERS) as pool:
                results = list(pool.map(_render_one, target_pages))
            results.sort(key=lambda x: x[0])
    finally:
        _close_document(pdf)
        
    return results
