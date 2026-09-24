import asyncio
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List
import database
from config import MIN_REQUEST_INTERVAL_SECONDS, DEFAULT_COOLDOWN_SECONDS

def calculate_free_quota_reset_info() -> Dict[str, Any]:
    """
    計算 Google AI Studio 免費額度每日重置時間與剩餘秒數。
    Google AI Studio 免費層額度固定於每日美西時間午夜 12:00 AM PT (Midnight Pacific Time) 重置。
    本函式精確換算為使用者本地時區 (如 Asia/Taipei) 與易讀時間字串。
    """
    try:
        pt_tz = ZoneInfo("America/Los_Angeles")
        now_pt = datetime.now(pt_tz)
    except Exception:
        now_utc = datetime.now(timezone.utc)
        # 降級備援時區偏移換算：4~10月美西夏令 PDT (UTC-7)，其餘月份 PST (UTC-8)
        offset_hours = -7 if (3 < now_utc.month < 11) else -8
        pt_tz = timezone(timedelta(hours=offset_hours))
        now_pt = now_utc.astimezone(pt_tz)

    next_midnight_pt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    remaining_seconds = max(0, int((next_midnight_pt - now_pt).total_seconds()))

    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo
    next_midnight_local = next_midnight_pt.astimezone(local_tz)

    hours = remaining_seconds // 3600
    minutes = (remaining_seconds % 3600) // 60
    seconds = remaining_seconds % 60

    is_today = next_midnight_local.date() == local_now.date()
    day_str = "今日" if is_today else "明日"
    hm_str = next_midnight_local.strftime("%H:%M")
    reset_time_display = f"{day_str} {hm_str} (本地時間)"

    return {
        "remaining_seconds": remaining_seconds,
        "reset_timestamp": next_midnight_pt.timestamp(),
        "reset_time_pt": next_midnight_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "reset_time_local": next_midnight_local.strftime("%Y-%m-%d %H:%M:%S"),
        "reset_time_display": reset_time_display,
        "remaining_formatted": f"{hours} 小時 {minutes} 分鐘 {seconds} 秒",
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds
    }

