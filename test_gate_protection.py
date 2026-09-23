import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import config
import database
from main import app

@pytest.mark.asyncio
async def test_gate_protection_flow():
    await database.init_db()
    
    # 確保初始重置測試狀態
    async with database.get_db() as db:
        await db.execute("DELETE FROM system_settings WHERE key IN ('admin_password_hash', 'access_gate_enabled', 'access_password_hash')")
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # --- 階段 1: 尚未初始化管理員 ---
        # 1.1 訪問首頁 / 應回傳獨立初次安裝精靈 (gate_lock.html mode="setup")
        res = await client.get("/")
        assert res.status_code == 200
        html = res.text
        assert "GASOCR 系統初次安裝" in html
        assert "管理員密碼" in html
        # 確保主系統關鍵字絕不外洩
        assert "reocrQueue" not in html
        assert "allTasks" not in html

        # 1.2 未初始化前，業務 API 一律 401 拒絕
        res_tasks = await client.get("/api/tasks")
        assert res_tasks.status_code == 401
        assert "系統尚未初始化" in res_tasks.json()["detail"]

        # 1.3 初始化管理員
        init_res = await client.post("/api/admin/init", json={"password": "adminSecretPassword123"})
        assert init_res.status_code == 200
        assert "gasocr_admin_token" in init_res.cookies
        admin_cookie = init_res.cookies["gasocr_admin_token"]

        # --- 階段 2: 已初始化，但尚未啟用全站通關保護 ---
        # 2.1 訪客訪問 / 應直接獲得主系統 index.html
        guest_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        res_guest = await guest_client.get("/")
        assert res_guest.status_code == 200
        assert "GASOCR" in res_guest.text
        assert "Google AI Studio & Gemini Web 古籍文獻高精度 OCR 轉譯系統" in res_guest.text
        assert "GASOCR 存取保護" not in res_guest.text

        # 2.2 業務 API 在未啟用通關時正常放行
        res_tasks_open = await guest_client.get("/api/tasks")
        assert res_tasks_open.status_code == 200

        # --- 階段 3: 管理員開啟全站通關密碼保護 ---
        admin_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies={"gasocr_admin_token": admin_cookie})
        enable_res = await admin_client.post(
            "/api/admin/settings/access-gate",
            json={"enabled": True, "password": "gateCode2026"}
        )
        assert enable_res.status_code == 200
        assert enable_res.json()["access_gate_enabled"] is True

        # --- 階段 4: 全站保護生效，未授權訪客前後端物理限制 ---
        # 4.1 訪客訪問首頁 / 回傳獨立鎖定頁面，且不包含主系統原始碼
        locked_home = await guest_client.get("/")
        assert locked_home.status_code == 200
        locked_html = locked_home.text
        assert "GASOCR 存取保護" in locked_html
        assert "本系統已啟用全站安全防護" in locked_html
        assert "reocrQueue" not in locked_html
        assert "allTasks" not in locked_html
        assert "Google AI Studio & Gemini Web 古籍文獻高精度 OCR 轉譯系統" not in locked_html

        # 4.2 訪客直接請求業務 API 被 401 攔截
        api_blocked = await guest_client.get("/api/tasks")
        assert api_blocked.status_code == 401
        assert "已啟用通關密碼保護" in api_blocked.json()["detail"]

        api_blocked_acc = await guest_client.get("/api/accounts")
        assert api_blocked_acc.status_code == 401

        # 4.3 輸入錯誤密碼被拒絕
        wrong_unlock = await guest_client.post("/api/auth/verify-access", json={"password": "wrong"})
        assert wrong_unlock.status_code == 401

        # --- 階段 5: 使用通關密碼解鎖 ---
        unlock_res = await guest_client.post("/api/auth/verify-access", json={"password": "gateCode2026"})
        assert unlock_res.status_code == 200
        assert "gasocr_access_token" in unlock_res.cookies
        assert unlock_res.json()["is_admin"] is False

        # 解鎖後訪問 / 成功獲得主系統
        unlocked_home = await guest_client.get("/")
        assert unlocked_home.status_code == 200
        assert "GASOCR 存取保護" not in unlocked_home.text
        assert "Google AI Studio & Gemini Web 古籍文獻高精度 OCR 轉譯系統" in unlocked_home.text

        # 業務 API 正常存取
        api_allowed = await guest_client.get("/api/tasks")
        assert api_allowed.status_code == 200

        # --- 階段 6: 重新上鎖 (Logout Access) ---
        lock_res = await guest_client.post("/api/auth/logout-access")
        assert lock_res.status_code == 200

        # 再次訪問首頁 / 回復為獨立鎖定畫面
        relocked_home = await guest_client.get("/")
        assert relocked_home.status_code == 200
        assert "GASOCR 存取保護" in relocked_home.text

        # 業務 API 再次被 401 拒絕
        api_blocked_again = await guest_client.get("/api/tasks")
        assert api_blocked_again.status_code == 401

        # --- 階段 7: 使用「管理員密碼」在鎖定頁面直接解鎖 ---
        admin_unlock = await guest_client.post("/api/auth/verify-access", json={"password": "adminSecretPassword123"})
        assert admin_unlock.status_code == 200
        assert admin_unlock.json()["is_admin"] is True
        assert "gasocr_access_token" in admin_unlock.cookies
        assert "gasocr_admin_token" in admin_unlock.cookies

        # 獲得管理員權限，可直接存取管理後台設定
        admin_settings_res = await guest_client.get("/api/admin/settings")
        assert admin_settings_res.status_code == 200

    print("🎉 全站密碼保護前後端物理隔離所有情境測試全數通過！")
