"""
LinkPlease intern assignment.

Design in one paragraph: webhook writes to sqlite and returns 200
immediately, it never calls the DM API itself. Two background loops
(see worker.py) do the actual sending and the delivery-status
reconciliation. Duplicate protection is a UNIQUE(user_id, rule_id)
constraint in the DB -- not an if-check in Python -- because that's the
only version of "don't double DM" that survives two requests racing
each other.
"""
import hmac
import hashlib
import json
import uuid
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from app.db import init_db, get_conn, bump_counter
from app import worker
from app import pseudogram_client as pg


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await worker.startup_recover()
    tasks = [
        asyncio.create_task(worker.sender_loop()),
        asyncio.create_task(worker.reconciler_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="LinkPlease mini", lifespan=lifespan)


class RuleIn(BaseModel):
    keyword: str
    dm_message: str


@app.get("/")
def root():
    return {"status": "up"}


@app.post("/rules", status_code=201)
def create_rule(rule: RuleIn):
    rule_id = str(uuid.uuid4())
    conn = get_conn()
    conn.execute(
        "INSERT INTO rules (rule_id, keyword, dm_message, created_at) VALUES (?, ?, ?, ?)",
        (rule_id, rule.keyword, rule.dm_message, now_iso()),
    )
    conn.commit()
    conn.close()
    return {"rule_id": rule_id, "keyword": rule.keyword, "dm_message": rule.dm_message}


def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    # read pg.API_KEY at call time, not import time -- learned this one
    # the hard way while testing (import copies the value once, so if the
    # key gets set after the module loads, the old check never sees it)
    api_key = pg.API_KEY
    if not api_key:
        # no key configured (e.g. running locally without one yet) -- don't
        # pretend to verify something we can't actually check
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(api_key.encode(), raw_body, hashlib.sha256).hexdigest()
    given = signature_header.split("sha256=", 1)[1]
    return hmac.compare_digest(expected, given)


@app.post("/webhook")
async def webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-PseudoGram-Signature", "")

    if not verify_signature(raw_body, signature):
        # deliberately not telling them *why* -- don't want to help someone
        # brute-force a forged signature
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad json")

    event_id = event.get("event_id")
    event_type = event.get("event_type")
    data = event.get("data", {})

    if not event_id:
        raise HTTPException(status_code=400, detail="missing event_id")

    conn = get_conn()
    try:
        # atomic dedup: only one caller (even across processes) will get
        # rowcount=1 for a given event_id, no matter how many times or how
        # close together it's redelivered
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_events (event_id, event_type, received_at) VALUES (?, ?, ?)",
            (event_id, event_type, now_iso()),
        )
        conn.commit()
        if cur.rowcount == 0:
            # already saw this event_id, this is just a redelivery
            return {"ok": True, "note": "duplicate event, ignored"}

        if event_type == "comment.deleted":
            comment_id = data.get("comment_id")
            conn.execute(
                "INSERT OR IGNORE INTO deleted_comments (comment_id, deleted_at) VALUES (?, ?)",
                (comment_id, now_iso()),
            )
            # if we had a DM queued for this comment and haven't sent it
            # yet, don't -- someone deleted the comment that triggered it
            conn.execute(
                "UPDATE dm_jobs SET status='cancelled', updated_at=? WHERE comment_id=? AND status='pending'",
                (now_iso(), comment_id),
            )
            conn.commit()
            return {"ok": True}

        if event_type == "comment.created":
            comment_id = data.get("comment_id")
            text = (data.get("text") or "")
            user_id = data.get("from", {}).get("user_id")

            already_deleted = conn.execute(
                "SELECT 1 FROM deleted_comments WHERE comment_id = ?", (comment_id,)
            ).fetchone()
            if already_deleted:
                return {"ok": True, "note": "comment already deleted, skipping"}

            rules = conn.execute("SELECT * FROM rules").fetchall()
            text_lower = text.lower()
            for rule in rules:
                if rule["keyword"].lower() in text_lower:
                    try:
                        conn.execute(
                            """INSERT INTO dm_jobs
                               (user_id, rule_id, comment_id, message, status,
                                idempotency_key, attempts, created_at, updated_at)
                               VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?)""",
                            (
                                user_id,
                                rule["rule_id"],
                                comment_id,
                                rule["dm_message"],
                                f"{rule['rule_id']}:{user_id}",
                                now_iso(),
                                now_iso(),
                            ),
                        )
                        conn.commit()
                    except Exception:
                        # UNIQUE(user_id, rule_id) tripped -- this person
                        # already got (or is getting) this DM
                        conn.rollback()
                        bump_counter("duplicates_blocked")
            return {"ok": True}

        # some other event_type we don't know about -- accept and ignore
        return {"ok": True, "note": f"unhandled event_type {event_type}"}
    finally:
        conn.close()


@app.get("/stats")
def stats():
    conn = get_conn()
    rows = conn.execute("SELECT status, COUNT(*) as c FROM dm_jobs GROUP BY status").fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    dup_row = conn.execute("SELECT value FROM stats_counters WHERE name='duplicates_blocked'").fetchone()
    conn.close()

    return {
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
        "queued": counts.get("pending", 0) + counts.get("queued_remote", 0),
        "duplicates_blocked": dup_row["value"] if dup_row else 0,
    }