class AccountScheduler:
    """智慧多帳號調度器：支援負載平衡、速率節流與 429 自動冷卻避讓"""
    
    def __init__(self):
        self._lock = asyncio.Lock()
        # 記憶體內快取每個 account_id 上次發出請求的精確時間點
        self._last_request_times: Dict[int, float] = {}
        # 追蹤每個帳號連續遭遇配額耗盡 (429 / Quota Exceeded) 的次數
        self._consecutive_quota_errors: Dict[int, int] = {}
        # 全域標記：所有啟用中的免費金鑰池是否皆已耗盡配額 (各連續 3 次以上)
        self._quota_exhausted_triggered: bool = False
        
    async def get_next_available_account(
        self, 
        specific_account_id: Optional[int] = None,
        require_paid: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        獲取下一個可用帳號：
        - 若指定 specific_account_id（付費指定金鑰）：
          1. 僅取得該帳號
          2. 若為付費帳號 (is_paid == 1)，解除 4.2s 強制節流限制（僅需極短防併發間隔 0.2s）
          3. 若觸發冷卻則等待或回傳 None
        - 若未指定但 require_paid == True（付費金鑰池輪詢模式）：
          1. 從 is_paid == 1 的付費金鑰池中依輪詢挑選
          2. 解除 4.2s 強制節流限制（僅需 0.2s 極短防併發間隔，享有 1000 RPM）
        - 若未指定且 require_paid == False（免費金鑰池輪詢模式）：
          1. 僅從 is_paid == 0 的免費金鑰池中選取（確保免費批次不消耗付費金鑰）
          2. 強制 4.2s 間隔保護，保證符合 15 RPM
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

            if require_paid:
                # 付費金鑰池輪詢模式：過濾出 is_paid == 1 且啟用的付費金鑰
                available = [
                    acc for acc in accounts 
                    if acc.get("is_active") == 1 
                    and acc.get("is_paid", 0) == 1
                    and (acc.get("cooldown_until") or 0.0) <= now
                ]
            else:
                # 免費金鑰池輪詢模式：過濾出 is_paid == 0 且啟用的免費金鑰
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
            
            # 計算安全呼叫間隔節流（付費金鑰 0.2s，免費金鑰 4.2s）
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
        """成功回報，更新計數器並重置該帳號的連續配額錯誤次數"""
        async with self._lock:
            self._consecutive_quota_errors[account_id] = 0
            self._quota_exhausted_triggered = False
        await database.update_account_usage(account_id)

    async def report_rate_limited(self, account_id: int, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS):
        """
        當觸發 429 時呼叫：
        將該帳號標記冷卻，暫時移出排程池
        """
        print(f"⚠️ 帳號 ID {account_id} 觸發速率限制 (429)，自動進入冷卻 {cooldown_seconds} 秒")
        await database.set_account_cooldown(account_id, cooldown_seconds)

    async def report_quota_exhausted(self, account_id: int) -> bool:
        """
        回報免費金鑰遭遇配額耗盡 (429 / RESOURCE_EXHAUSTED / Quota exceeded)。
        需求規範：
        檢查到所有 free api pool 的 response 都是 quota 已經用完，
        每個 free key 訊息連續出現三次。
        回傳 True 代表「所有啟用中的免費金鑰皆已連續 3 次回傳額度耗盡」，否則回傳 False。
        """
        async with self._lock:
            current_count = self._consecutive_quota_errors.get(account_id, 0) + 1
            self._consecutive_quota_errors[account_id] = current_count
            print(f"⚠️ [免費配額追蹤] 帳號 ID {account_id} 連續回傳配額耗盡: {current_count} 次")

            accounts = await database.get_accounts()
            free_accounts = [
                acc for acc in accounts 
                if acc.get("is_active") == 1 and acc.get("is_paid", 0) == 0
            ]

            if not free_accounts:
                return False

            # 檢查是否「所有」啟用中的免費金鑰池皆連續 >= 3 次配額耗盡
            all_exhausted = all(
                self._consecutive_quota_errors.get(acc["id"], 0) >= 3 
                for acc in free_accounts
            )

            if all_exhausted:
                self._quota_exhausted_triggered = True
                reset_info = calculate_free_quota_reset_info()
                print(f"🚨 [全域免費配額耗盡] 所有免費金鑰池皆已連續 3 次回傳配額耗盡！預計重置: {reset_info['reset_time_display']} (美西 00:00 PT)")
                return True

            return False

    async def is_all_free_quota_exhausted(self) -> bool:
        """
        查詢當前免費金鑰池是否全數處於連續 3 次以上配額耗盡狀態
        """
        async with self._lock:
            accounts = await database.get_accounts()
            free_accounts = [
                acc for acc in accounts 
                if acc.get("is_active") == 1 and acc.get("is_paid", 0) == 0
            ]
            if not free_accounts:
                return False
            return all(
                self._consecutive_quota_errors.get(acc["id"], 0) >= 3 
                for acc in free_accounts
            )

    async def reset_quota_tracking(self):
        """重置所有金鑰的連續配額錯誤次數與耗盡狀態"""
        async with self._lock:
            self._consecutive_quota_errors.clear()
            self._quota_exhausted_triggered = False
            print("🔄 [免費配額追蹤] 已重置所有帳號之連續配額耗盡計數器")

    def get_free_quota_reset_info(self) -> Dict[str, Any]:
        """獲取免費配額重置時間與倒數資訊"""
        return calculate_free_quota_reset_info()

    async def get_quota_status(self) -> Dict[str, Any]:
        """取得當前免費配額全域狀態摘要 (供前端 API 查詢與 Prompt 展示)"""
        async with self._lock:
            accounts = await database.get_accounts()
            free_accounts = [
                acc for acc in accounts 
                if acc.get("is_active") == 1 and acc.get("is_paid", 0) == 0
            ]
            exhausted = False
            if free_accounts:
                exhausted = all(
                    self._consecutive_quota_errors.get(acc["id"], 0) >= 3 
                    for acc in free_accounts
                )
            
            reset_info = calculate_free_quota_reset_info()
            return {
                "all_free_exhausted": exhausted,
                "free_accounts_count": len(free_accounts),
                "consecutive_counts": {str(k): v for k, v in self._consecutive_quota_errors.items()},
                "free_account_details": [
                    {
                        "id": acc["id"],
                        "name": acc["name"],
                        "consecutive_errors": self._consecutive_quota_errors.get(acc["id"], 0)
                    }
                    for acc in free_accounts
                ],
                "reset_info": reset_info
            }

    async def wait_for_any_account(
        self, 
        max_wait_seconds: float = 120.0, 
        specific_account_id: Optional[int] = None,
        require_paid: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        若當前帳號皆處於冷卻中，等待直到解凍恢復
        """
        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            account = await self.get_next_available_account(
                specific_account_id=specific_account_id,
                require_paid=require_paid
            )
            if account:
                return account
                
            accounts = await database.get_accounts()
            if specific_account_id is not None:
                target_accounts = [acc for acc in accounts if acc["id"] == specific_account_id and acc.get("is_active") == 1]
            elif require_paid:
                target_accounts = [acc for acc in accounts if acc.get("is_active") == 1 and acc.get("is_paid", 0) == 1]
            else:
                target_accounts = [acc for acc in accounts if acc.get("is_active") == 1 and acc.get("is_paid", 0) == 0]
            
            if not target_accounts:
                return None
                
            now = time.time()
            remains_to_wait = max_wait_seconds - (now - start_time)
            cooldown_remains = [max(0.5, acc.get("cooldown_until", 0.0) - now) for acc in target_accounts]
            min_wait = min(cooldown_remains)
            # 若最短冷卻剩餘時間已大於剩餘可等待時間，則確定超時，提早結束等待以利備援接手
            if min_wait > remains_to_wait:
                return None
            sleep_duration = min(min_wait, 5.0)
            await asyncio.sleep(sleep_duration)
            
        return None

# 全域單例排程器
scheduler = AccountScheduler()
