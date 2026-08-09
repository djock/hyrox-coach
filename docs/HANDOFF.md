# Handoff — finish the week-based refactor, then auto-deploy

**Repo:** `~/Projects/Web/hyrox-coach` (remote `git@github.com:djock/hyrox-coach.git`)
**HEAD:** `cabae5f` — *Add design brief; begin week-based queue refactor*
**Working tree:** dirty, 7 modified files, **nothing committed since `cabae5f`**
**Tests:** `139 passed, 5 failed`
**Production:** live at https://hyrox.miloprogressive.fit, still running the
commit *before* this refactor. Untouched and healthy. Do not deploy until the
suite is green and the migration has been rehearsed against a copy of the prod
database (§4).

Run tests with `.venv/bin/python -m pytest -q`. Run the app with `./scripts/dev.sh`
(port 8099). Do **not** `source secrets/secrets.env` — bcrypt hashes contain `$`
and sourcing expands them away; `dev.sh` reads it with `read` instead.

---

## 1. Why this refactor exists

The old model advanced a plan-wide *session* pointer. The plan holds 240 sessions
across 48 weeks (5/week) but the target is 4/week and the floor is 3, so
`sessions_remaining / rate` always implied more than 52 weeks. Consequences:

- With **perfect** adherence the projected date moved a week later *every week*
  (2 Aug → 29 Aug over 12 weeks).
- Unfinished optional sessions never expired; they queued up, so after 12 perfect
  weeks the athlete was only on plan week 10 and drifting.

**The new model:** `progress.plan_week` (1..48) is the position. The plan advances
one week when the weekly floor is met, and repeats the week otherwise. Sessions
unfinished at week close simply expire. Projection counts *plan weeks remaining
÷ progress rate*, so keeping up holds the baseline date exactly.

This is already implemented and tested in `engine.py` — see
`test_perfect_adherence_holds_the_baseline_date`, the regression this exists for.

## 2. What is already done

- **`db.py`** — `SCHEMA_VERSION = 2`; `progress.plan_week` and
  `week_outcomes.plan_week` added; `_migrate_to_week_based()` backfills
  `plan_week` from the old pointer's `global_week` via guarded
  `ALTER TABLE … ADD COLUMN`. Additive and idempotent.
- **`engine.py`** — `WeekContext` carries `plan_week` / `total_plan_weeks`
  instead of `sessions_remaining`; new `progress_rate()` (window 4, clamp
  `[0.25, 1.0]`, bootstraps to 1.0); `project_race_date()` counts weeks;
  `WeekOutcome` gained `plan_week` and `advance_plan_week`;
  `phase_exit_status()` takes `plan_week` instead of `sessions_completed`.
- **`plandata/phase-*.yaml`** — exit criteria switched from `sessions_completed`
  to `plan_week` (12 / 24 / 36 / 48).
- **`service.py`** — `logged_slugs()`, `current_week_sessions()` (floor sessions
  sorted first), `current_session()` bounded to the current plan week and
  returning `None` when the week is complete, `_refresh_pointer()` replacing the
  plan-wide walk, `persist_outcome()` writing `plan_week` and applying
  `advance_plan_week`, `plan_overview()` marking past weeks `missed`,
  `dashboard_state()` exposing `plan_week` / `total_plan_weeks` /
  `weeks_remaining` / `week_complete`.
- **Templates** — `athlete.html` (week-complete state, "week X of 48", this
  week's session list, "Race ready by" wording), `plan.html` (missed vs pending,
  current-week pill), `coach.html` (plan-week column). CSS for `.weeklist` and
  `.plan-missed` appended.
- **`tests/test_engine.py`** — updated for the new context; 9 new tests covering
  `progress_rate`, the baseline regression, and plan-week advancement. **All 34
  engine tests pass.**

## 3. What remains

### 3.1 Fix the five failing tests

All five assert the old session-pointer model. The production code is believed
correct; these are test updates, but **verify each rather than assuming** — if
one reveals a real bug, fix the code instead.

| Test | Why it fails | Fix |
|---|---|---|
| `test_service.py::test_the_substitution_shows_up_in_the_queue` | Sets `pointer_slug = 'p1-w05-s2'` directly; position is now `plan_week` | Set `progress.plan_week = 5`, then assert `current_session()` substitutes the bike |
| `test_service.py::test_phase_does_not_advance_without_its_manual_criterion` | Logged 40 sessions to satisfy a `sessions_completed` criterion that no longer exists | Drive `plan_week` to 12 (log 3+/week and evaluate, or set it directly), assert no advance without the manual confirmation |
| `test_service.py::test_coach_confirmation_can_complete_a_phase` | Same cause | Same approach, then `confirm_phase_manual()` and assert `advance_phase_if_ready()` is True |
| `test_service.py::test_dashboard_state_reports_the_plan_as_complete_at_the_end` | Sets `pointer_slug = NULL` | Set `plan_week = 49`; `plan_complete` is now `plan_week > total_plan_weeks` |
| `test_web.py::test_api_state_reports_the_queue` | `sessions_remaining` key removed from `/api/state` | Assert `weeks_remaining == 48` and `plan_week == 1` |

