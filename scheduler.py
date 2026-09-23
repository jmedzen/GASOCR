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
        
    async def get_next_available_account(self, specific_account_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        獲取下一個可用帳號：
        - 若指定 specific_account_id（付費指定金鑰）：
          1. 僅取得該帳號
          2. 若為付費帳號 (is_paid == 1)，解除 4.2s 強制節流限制（僅需極短防併發間隔 0.2s）
          3. 若觸發冷卻則等待或回傳 None
        - 若未指定（免費輪詢模式）：
          1. 僅從 is_paid == 0 的免費金鑰池中選取（確保免費批次不消耗付費金鑰）
          2. 必須是 is_active = 1 且當前時間 >= cooldown_until
          3. 依上次使用時間最早者優先（負載均衡）
          4. 強制 4.2s 間隔保護，保證符合 15 RPM
        """
        async with self._lock:
            accounts = await database.get_accounts(include_secrets=True)
            now = time.time()
            
            if specific_account_id is not None:
                # 專用模式：只使用指定的帳號
                chosen = next((a for a in accounts if a["id"] == specific_account_id), None)
                if not chosen or chosen.get("is_active") != 1:
                    return None
                if (chosen.get("cooldown_until") or 0.0) > now:
                    return None
                
                # 若為付費帳號，不需 4.2 秒節流，僅給予 0.2 秒極短防抖
                is_paid = chosen.get("is_paid", 0) == 1
                min_interval = 0.2 if is_paid else MIN_REQUEST_INTERVAL_SECONDS
                last_req = self._last_request_times.get(chosen["id"], chosen.get("last_used_at", 0.0))
                elapsed = now - last_req
                if elapsed < min_interval:
                    wait_needed = min_interval - elapsed
                    await asyncio.sleep(wait_needed)
                
                self._last_request_times[chosen["id"]] = time.time()
                return chosen

            # 免費輪詢模式：過濾出 is_paid == 0 且啟用的免費金鑰
            available = [
                acc for acc in accounts 
                if acc.get("is_active") == 1 
                and acc.get("is_paid", 0) == 0
                and (acc.get("cooldown_until") or 0.0) <= now
            ]
            
            if not available:
                return None
                
            # 依上次使用時間排序（最久沒使用的優先）
            available.sort(key=lambda acc: self._last_request_times.get(acc["id"], acc.get("last_used_at", 0.0)))
            chosen = available[0]
            
            # 計算安全呼叫間隔節流（避免單一帳號超過 15 RPM）
            min_interval = 0.2 if chosen.get("is_paid", 0) == 1 else MIN_REQUEST_INTERVAL_SECONDS
            last_req = self._last_request_times.get(chosen["id"], chosen.get("last_used_at", 0.0))
            elapsed = now - last_req
            if elapsed < min_interval:
                wait_needed = min_interval - elapsed
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

    async def wait_for_any_account(self, max_wait_seconds: float = 120.0, specific_account_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        若當前帳號皆處於冷卻中，等待直到解凍恢復
        """
        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            account = await self.get_next_available_account(specific_account_id=specific_account_id)
            if account:
                return account
                
            accounts = await database.get_accounts()
            if specific_account_id is not None:
                target_accounts = [acc for acc in accounts if acc["id"] == specific_account_id and acc.get("is_active") == 1]
            else:
                target_accounts = [acc for acc in accounts if acc.get("is_active") == 1 and acc.get("is_paid", 0) == 0]
            
            if not target_accounts:
                return None
                
            now = time.time()
            cooldown_remains = [max(0.5, acc.get("cooldown_until", 0.0) - now) for acc in target_accounts]
            min_wait = min(cooldown_remains)
            sleep_duration = min(min_wait, 5.0)
            await asyncio.sleep(sleep_duration)
            
        return None

# 全域單例排程器
scheduler = AccountScheduler()
