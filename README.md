# LinkPlease mini — intern assignment

FastAPI service that receives Instagram comment webhooks, matches them
against keyword rules, and sends DMs through the mock PseudoGram API —
without losing DMs or double-sending them when the API misbehaves.

Parts done: **A + B + most of C** (reconciliation and delete-handling are
in; haven't run a real 500-events-in-10s burst against the *live* mock
API yet, only local tests — see `FAILURES.md`).

## How it's built

- `app/main.py` — the three required routes (`/webhook`, `/rules`, `/stats`)
- `app/worker.py` — background loop that actually sends DMs and retries them
- `app/pseudogram_client.py` — talks to the mock API, handles the rate limit
- `app/db.py` — sqlite schema. `dm_jobs` has `UNIQUE(user_id, rule_id)` —
  that constraint, not app logic, is what actually stops double-DMs.

The webhook handler never calls the DM API directly. It writes a row and
returns. A background loop (`sender_loop`) picks up pending rows and does
the actual sending, so a slow or failing DM call can't make `/webhook`
blow past the 5-second limit. A second loop (`reconciler_loop`) polls
`GET /v1/dm/{id}` for anything we got a `202` on, and retries it if it
later comes back `failed`.

## Running it locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export PSEUDOGRAM_API_KEY=your_key_here
uvicorn app.main:app --reload
```

Without `PSEUDOGRAM_API_KEY` set, signature verification is skipped
(there's nothing to check it against) — useful for poking at `/rules`
and `/webhook` locally before you have a key, but set it before you
deploy or test against the real mock API.

## Testing against the mock API

Once deployed (or running with a public URL via ngrok/similar):

```bash
curl -X POST https://pseudogram-api.onrender.com/v1/simulate/start \
  -H "X-API-Key: $PSEUDOGRAM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://your-app-url/webhook", "count": 500, "duration_seconds": 10}'
```

Then compare `GET /stats` on this app against
`GET /v1/simulate/{run_id}/truth`.

## Env vars

| Var | Required | What |
|---|---|---|
| `PSEUDOGRAM_API_KEY` | yes, before real testing | your key, used both to call the mock API and to verify incoming webhook signatures |
| `PSEUDOGRAM_BASE_URL` | no | defaults to `https://pseudogram-api.onrender.com` |
| `DB_PATH` | no | defaults to `linkplease.db` next to the app |

## Deploying

Any host that runs a long-lived Python process works (Render, Railway,
Fly.io). It needs to be long-lived, not serverless-per-request, because
the background sender/reconciler loops need to keep running between
webhook calls. A `Procfile` is included for Render/Railway-style
buildpacks:

```
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

One thing to know: `DB_PATH` should point at a persistent disk if the
host wipes the filesystem on redeploy (e.g. Render's free tier does,
unless you attach a disk) — otherwise pending jobs get lost on redeploy,
same as any restart-without-persistence issue.
