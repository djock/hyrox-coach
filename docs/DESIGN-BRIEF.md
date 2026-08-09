# Hyrox Coach — design brief

A brief for designing the interface. The backend exists and works; this describes
what it does, who it is for, and what the screens need to carry. Treat the visual
direction as wide open — what follows is the substance, not the styling.

---

## 1. What this is

A private training tracker for **one athlete and one observer**, covering a
12-month build toward a Hyrox Doubles race. Two people, one URL, no signups, no
marketing pages, no settings screen. It lives on a phone.

**Dragos** is the athlete. 37, 1.90 m, ~87 kg. He used to run 10 km occasionally,
years ago; he is detrained now and has essentially no strength-training history.
He trains at home with **one pair of 14 kg dumbbells and a stationary bike** for
the first six months, then joins a gym. His own description of himself is *lazy*.
He asked for: a weekly schedule with checkboxes, the ability to pick strength
workout A or B, somewhere to say how he felt (especially pain), a monthly
checkpoint with a progress bar, and every session clickable to see what it
actually is.

**Ionut** is his training partner, not his coach. He built this. He wants to see
whether Dragos is actually training, and to leave the occasional comment. He is
not in charge of Dragos's plan — Dragos controls everything about his own
training.

They will race together as a pair. Goal: **finish, uninjured.** Not a time.

## 2. The one idea everything serves

**Adherence is the product.** A physiologically perfect plan followed 40% of the
time is worse than a decent plan followed 85% of the time. Dragos is the kind of
person who quits in week five. Every design decision — including visual ones —
should be judged on a single question: *does this make him more likely to open
the app tomorrow?*

Three consequences that shape the whole interface:

**The week is the unit, and three sessions wins it.** The plan asks for 4–5
sessions but the *floor* is 3. Hitting three means the week was a success — not
"partially complete", not amber, a success. This should feel unambiguous and
good. Sessions beyond three are a bonus and their absence carries no penalty at
all.

**Nothing ever accumulates.** Unfinished sessions expire when the week closes.
He never opens the app to a backlog, a debt, or a list of things he failed to do.
The plan waits for him; it does not pile up behind him.

**Honesty over gamification.** No streaks-as-guilt, no fake urgency, no
notifications shaming him. The numbers shown are real and he can verify them.
The motivation comes from the work being visibly survivable, not from pressure.

## 3. Race-ready date, not a deadline

There is **no fixed race date and no countdown to a booked event.** What the app
shows is a **projected race-ready date**: the date by which, at his current rate,
he will have completed the twelve-month build and be ready to race. It is a
forecast, not a commitment. The wording throughout should make that obvious —
"ready by", not "race day", and never a ticking clock.

How it moves:

- The plan is 48 weeks of training plus **4 weeks of slack** ("buffer").
- Hit the floor in a calendar week and the plan advances one week. Plan and
  calendar stay in step, and the ready date does not move.
- Miss the floor and the plan week repeats — and one week of slack is spent.
- **While slack remains, the ready date does not move at all.** The UI shows
  slack remaining rather than a receding date, because "you have 3 weeks of
  slack left" is motivating and a finish line walking away from you is not.
- Only once slack is exhausted does the date start sliding, by at most one week
  per week, so it never lurches.

Design implication: **slack remaining is a first-class number**, arguably more
important than the date itself. It is the thing he can protect.

## 4. What happens when he misses — the full ladder

Worth designing for explicitly, because most of the emotional weight of the app
lives here.

| Situation | What the app does |
|---|---|
| Missed a session, still hit 3 | Nothing at all. Successful week. |
| Below 3 in a week | Plan week repeats at the same loads. One week of slack spent. Status: amber. |
| Two sub-floor weeks running | Status: red. Another week of slack spent. |
| Three or more | **Re-entry mode.** Floor drops to 1, loads drop to ~70%, status becomes "restart" — deliberately neither green nor red. |
| He comes back | A single session clears re-entry. Nothing to make up, ever. |
| Holiday, illness, injury | He can mark a **pause**. The floor is suspended, no slack is spent, status is "paused". This is how a planned break stops looking like quitting. |

The states to design for are therefore: **green** (floor met), **amber** (missed
once), **red** (missed twice), **restart** (coming back after an absence, must
feel encouraging), **paused** (deliberate break, neutral). Red must clear the
moment its cause clears — no penalty box.

## 5. The screens

### 5.1 The week board — the main screen

**This is the app.** Dragos opens it and sees the whole week laid out as
**columns, Monday through Sunday**. Each day shows either its workout or that it
is a rest day. Each day carries a **check** he can tap to mark it done, and once
done it should read unmistakably as done at a glance.

Around the board, the meta:

- Progress toward the floor this week ("2 of 3") — the most important number on
  the screen
- Progress bar toward the goal, and the projected race-ready date
- Slack remaining
- Current phase and plan week ("week 7 of 48")

