# FAILURES.md

Things I know are broken or would break under the right conditions. Tested
with `/v1/simulate/start` a few times, not exhaustively.

1. **Rate limiter resets on restart.** The 10-req/60s window is tracked in
   an in-memory `deque`, not the DB. If the process restarts, it forgets
   what it already sent in the last 60 seconds, so right after a restart
   it could burst past 10 requests before the mock API's own limiter
   catches it and starts 429ing me back into line. A real fix is
   persisting the timestamp window somewhere durable.

2. **Retry-after during a crash is lost, but not the job.** If the process
   dies while a job is sitting in `pending` with a future `next_attempt_at`
   (e.g. mid-backoff after a 500), that's fine — it's in sqlite, it'll get
   picked up again on restart. What's *not* fine: if it dies in the
   narrow window after `send_dm()` got a 202 back but before the DB
   `UPDATE` to `queued_remote` commits, we've now sent a DM the DB has no
   record of ever being accepted. The reconciler will never look for it
   because it doesn't have the `dm_id`. Small window, but it's real.

3. **Reconciler retry assumes idempotency keys are safe to reuse-ish.**
   When a `queued_remote` DM comes back `failed` on reconciliation, I flip
   it back to `pending` and let the sender loop retry it with a fresh
   attempt count, using the same base idempotency key
   (`{rule_id}:{user_id}`) suffixed with the new attempt number. I'm
   assuming a new attempt number is enough to make it a "different"
   request from the API's point of view. I didn't get confirmation of
   exactly how long the API remembers an idempotency key or whether it
   scopes it by request body too, so if it treats it as a true dedupe on
   the key alone regardless of attempt suffix, this retry could just
   silently return the old failed dm_id forever.

4. **Two dm_jobs can both reach `_attempt_send` in the same batch if the
   worker loop free-runs faster than a single event's DB write commits.**
   In practice the `UNIQUE(user_id, rule_id)` constraint stops duplicate
   *jobs* from ever being created, so this isn't a duplicate-DM bug — but
   it does mean the sender loop could in theory pick up the same row
   twice in adjacent batches if a batch takes longer than 1 second to
   process (unlikely at low volume, more likely if the mock API is slow
   under the 500-events-in-10s test). I didn't add a `SELECT ... FOR
   UPDATE`-style lock on job rows because sqlite doesn't really do that;
   a Postgres version of this would fix it properly.

5. **`GET /stats` under heavy concurrent write load hasn't been stress
   tested past a couple hundred events on my machine.** The counts come
   from a live `GROUP BY` over `dm_jobs`, so they should always be
   accurate at read time, but I haven't run a full 500-in-10s burst
   against a real deployed instance yet (only local, without hitting the
   real rate limits and 500-error rates of the actual mock API) — so I
   can't promise it holds up exactly as tested until I do.
