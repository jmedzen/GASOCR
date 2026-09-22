import asyncio
import time
from typing import Optional, Dict, Any, List
import database
from config import MIN_REQUEST_INTERVAL_SECONDS, DEFAULT_COOLDOWN_SECONDS

class AccountScheduler:
    """智慧多帳號調度器：支援負載平衡、速率節流與 429 自動冷卻避讓"""
    
    def __init__(self):
        self._lock = asyncio.Lock()
        # 記憶體內快取每個 account_id 上次發出請求的精確時間點
        self._last_request_times: Dict[int, float] = {}
        
    async def get_next_available_account(self) -> Optional[Dict[str, Any]]:
        """
        獲取下一個可用帳號：
        1. 必須是 is_active = 1
        2. 當前時間 >= cooldown_until
        3. 依照 (上次使用時間) 最早者優先（達到最佳負載均衡）
        """
        async with self._lock:
            accounts = await database.get_accounts()
            now = time.time()
            
            # 過濾可用帳號
            available = [
                acc for acc in accounts 
                if acc.get("is_active") == 1 and (acc.get("cooldown_until") or 0.0) <= now
            ]
            
            if not available:
                return None
                
            # 依上次使用時間排序（最久沒使用的優先）
            available.sort(key=lambda acc: self._last_request_times.get(acc["id"], acc.get("last_used_at", 0.0)))
            chosen = available[0]
            
            # 計算安全呼叫間隔節流（避免單一帳號超過 15 RPM）
            last_req = self._last_request_times.get(chosen["id"], chosen.get("last_used_at", 0.0))
            elapsed = now - last_req
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                wait_needed = MIN_REQUEST_INTERVAL_SECONDS - elapsed
                await asyncio.sleep(wait_needed)
                
            # 登記本次使用時間
            self._last_request_times[chosen["id"]] = time.time()
            return chosen

    async def report_success(self, account_id: int):
        """成功回報，更新計數器"""
        await database.update_account_usage(account_id)

    async def report_rate_limited(self, account_id: int, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS):
        """
        當觸發 429 時呼叫：
        將該帳號標記冷卻，暫時移出排程池
        """
        print(f"⚠️ 帳號 ID {account_id} 觸發速率限制 (429)，自動進入冷卻 {cooldown_seconds} 秒")
        await database.set_account_cooldown(account_id, cooldown_seconds)

    async def wait_for_any_account(self, max_wait_seconds: float = 120.0) -> Optional[Dict[str, Any]]:
        """
        若當前所有帳號皆處於冷卻中，等待直到最早解凍的帳號恢復
        """
        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            account = await self.get_next_available_account()
            if account:
                return account
                
            # 檢查是否有帳號即將解除冷卻
            accounts = await database.get_accounts()
            active_accounts = [acc for acc in accounts if acc.get("is_active") == 1]
            if not active_accounts:
                # 系統中沒有任何啟用的帳號
                return None
                
            now = time.time()
            # 找出最早解凍的剩餘時間
            cooldown_remains = [max(0.5, acc.get("cooldown_until", 0.0) - now) for acc in active_accounts]
            min_wait = min(cooldown_remains)
            # 等待一小段時間或至最早解凍時間
            sleep_duration = min(min_wait, 5.0)
            await asyncio.sleep(sleep_duration)
            
        return None

# 全域單例排程器
scheduler = AccountScheduler()
