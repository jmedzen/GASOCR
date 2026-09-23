"""
Rate Limiter module for protecting authentication and sensitive endpoints
against brute-force attacks and abuse.
"""

import time
import asyncio
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from fastapi import Request


class SlidingWindowRateLimiter:
    """
    In-memory rate limiter with sliding window & temporary lockout.
    Thread-safe and async-compatible.
    """
    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        lockout_failures: int = 8,
        lockout_duration_seconds: float = 900.0  # 15 minutes
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_failures = lockout_failures
        self.lockout_duration_seconds = lockout_duration_seconds

        # key (ip:action) -> list of timestamp of attempts
        self._attempts: Dict[str, List[float]] = defaultdict(list)
        # key (ip:action) -> consecutive failure count
        self._consecutive_failures: Dict[str, int] = defaultdict(int)
        # key (ip:action) -> lockout until timestamp
        self._lockout_until: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _key(self, ip: str, action: str) -> str:
        return f"{ip}:{action}"

    async def check_rate_limit(self, ip: str, action: str) -> Tuple[bool, Optional[int]]:
        """
        Check if request is allowed.
        Returns: (is_allowed, retry_after_seconds)
        """
        async with self._lock:
            now = time.time()
            k = self._key(ip, action)

            # 1. Check if locked out due to excessive failures
            lockout_time = self._lockout_until.get(k, 0.0)
            if now < lockout_time:
                return False, int(lockout_time - now) + 1

            # 2. Clean sliding window
            cutoff = now - self.window_seconds
            valid_attempts = [t for t in self._attempts[k] if t > cutoff]
            self._attempts[k] = valid_attempts

            # 3. Check window attempts
            if len(valid_attempts) >= self.max_attempts:
                retry_after = int(valid_attempts[0] + self.window_seconds - now) + 1
                return False, max(1, retry_after)

            return True, None

    async def record_attempt(self, ip: str, action: str):
        """Record an attempt in the sliding window"""
        async with self._lock:
            now = time.time()
            k = self._key(ip, action)
            self._attempts[k].append(now)

    async def record_failure(self, ip: str, action: str):
        """Record an authentication failure; trigger lockout if threshold reached"""
        async with self._lock:
            now = time.time()
            k = self._key(ip, action)
            self._consecutive_failures[k] += 1
            if self._consecutive_failures[k] >= self.lockout_failures:
                self._lockout_until[k] = now + self.lockout_duration_seconds
                self._consecutive_failures[k] = 0

    async def record_success(self, ip: str, action: str):
        """Clear failure counts on successful auth"""
        async with self._lock:
            k = self._key(ip, action)
            self._consecutive_failures[k] = 0
            self._lockout_until.pop(k, None)

    async def reset(self, ip: Optional[str] = None, action: Optional[str] = None):
        """Reset limits for testing or administrative reset"""
        async with self._lock:
            if ip and action:
                k = self._key(ip, action)
                self._attempts.pop(k, None)
                self._consecutive_failures.pop(k, None)
                self._lockout_until.pop(k, None)
            else:
                self._attempts.clear()
                self._consecutive_failures.clear()
                self._lockout_until.clear()


def get_client_ip(request: Request) -> str:
    """Extract client IP from request headers or direct client connection"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


# Global rate limiter instance for auth endpoints (5 attempts per minute, 8 failures = 15m lockout)
auth_rate_limiter = SlidingWindowRateLimiter(
    max_attempts=5,
    window_seconds=60.0,
    lockout_failures=8,
    lockout_duration_seconds=900.0
)
