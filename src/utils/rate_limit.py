"""Minimal in-memory rate limiter for auth endpoints (login, register,
forgot-password) — none of these had any throttling at all, confirmed live
(5 rapid wrong-password login attempts, zero pushback).

In-memory only: effective against simple/single-instance brute-force, but
resets on deploy and doesn't coordinate across multiple server processes.
If KappGen scales to multiple backend instances, replace with a shared store
(Redis) — not urgent at current traffic."""
import time
import threading
from collections import defaultdict
from fastapi import HTTPException, Request, status

_attempts: dict[str, list[float]] = defaultdict(list)
_lock = threading.Lock()


def rate_limit(key_prefix: str, max_attempts: int, window_seconds: int):
    def _check(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        key = f"{key_prefix}:{client_ip}"
        now = time.time()
        with _lock:
            attempts = [t for t in _attempts[key] if now - t < window_seconds]
            if len(attempts) >= max_attempts:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Trop de tentatives. Réessaie dans quelques minutes.",
                )
            attempts.append(now)
            _attempts[key] = attempts
    return _check