### 3.2 Add the missing service-level tests

Named in the approved plan, not yet written. Put them in `tests/test_service.py`:

- Perfect adherence ⇒ `plan_week == calendar weeks elapsed + 1`.
- A sub-floor week does **not** advance `plan_week`, and the same sessions are
  offered again the following week.
- Optional sessions left unfinished at week close never reappear (log only the
  3 floor sessions, evaluate, assert the 2 optional slugs are not in
  `current_week_sessions()` next week).
- A paused week neither advances `plan_week` nor spends buffer.
- **Migration test**: build a v1-shaped database (schema without `plan_week`,
  `pointer_slug` set mid-plan), run `db.migrate()`, assert `plan_week` is
  backfilled from that slug's `global_week` and no `session_events` are lost.

### 3.3 Verify by hand

```bash
.venv/bin/python scripts/reset.py --start 2026-08-03   # empty DB
./scripts/dev.sh
```

1. Log 4 sessions in week 1, `hyrox-job evaluate`, confirm the ready date equals
   the baseline and `plan_week` is 2.
2. Log 2 sessions in week 2, evaluate, confirm `plan_week` stays at 2 and the
   same sessions are offered again.
3. Open `/plan` — week 1's unfinished optional sessions must read `missed`.
4. Open `/` — the week list, "week X of 48", and "Race ready by" all render.

### 3.4 Rename `race_date` → `ready_date` (optional, do last)

The user wants this framed as a projected **race-ready** date, never a deadline.
UI wording is already changed. The identifiers are not:
`progress.baseline_race_date`, `progress.projected_race_date`,
`week_outcomes.projected_race_date`, `WeekContext.baseline_race_date`,
`WeekOutcome.projected_race_date`, `project_race_date()`. Renaming the DB columns
means schema v3; renaming only the Python/template layer avoids that. Either is
fine — do not leave it half-done.

### 3.5 Auto-deploy on push (Part B, not started)

The Pi's existing runner is bound to `djock/milo-coach`
(`actions.runner.djock-milo-coach.milo-pi.service`, label `milo-pi`). Runners on
user-owned repos cannot be shared, so hyrox needs its own.

1. On the Pi (`admin@raspberrypi.local`), create `~/actions-runner-hyrox`,
   download the runner, and configure it against `djock/hyrox-coach` with label
   `hyrox-pi`. Registration token:
   `gh api -X POST repos/djock/hyrox-coach/actions/runners/registration-token`
   (`gh` is already authenticated as `djock` on the laptop). Install as a service
   with `./svc.sh install && ./svc.sh start`.
2. Add `.github/workflows/deploy.yml`, copied from
   `~/Projects/Web/milo_coach/.github/workflows/deploy.yml`, changing
   `working-directory` to `/home/admin/Projects/hyrox`, the label to `hyrox-pi`,
   and `concurrency.group` to `deploy-pi-hyrox`. Reuse it as-is otherwise: it
   updates the checkout in place with `git reset --hard origin/main` (never
   `git clean`, so untracked `data/` and gitignored `secrets/` survive) and
   rebuilds only when image inputs changed.

## 4. Deploying the migration safely

The live database has real (though currently empty) state and a v1 schema.
Rehearse before pushing:

```bash
scp admin@raspberrypi.local:/home/admin/Projects/hyrox/data/hyrox.sqlite /tmp/prod-copy.sqlite
cd ~/Projects/Web/hyrox-coach
HYROX_DB_PATH=/tmp/prod-copy.sqlite .venv/bin/python -c "
from hyrox import db; from pathlib import Path
c = db.connect(Path('/tmp/prod-copy.sqlite')); db.migrate(c)
print(dict(db.fetch_progress(c)))"
```

Expect `plan_week` present and sane, and no exception. Then deploy and confirm
`https://hyrox.miloprogressive.fit/healthz` returns 200.

## 5. Context worth knowing

- **Credentials.** Production passwords are in the `cs` keychain:
  `cs hyrox -secrets get HYROX_PW_DRAGOS` / `HYROX_PW_IONUT`. The Cloudflare API
  token is `cs milo -secrets get CLOUDFLARE_API_TOKEN`. `ws -secrets get` is
  broken — do not use it.
- **The design brief** (`docs/DESIGN-BRIEF.md`) has gone to a designer. It
  specifies a Monday–Sunday **column** week board with per-day completion, which
  the current single-session-card layout does not implement. Expect the templates
  to be reworked; avoid heavy investment in their current styling.
- **`scripts/reset.py`** wipes the DB (`--demo` seeds a sample week). Default is
  empty on purpose: fabricated progress in a training log reads as a bug.
- **Docker is not running on the laptop**, so the image build is unverified
  locally; it builds fine on the Pi.
- Two environment traps that have already cost time: session cookies are issued
  `Secure`, so any test client must use an `https://` base URL or every authed
  request silently 401s; and never `source secrets/secrets.env`.