Notes for the layout:

- Seven columns must survive a narrow phone. Horizontal scroll, a stacked
  fallback, or a compressed rest-day column are all fair game.
- Rest days are not filler — they are prescribed, and should look intentional
  rather than empty.
- The plan is *not* pinned to specific weekdays. Sessions are ordered but he
  chooses which day to do each on, so the board is a week's worth of work to
  distribute, not a fixed timetable. This is a genuine design problem worth
  solving well: it needs to feel like a plan without implying Tuesday is
  compulsory.
- A session tapped once should log it in **one tap**. Details (how it felt, RPE,
  pain) come on a second, entirely skippable screen. Never block a completion
  behind a form.

### 5.2 Session detail

Every session is clickable and shows the actual workout: exercises, sets, reps,
loads, and a short coaching note per exercise explaining the point of it. Some
sessions declare an **alternative** (typically the bike instead of a run) that he
may swap to once per week.

### 5.3 The phase and plan screen

He should be able to see the whole twelve months: **four phases**, each with its
purpose, what it contains, and the workouts inside it, week by week. Completed
work marked, current position marked, missed work honestly marked as missed.

The four phases:

1. **Show up** (months 1–3, home) — build the habit, restore impact tolerance,
   learn to squat and hinge. Two weeks of walking before any jogging.
2. **Engine and leg capacity** (months 4–6, home) — aerobic base, and the leg
   endurance wall balls and lunges will demand.
3. **Stations and partner** (months 7–9, gym) — all eight Hyrox stations,
   compromised running, training as a pair.
4. **Rehearse and taper** (months 10–12, gym) — prove readiness, rehearse the
   race, arrive healthy.

This screen carries a lot of content (240 sessions) and needs real information
design — it should reward browsing rather than overwhelm.

### 5.4 Monthly checkpoint

Every fourth week, a four-test battery, about 25 minutes. He enters four numbers
and the app shows each against his own previous month. This is the "am I actually
getting fitter" screen, and it is where the slow, real progress shows up.

Tests change at the gym transition: at home it is a 12-minute run, goblet
thrusters, farmers carry, split squats; in the gym it becomes the 12-minute run,
wall balls, heavier farmers carry, sandbag lunges. The 12-minute run runs the
whole year so one line spans all twelve months.

**A skipped checkpoint is not a failure** and must never block anything — it
simply reads as "not tested".

### 5.5 The observer view

Ionut's screen. Adherence over time, current streak, pain flags, which sessions
were swapped or skipped and why, benchmark trends, and a comment box per session.
Read-and-comment, not control.

## 6. Two things that need care

**Pain reporting.** After every run, and periodically otherwise, he is asked
whether anything hurt — a 0–10 score and a location from a fixed list (ankle,
knee, shin, hip, back, shoulder, other). It is prompted, never mandatory,
because a required field just teaches him to type zero to escape it.

Two things follow automatically:

- Same lower-limb pain reported after two runs within 14 days → the app
  **silently moves his next run to the bike** and tells him why. This is the only
  place the app changes his plan without being asked.
- Pain of 5 or more → that kind of training **stops** until he explicitly
  resumes it, giving a written reason. Ionut is notified either way.

This needs to feel like care, not surveillance. He is 37, returning to running at
87 kg, and an injury in month three ends the whole project.

**Strength A / B.** Two alternating strength workouts. The app alternates them
automatically and correctly; he can override. He asked to "set A or B", and the
design should honour that without making it a decision he has to get right —
free choice means he does the easier one forty times.

## 7. Constraints

- **Phone first.** He will use this on a phone, standing in a gym or a living
  room, possibly with no signal. Desktop is a nice-to-have.
- **Installable as a PWA** — home-screen icon, opens like an app.
- **Works offline.** Completions queue locally and sync later.
- **Server-rendered** (FastAPI + Jinja2). Rich CSS is very welcome; a heavy JS
  framework is not. Interactions should degrade to plain form posts.
- **Light and dark**, following the system.
- **English.**
- Self-hosted on a Raspberry Pi behind Cloudflare, two accounts, one login ever.

## 8. What we want from you

Freedom. The current interface is functional and plain — it was built to prove
the mechanics, not to be looked at for a year. What it needs now is to be
something Dragos *wants* to open on a Tuesday evening in February when he is
tired and it is raining.

Please ask questions before designing. Things genuinely undecided and worth
pushing on:

- How the week board handles seven columns on a phone without becoming a
  spreadsheet
- How to show "3 is a success, 5 is a bonus" without making the bonus sessions
  look like failures
- Whether slack remaining, the ready date, or the weekly floor is the hero number
- How rest days earn their space
- How to make the 240-session plan screen browsable rather than exhausting
- What "restart" should feel like — it is the most emotionally important state in
  the app and the one most likely to decide whether he continues
