# Hyrox Coach

Self-hosted training tracker for one athlete and one coach, covering a 12-month
Hyrox Doubles build. The athlete logs sessions in one tap; the plan adapts to the
adherence he actually manages; the coach sees everything and can comment.

Exposed via Cloudflare Tunnel — no open router ports. Same shape as `milo_coach`.

## The one idea

**Adherence is the product.** The athlete this was built for is detrained, 37,
and describes himself as lazy. A physiologically optimal plan completed 40% of
the time is worse than a decent plan completed 85% of the time, so nearly every
design decision trades something else away for the chance he opens the app.

That produces three things you would not otherwise build:

- **The plan is a queue, not a calendar.** Sessions advance when completed.
  Nothing is scheduled for a date, so he never opens the app to a backlog of
  missed work. The race date is *derived* from his real training rate.
- **A four-week buffer absorbs bad weeks before the date moves.** The UI shows
  slack remaining rather than a receding finish line, and slack regenerates
  after four weeks at target — one bad month must not be unrecoverable.
- **The floor is a success condition, not a target.** Three sessions is a good
  week, full stop. Anything above is a bonus and carries no penalty when missed.

## How the plan is stored

`src/hyrox/plandata/*.yaml` holds four phases of weekly *slots*, which
`plan.py` expands into 240 concrete sessions. Two properties matter:

- **Slugs are immutable.** `p1-w03-s02` identifies a session forever. Logs
  reference slugs, so revising the plan in month 4 cannot silently repoint
  March's easy run at a deadlift.
- **The plan has a content hash.** Editing the YAML produces a new revision;
  old revisions are retired rather than mutated, and every logged event carries
  a frozen snapshot of what it actually said.

Revising the plan is therefore a git commit and a redeploy, not a migration.

## Adaptation

`engine.evaluate_week(ctx) -> WeekOutcome` is pure and idempotent. Everything it
needs arrives in an explicit `WeekContext`; the result is persisted once to
`week_outcomes` and never recomputed, so history stays stable even as logs are
voided underneath it.

The projection carries four guards:

| Guard | Why |
|---|---|
| Rate counts distinct training days, max 2 credited per day | Batch-logging ten sessions on a Sunday cannot fake a heroic rate |
| Rate clamped to `[1.0, 5.0]` | No division by zero; a three-week absence cannot project a race date three years out |
| Planned rate used until 4 weeks of history exist | Week 1 has no trailing window |
| The date moves at most ±1 week per evaluation | A date that swings by months and snaps back is a number nobody trusts |

## Time

All week logic is ISO weeks, Monday-start, in `Europe/Bucharest`. Each event
stores a local `training_date` separately from its UTC `completed_at`, so a
session finished at 00:15 on Monday counts for Sunday — which is what the
athlete means, and stops a phantom missed week burning buffer.

## The one automatic intervention

Two sessions carrying `counts_as: run_exposure`, within 14 days, both reporting
the same lower-limb pain location at ≥ 3, automatically substitute the next run
for its declared bike equivalent.

Pain ≥ 5 is a **stop**, not a hold: that modality is blocked, the coach is
alerted, and it stays blocked until acknowledged. An app should not quietly keep
programming through reported pain.

## Swaps

Each template declares its own `alternatives`. They are authored, never selected
from the queue, so a hard session cannot be swapped for an easy one from three
months ahead and deferred indefinitely. One voluntary swap per ISO week;
automatic impact swaps are free. Strength A/B alternates deterministically —
free choice means Strength A forty times.

## Local development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest

cp secrets.env.example secrets/secrets.env
.venv/bin/python scripts/hash_password.py     # once per user
./scripts/dev.sh                              # http://localhost:8099
```

`secrets/secrets.env` is stored **raw** because compose reads it with
`format: raw`. bcrypt hashes contain `$`, so `source`-ing that file would expand
them away and every login would fail with a confusing 401 — `scripts/dev.sh`
reads it with `read` instead, which does no expansion.

## Deploy to the Pi

```bash
rsync -av --exclude .venv --exclude .git --exclude data \
  ~/Projects/Web/hyrox-coach/ admin@raspberrypi.local:/home/admin/Projects/hyrox/

ssh admin@raspberrypi.local
cd /home/admin/Projects/hyrox
cp secrets.env.example secrets/secrets.env   # first time only
nano secrets/secrets.env                     # fill in real values
docker compose up -d --build
```

The account is `admin@`, not `pi@`. Each project on the Pi runs its own
`cloudflared` sidecar with its own `TUNNEL_TOKEN` — there is no shared tunnel to
add a hostname to, so `hyrox` needs a tunnel created in the Zero Trust dashboard
(public hostname `hyrox.miloprogressive.fit`, service `http://hyrox:8000`).

## Cron

```
0  3 * * 1   hyrox-job evaluate   # Monday 03:00 local: close last week
0 18 * * 4   hyrox-job nudge      # Thursday evening, only if the floor is at risk
5  3 * * 1   hyrox-job digest     # weekly summary to the coach
30 2 * * *   hyrox-job backup     # nightly snapshot, 14-day retention
```

Restore with `hyrox-job restore --snapshot data/backups/hyrox-YYYY-MM-DD.sqlite`.
It is covered by a round-trip test, because an untested restore path is not a
backup.

## Layout

```
src/hyrox/
  plan.py       compiles plandata/*.yaml into 240 immutable sessions
  db.py         schema, migrations, seeding, plan revisions
  engine.py     pure adaptation logic — rate, buffer, status, phases
  service.py    queue, logging, swaps, impact rule, benchmarks
  web.py        routes
  auth.py       sessions, roles, CSRF
  jobs.py       cron entry points
  notify.py     Discord, best-effort
```
