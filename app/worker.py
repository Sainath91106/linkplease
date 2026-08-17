"""
Background loop. The webhook handler's only job is to write a row and
return 200 fast -- this is what actually talks to the DM API, retries
failures, and reconciles "queued_remote" DMs into sent/failed.

Two loops running side by side:
  - sender_loop: picks up 'pending' jobs whose next_attempt_at has passed
  - reconciler_loop: for jobs we already got a 202 on, polls until terminal
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.db import get_conn, bump_counter
from app import pseudogram_client as pg

MAX_ATTEMPTS = 6
POLL_INTERVAL = 1.0


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def backoff_seconds(attempts: int) -> float:
    # 1, 2, 4, 8, 16, 32... capped, nothing fancy
    return min(2 ** attempts, 60)


async def sender_loop():
    while True:
        try:
            await _send_one_batch()
        except Exception as e:
            # a bug in the worker should never kill the whole loop
            print(f"[sender_loop] error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def _send_one_batch():
    conn = get_conn()
    now = now_iso()
    rows = conn.execute(
        """SELECT * FROM dm_jobs
           WHERE status = 'pending'
             AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
           ORDER BY created_at ASC
           LIMIT 20""",
        (now,),
    ).fetchall()
    conn.close()

    for row in rows:
        await _attempt_send(dict(row))


async def _attempt_send(job: dict):
    idem_key = job["idempotency_key"] or f"{job['rule_id']}:{job['user_id']}:{job['attempts']}"

    ok, dm_id, status_code, retry_after, error = await pg.send_dm(
        recipient_user_id=job["user_id"],
        message=job["message"],
        comment_id=job["comment_id"],
        idempotency_key=idem_key,
    )

    conn = get_conn()
    try:
        if ok:
            conn.execute(
                """UPDATE dm_jobs SET status='queued_remote', dm_id=?, updated_at=?, last_error=NULL
                   WHERE id=?""",
                (dm_id, now_iso(), job["id"]),
            )
        elif status_code == 429:
            next_attempt = datetime.now(timezone.utc) + timedelta(seconds=retry_after or 10)
            conn.execute(
                """UPDATE dm_jobs SET next_attempt_at=?, updated_at=?, last_error='rate_limited'
                   WHERE id=?""",
                (next_attempt.isoformat(), now_iso(), job["id"]),
            )
        elif status_code == 400:
            # retrying a malformed request forever is pointless
            conn.execute(
                """UPDATE dm_jobs SET status='failed', updated_at=?, last_error=?
                   WHERE id=?""",
                (now_iso(), f"400: {error}", job["id"]),
            )
        else:
            attempts = job["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                conn.execute(
                    """UPDATE dm_jobs SET status='failed', attempts=?, updated_at=?, last_error=?
                       WHERE id=?""",
                    (attempts, now_iso(), f"gave up after {attempts} tries: {error}", job["id"]),
                )
            else:
                next_attempt = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds(attempts))
                conn.execute(
                    """UPDATE dm_jobs SET attempts=?, next_attempt_at=?, updated_at=?, last_error=?
                       WHERE id=?""",
                    (attempts, next_attempt.isoformat(), now_iso(), str(error), job["id"]),
                )
        conn.commit()
    finally:
        conn.close()


async def reconciler_loop():
    while True:
        try:
            await _reconcile_one_batch()
        except Exception as e:
            print(f"[reconciler_loop] error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def _reconcile_one_batch():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM dm_jobs WHERE status = 'queued_remote' LIMIT 20"
    ).fetchall()
    conn.close()

    for row in rows:
        job = dict(row)
        remote_status, error = await pg.check_dm_status(job["dm_id"])
        conn = get_conn()
        try:
            if remote_status == "delivered":
                conn.execute(
                    "UPDATE dm_jobs SET status='sent', updated_at=? WHERE id=?",
                    (now_iso(), job["id"]),
                )
            elif remote_status == "failed":
                # accepted, then failed later. per the assignment: catch and retry.
                attempts = job["attempts"] + 1
                if attempts >= MAX_ATTEMPTS:
                    conn.execute(
                        """UPDATE dm_jobs SET status='failed', attempts=?, updated_at=?, last_error=?
                           WHERE id=?""",
                        (attempts, now_iso(), "delivery failed after accept, retries exhausted", job["id"]),
                    )
                else:
                    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds(attempts))
                    conn.execute(
                        """UPDATE dm_jobs SET status='pending', attempts=?, next_attempt_at=?, dm_id=NULL,
                           updated_at=?, last_error='delivery failed after accept, retrying'
                           WHERE id=?""",
                        (attempts, next_attempt.isoformat(), now_iso(), job["id"]),
                    )
            # if still 'queued', leave it -- we'll check again next pass
            conn.commit()
        finally:
            conn.close()


async def startup_recover():
    """
    On boot, anything sitting in queued_remote from before a restart needs
    to get picked back up by the reconciler (it will, since it's read from
    the DB not memory). Nothing extra to do here really -- that's the
    actual point of persisting state instead of keeping it in a Python
    list. Left as its own function mainly so it's obvious in main.py that
    this was a deliberate design decision, not an oversight.
    """
    pass
