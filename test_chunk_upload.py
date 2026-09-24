import io
import os
import hashlib
from pathlib import Path
from fastapi.testclient import TestClient

import config
import database
from main import app

client = TestClient(app)

def test_chunked_upload_and_task_creation():
    print("👉 測試大檔案切片上傳 (Chunked Upload) 與任務建立流程...")
    
    import pypdfium2 as pdfium
    temp_seed_pdf = config.UPLOADS_DIR / "temp_chunk_seed.pdf"
    doc = pdfium.PdfDocument.new()
    doc.new_page(width=100, height=100)
    doc.save(str(temp_seed_pdf))
    doc.close()
    full_content = temp_seed_pdf.read_bytes()
    temp_seed_pdf.unlink(missing_ok=True)
    original_hash = hashlib.sha256(full_content).hexdigest()
    
    upload_id = "test_chunk_upload_999"
    filename = "test_large_document.pdf"
    
    # 分割為多塊 (每塊 300 bytes)
    chunk_size = 300
    total_chunks = (len(full_content) + chunk_size - 1) // chunk_size
    assert total_chunks >= 2, f"應至少分割為 2 塊，實際為 {total_chunks}"
    
    final_res_data = None
    for i in range(total_chunks):
        start = i * chunk_size
        end = min(len(full_content), start + chunk_size)
        chunk_bytes = full_content[start:end]
        
        files = {
            "chunk": (filename, io.BytesIO(chunk_bytes), "application/pdf")
        }
        data = {
            "upload_id": upload_id,
            "chunk_index": i,
            "total_chunks": total_chunks,
            "filename": filename
        }
        
        res = client.post("/api/upload/chunk", data=data, files=files)
        assert res.status_code == 200, f"分片 {i} 上傳應為 200，但為 {res.status_code}: {res.text}"
        res_json = res.json()
        
        if i < total_chunks - 1:
            assert res_json["status"] == "chunk_received"
            assert res_json["chunk_index"] == i
        else:
            assert res_json["status"] == "completed"
            assert res_json["file_hash"] == original_hash
            final_res_data = res_json

    print(f"   ✅ 分片組裝完成，檔案特徵碼正確: {final_res_data['file_hash']}")
    saved_filepath = Path(final_res_data["filepath"])
    assert saved_filepath.exists(), f"伺服器組裝後的檔案應存在於: {saved_filepath}"
    assert saved_filepath.stat().st_size == len(full_content), "組裝後的檔案大小應與原始檔案完全相同"

    # 驗證透過 existing_files 呼叫 /api/tasks 建立任務
    import json
    existing_files_payload = json.dumps([{
        "hash": final_res_data["file_hash"],
        "filename": filename,
        "filepath": str(saved_filepath)
    }])
    
    task_res = client.post("/api/tasks", data={
        "existing_files": existing_files_payload,
        "model": config.DEFAULT_MODEL
    })
    assert task_res.status_code == 200, f"建立任務應為 200: {task_res.text}"
    task_data = task_res.json()
    assert task_data["status"] == "ok"
    assert task_data["count"] == 1
    print(f"   ✅ 使用切片上傳之檔案成功建立任務: {task_data['task_id']}")

    # 清理測試資源
    if saved_filepath.exists():
        saved_filepath.unlink()
    print("   ✅ 測試資源清理完成！")

if __name__ == "__main__":
    test_chunked_upload_and_task_creation()
    print("🎉 大檔案分塊切片上傳自動化驗證全部通過！")
