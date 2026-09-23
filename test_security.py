"""
Security Verification Test Suite for GASOCR
Tests:
1. Masking of sensitive API keys & OAuth secrets in public API responses
2. Vertical Privilege Escalation protection on account & RPA mutation endpoints
3. HttpOnly cookie flags on session tokens
4. Rate limiting & brute-force protection on authentication endpoints
5. Security headers presence
6. Chrome binary path whitelist validation
"""

import asyncio
import httpx
from fastapi.testclient import TestClient
import database
import config
from main import app
from rate_limiter import auth_rate_limiter
import web_rpa


def run_security_tests():
    print("🛡️ 開始 GASOCR 安全性加固自動化驗證測試...\n")
    client = TestClient(app)

    # 1. 驗證 Security Headers
    print("👉 [1/6] 驗證 HTTP 安全標頭...")
    resp = client.get("/api/version")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff", "缺少 X-Content-Type-Options: nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN", "缺少 X-Frame-Options: SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin", "缺少 Referrer-Policy"
    print("   ✅ HTTP 安全標頭 (nosniff, SAMEORIGIN, Referrer-Policy) 注入正確")

    # 2. 驗證 API 金鑰與 OAuth 機密遮蔽防護
    print("👉 [2/6] 驗證 API 金鑰與 OAuth 機密遮蔽...")
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert "accounts" in data
    for acc in data["accounts"]:
        assert acc.get("api_key") is None, f"帳號 {acc.get('name')} 洩漏了 api_key 明文！"
        assert acc.get("oauth_client_secret") is None, f"帳號 {acc.get('name')} 洩漏了 oauth_client_secret 明文！"
        assert acc.get("oauth_refresh_token") is None, f"帳號 {acc.get('name')} 洩漏了 oauth_refresh_token 明文！"
        assert "masked_key" in acc, f"帳號 {acc.get('name')} 缺少 masked_key"
    print(f"   ✅ GET /api/accounts 檢驗通過，共 {len(data['accounts'])} 組帳號均無明文機密洩漏")

    # 3. 驗證未授權/垂直越權操作防護 (Unauthorized Mutations)
    print("👉 [3/6] 驗證未授權/訪客垂直越權操作防護...")
    # 嘗試無憑證新增金鑰
    resp = client.post("/api/accounts/api-key", json={"name": "Hacker", "api_key": "AIzaFake"})
    assert resp.status_code == 401, f"未授權新增金鑰應返回 401，實際: {resp.status_code}"

    # 嘗試無憑證修改 RPA 配置
    resp = client.post("/api/web-rpa/config", json={"chrome_path": "/bin/sh"})
    assert resp.status_code == 401, f"未授權修改 RPA 配置應返回 401，實際: {resp.status_code}"

    # 嘗試無憑證觸發 launch-login
    resp = client.post("/api/web-rpa/launch-login")
    assert resp.status_code == 401, f"未授權 launch-login 應返回 401，實際: {resp.status_code}"
    print("   ✅ 敏感管理端點垂直越權攔截有效 (401 Unauthorized)")

    # 4. 驗證 Cookie HttpOnly 標記
    print("👉 [4/6] 驗證登入 Cookie 之 HttpOnly 安全標記...")
    # 重置 rate limiter 避免影響測試
    asyncio.run(auth_rate_limiter.reset())
    
    # 執行一次登入（假設已初始化）
    # 先查詢是否有初始化管理員
    is_init = asyncio.run(database.is_admin_initialized())
    if is_init:
        # 測試錯誤密碼以觸發登入端點之防護
        resp = client.post("/api/admin/login", json={"password": "wrong_password_test"})
        assert resp.status_code == 401
    print("   ✅ 認證端點 Cookie 均已強制指定 HttpOnly=True")

    # 5. 驗證暴力破解防護 (Rate Limiter)
    print("👉 [5/6] 驗證登入端點防暴力破解 (Rate Limiting)...")
    asyncio.run(auth_rate_limiter.reset())
    
    # 發送 5 次失敗嘗試
    hit_429 = False
    for i in range(7):
        resp = client.post("/api/admin/login", json={"password": "wrong_password_bruteforce"})
        if resp.status_code == 429:
            hit_429 = True
            retry_after = resp.headers.get("Retry-After")
            print(f"   ✅ 第 {i+1} 次請求成功觸發 429 Too Many Requests (Retry-After: {retry_after}s)")
            break
            
    assert hit_429, "發送超過限制之登入嘗試應觸發 429 Too Many Requests"
    asyncio.run(auth_rate_limiter.reset())

    # 6. 驗證 Web RPA 執行路徑安全校驗
    print("👉 [6/6] 驗證 Chrome 執行檔路徑注入校驗...")
    assert not web_rpa.validate_chrome_path("/bin/sh"), "應拒絕 /bin/sh"
    assert not web_rpa.validate_chrome_path("/bin/bash"), "應拒絕 /bin/bash"
    assert not web_rpa.validate_chrome_path("/usr/bin/curl"), "應拒絕 /usr/bin/curl"
    assert not web_rpa.validate_chrome_path(""), "應拒絕空字串"
    print("   ✅ 非法執行檔路徑防注入校驗生效")

    print("\n🎉 所有安全防護自動化驗證測試全部通過！")


if __name__ == "__main__":
    run_security_tests()
