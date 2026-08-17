"""
Thin wrapper around the mock API. Only job here is: send a DM, check a DM's
status, and don't blow through the rate limit doing it.

Rate limiting note: this is a simple in-memory sliding window, which means
it resets if the process restarts. Good enough for this assignment, called
it out in FAILURES.md rather than pretending it's bulletproof.
"""
import os
import time
import asyncio
import collections
import httpx

BASE_URL = os.environ.get("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com")
API_KEY = os.environ.get("PSEUDOGRAM_API_KEY", "")

RATE_LIMIT = 10
WINDOW_SECONDS = 60

_send_timestamps = collections.deque()
_rate_lock = asyncio.Lock()


async def _wait_for_rate_limit_slot():
    """Blocks until we have room under 10 req / rolling 60s."""
    async with _rate_lock:
        while True:
            now = time.monotonic()
            while _send_timestamps and now - _send_timestamps[0] > WINDOW_SECONDS:
                _send_timestamps.popleft()
            if len(_send_timestamps) < RATE_LIMIT:
                _send_timestamps.append(now)
                return
            sleep_for = WINDOW_SECONDS - (now - _send_timestamps[0]) + 0.05
            await asyncio.sleep(max(sleep_for, 0.05))


def _headers():
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


async def send_dm(recipient_user_id: str, message: str, comment_id: str, idempotency_key: str):
    """
    Returns (ok, dm_id_or_none, status_code, retry_after_or_none, error_or_none)
    Caller decides what to do with a failure -- this function just reports
    what actually happened, it doesn't retry itself (the worker loop does
    that, so it can respect rate limits / restarts).
    """
    await _wait_for_rate_limit_slot()

    headers = _headers()
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    payload = {
        "recipient_user_id": recipient_user_id,
        "message": message,
        "comment_id": comment_id,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(f"{BASE_URL}/v1/dm/send", json=payload, headers=headers)
        except httpx.RequestError as e:
            return False, None, None, None, str(e)

    if resp.status_code == 202:
        body = resp.json()
        return True, body.get("dm_id"), 202, None, None

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "10"))
        return False, None, 429, retry_after, "rate_limited"

    if resp.status_code == 400:
        # not retryable, payload itself is wrong
        return False, None, 400, None, resp.text

    # 500 and anything else unexpected -- treat as retryable
    return False, None, resp.status_code, None, resp.text


async def check_dm_status(dm_id: str):
    """Returns (status_or_none, error_or_none). Doesn't count against rate limit."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{BASE_URL}/v1/dm/{dm_id}", headers=_headers())
        except httpx.RequestError as e:
            return None, str(e)

    if resp.status_code == 200:
        return resp.json().get("status"), None
    return None, resp.text
