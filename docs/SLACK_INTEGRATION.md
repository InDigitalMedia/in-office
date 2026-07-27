# Slack Integration

Lets people fill in their week and see who's at Neal Street without leaving Slack. This doc covers what it does, where its credentials live, how to set it up from scratch (for a handover or a fresh workspace), and how to add new team members so they're picked up correctly.

## What it does

- **`/enter-week` slash command** — opens a modal to fill in your week directly (Mon–Fri). Each day has a "Split into morning/afternoon" checkbox; unchecked (the default) shows one location field, checked shows two independent ones, each able to be any location (including its own Client Office/Other client name). Re-running it pre-fills whatever you've already entered that week, split days included.
- **Quick-fill buttons** ("🔁 Same as last week" / "✏️ Fill in week") — shown in the daily reminder DM (a bot can't open a modal without a fresh interaction, so the DM needs a button first; the slash command skips straight to the modal since it already has one). Both open the same modal, titled "Same as last week" pre-filled from last week's entries for the former (split days carried over just like full days), so the user can review and edit before explicitly submitting rather than a week being saved unseen.
- **Daily reminder DM** — every weekday morning (~9:05am), anyone who hasn't filled in their week yet gets DMed the quick-fill buttons, addressed to them by name (`Hey @Name — ...`). Runs as its own scheduled action, independent of the digest below.
- **Afternoon reminder DM** — every weekday afternoon (~2pm), anyone *still* unfilled gets a second nudge ("⏰ Last call today — don't forget to fill in your week!"). A separate scheduled action with its own dedup tracking from the morning reminder, so both can send the same day independently.
- **Daily office digest** — every weekday morning, a message goes to a configured channel: "*:coffee: Good morning everyone! Here's who will be in the office today :point_down:*", followed by the day's Neal Street/Client Office breakdown, a "📅 See Full Schedule" button, and a "✏️ Fill My Week" button. Runs as its own scheduled action, independent of the reminder above.
- **Tomorrow/next-week office digest** — every weekday afternoon (4pm London), a message goes to the same channel with who's in tomorrow ("*:wave: Good afternoon everyone! Here's who will be in the office tomorrow :point_down:*"). On Fridays, "tomorrow" would be Saturday (not useful), so it instead posts the whole of next week's schedule ("*:wave: Good afternoon everyone! Here's who will be in the office next week :point_down:*") — one action, branching by day, rather than a separate job. Both also end with "📅 See Full Schedule" + "✏️ Fill My Week" buttons.
- **"✏️ Fill My Week" button** — on the daily/tomorrow/next-week digests, opens the week modal directly for whoever clicks it (for that message's own week — this week for the daily/tomorrow digests, next week for the Friday variant), reusing the same click-to-modal mechanism as the "Fill in week" quick-fill button. Not shown on the post-submission week summary (redundant right after someone just submitted).
- All of the above (today's digest, tomorrow/next-week digest, and the post-submission week summary) show **Neal Street and Client Office** on separate lines, each with a headcount (e.g. "🏢 Neal Street (5)"), matching the tracker website's Who's Where grouping — Client Office is further broken out by client name (e.g. "Sky: @Alice"). Other locations (WFH, Holiday, etc.) aren't shown, since these messages are specifically answering "who's in an office". Header wording says "in the office" generically; the Neal Street/Client Office labels stay as-is within the breakdown itself.
- **Next-week reminder DM** — every Friday afternoon (2pm London), anyone who hasn't yet entered *next* week's locations gets the same quick-fill buttons, reworded for next week. "Same as last week" here naturally means "same as this week", since it's relative to next week's Monday.
- **Week summary** — after successfully saving (via the modal or "Same as last week"), you privately get an Officely-style breakdown ("Here's who's in the office this week") of who's at Neal Street/Client Office each day that week, with a "📅 See Full Schedule" link back to the web app.

## Architecture (brief)

- Backend: `backend/slack_routes.py` (the 7 HTTP endpoints), `backend/slack_views.py` (Block Kit message/modal building + parsing), `backend/slack_client.py` (thin Slack Web API wrapper + request signature verification), `backend/slack_directory.py` (matches `team-members.json` names against the Slack workspace directory), `backend/daily_notifications.py` (the scheduled jobs' logic).
- The 7 endpoints, all under `app.include_router(slack_router)` in `backend/app.py`:
  - `POST /slack/commands` — the `/enter-week` slash command
  - `POST /slack/interactivity` — button clicks, modal field changes, and modal submission all come through here
  - `POST /internal/slack/daily-digest` — the ~9:15am "who's in the office today" channel post (gated by its own secret, see below); accepts an optional `?force=true` query param that bypasses the weekday/hour/already-sent gates, for manual test runs
  - `POST /internal/slack/morning-unfilled-reminders` — the ~9:05am DM reminders to anyone who hasn't filled in their week; same gating and `?force=true` behavior. Split from the digest above into its own endpoint/workflow so each can be tested or fail independently, and deliberately fires 10 minutes before it so the digest reflects anyone who just filled in from the reminder.
  - `POST /internal/slack/afternoon-unfilled-reminders` — the ~2pm second nudge to anyone still unfilled; same gating and `?force=true` behavior, with its own `ScheduledRunLog` key so it doesn't interfere with the morning reminder's dedup tracking.
  - `POST /internal/slack/tomorrow-digest` — the ~4:05pm scheduler trigger; same gating and `?force=true` behavior
  - `POST /internal/slack/next-week-reminder` — the Friday-~2:05pm scheduler trigger; same gating and `?force=true` behavior
- Scheduling: real triggering is done by **cron-job.org** (an external cron-as-a-service), which POSTs directly to the five `/internal/slack/...` endpoints above on schedule (~9:05am morning-unfilled-reminders, ~9:15am daily-digest, ~2:05pm afternoon-unfilled-reminders, ~4:05pm tomorrow-digest, Friday ~2:05pm next-week-reminder), sending the `X-Scheduler-Secret` header itself. GitHub Actions previously did this via `schedule:` crons but was retired as the live scheduler after it was observed firing hours late or not at all; the corresponding workflow files (`.github/workflows/slack-*.yml`) are kept only for manual `workflow_dispatch` force-testing (with a `force` checkbox) — they no longer have a `schedule:` trigger. Each endpoint still checks whether it's actually within the target hour window in London and no-ops otherwise — this is deliberate, so cron-job.org's schedule never needs editing for BST/GMT either.
- **Late/duplicate firing resilience**: any cron trigger source (the retired GitHub Actions scheduler, or cron-job.org) is treated as best-effort/at-least-once, not exactly-once. To cope: each gate accepts any hour within `SLACK_GATE_WINDOW_HOURS` (default 8) of its target, not just the exact hour, and a `ScheduledRunLog` DB row per job (keyed by job name, storing the last London-local date it actually sent) guarantees at most one real send per job per day even if multiple firings land inside that widened window on the same day. `force=True` bypasses both entirely and never touches the log, so manual test runs can't block or be blocked by the real scheduled send.
- Testing without spamming the team: set `SLACK_TEST_MODE_USER_NAME` (a Slack real name, e.g. "Cam Doherty") in Render to redirect **every** outbound message — channel digests and DM reminders alike — to just that person. Unset it (blank the value in the Render dashboard) to go back to normal broadcast behavior.
- Identity: there are no Slack-specific accounts. A person's identity for Slack-submitted entries is resolved from their Slack profile's real name at submission time; for the *outbound* DM reminders, `team-members.json` names are matched against the Slack workspace directory (`users.list`) by normalized name — see "Adding a new team member" below.

## Credentials — where they live

All of these are **Render environment variables** (dashboard → the `api` service → **Environment** tab), except the last:

| Variable | What it's for | How it's set |
|---|---|---|
| `SLACK_BOT_TOKEN` | Auth for all outbound Slack API calls | Manually pasted from the Slack app's OAuth & Permissions page — **not** auto-generated, `render.yaml` just declares the key exists (`sync: false`) |
| `SLACK_SIGNING_SECRET` | Verifies incoming requests really came from Slack | Same as above — manually pasted from the Slack app's Basic Information page |
| `SLACK_GENERAL_CHANNEL_ID` | Which channel gets the daily Neal Street digests (today's at 9am, tomorrow's at 4pm) | Plain value in `render.yaml`, currently `CGHDKEF6J` |
| `SLACK_SCHEDULER_SECRET` | Gates the scheduler-trigger endpoints (separate from `ADMIN_SECRET`, to scope blast radius) | Auto-generated by Render (`generateValue: true`) |
| `SLACK_SCHEDULER_SECRET` (again) | The GitHub Actions workflows need the *same* value to call the endpoints | Copied manually from Render's dashboard into **GitHub repo → Settings → Secrets and variables → Actions** — Render and GitHub don't sync this automatically, so if it's ever regenerated on one side, it needs re-copying to the other |
| `SLACK_TEST_MODE_USER_NAME` | Optional: redirects every outbound message to just this one person for testing | Manually set/unset in Render dashboard (`sync: false`, blank by default) |

**Also relevant, not Slack-specific:** `ROSTER_URL`/`CLIENTS_URL` default to fetching `team-members.json`/`clients.json` from the live frontend (`in-office.vercel.app`) — no env var needed unless overriding. `CORS_ORIGIN_REGEX` (in `backend/app.py`, not an env var by default) matches preview deployment URLs by the **Vercel project name** — if that project is ever renamed again, this needs updating too (see the comment right above it in the code, and `backend/tests/test_api.py::test_vercel_preview_cors_regex_matches_current_project_name`, which will start failing if it drifts).

## Ownership — what a handover needs to include

Unlike GitHub/Vercel/Render (tracked in `HANDOVER.md`), **the Slack app itself lives at api.slack.com/apps under whichever Slack account created it** — this repo has no record of which account that is. Before handing over ownership:

- [ ] Confirm who has admin access to the Slack app (api.slack.com/apps → the app → **Collaborators**, or ask a workspace admin) and add the new owner.
- [ ] Confirm the new owner has access to the Render dashboard (for `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`/`SLACK_SCHEDULER_SECRET`) and the GitHub repo's Actions secrets (for the copy of `SLACK_SCHEDULER_SECRET`) — both already covered by the existing GitHub/Render handover steps, just flagging that these particular secrets are Slack-specific and easy to overlook.
- [ ] If the Slack app needs to move to a different Slack workspace entirely (not just a different admin within the same workspace), it needs to be recreated from scratch there — see "Setting up from scratch" below.

## Adding a new team member

1. Add their name to `frontend/public/team-members.json` (keep the array alphabetically sorted, case-insensitive — see `CLAUDE.md`). Match it to their **Slack profile's Full Name** (Slack → their profile → this is what daily reminder matching compares against) — if it doesn't match, they won't get reminder DMs (they'd show up in `unmatched_roster_names` in the daily job's log, see Troubleshooting).
2. Nothing else is needed — `/enter-week` works for anyone in the workspace immediately (it resolves identity directly from their Slack profile, not from the roster), and they'll start getting daily reminders once their name matches.

## Setting up from scratch (new workspace, or recreating the app)

1. Create a Slack app at **api.slack.com/apps** → "From scratch" → pick the workspace.
2. **OAuth & Permissions → Bot Token Scopes**: add `chat:write`, `users:read`, `im:write`.
3. **Install to Workspace** (same page) → copy the **Bot User OAuth Token** (`xoxb-...`) → `SLACK_BOT_TOKEN`.
4. **Basic Information → App Credentials → Signing Secret** → copy it → `SLACK_SIGNING_SECRET`.
5. **Slash Commands → Create New Command**: `/enter-week` (or any name, just workspace-unique), Request URL `https://api-a8uz.onrender.com/slack/commands`.
6. **Interactivity & Shortcuts** → toggle on, Request URL `https://api-a8uz.onrender.com/slack/interactivity`.
7. Invite the bot to whichever channel should get the daily digest, grab that channel's ID → `SLACK_GENERAL_CHANNEL_ID`.
8. Set `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`/`SLACK_GENERAL_CHANNEL_ID` in the Render dashboard (the `sync: false` entries in `render.yaml` are placeholders — Render won't fill them in for you).
9. After the next deploy, copy the Render-generated `SLACK_SCHEDULER_SECRET` into a GitHub Actions repo secret of the same name.
10. If you change scopes later, **reinstall the app to the workspace** (OAuth & Permissions → Reinstall) — Slack requires this for scope/feature changes to take effect, and skipping it is the most common cause of things silently not working.

## Troubleshooting

- **"/enter-week is not a valid command"** — the app hasn't been reinstalled since the slash command was added. Reinstall (OAuth & Permissions → Reinstall to Workspace).
- **"App did not respond"** — either a cold Render instance (free tier sleeps after inactivity; try again a few seconds later), or a genuine bug in a handler that's taking >3s to ack. Check Render logs for what actually happened.
- **A message literally says `{}`** — this was a real bug (fixed): Slack's slash-command/block-actions contract needs a truly empty HTTP response to ack silently; returning `{}` as a JSON body could get rendered as a literal `{}`. If it recurs, check for a route returning `JSONResponse({})` where it should return `Response(status_code=200)`.
- **401 on every Slack request** — `SLACK_SIGNING_SECRET` is missing, wrong, or has a stray whitespace/newline from copy-paste. `backend/slack_client.py`'s `verify_signature` logs the configured secret's length and whether it has surrounding whitespace (never the secret itself) on a mismatch — check Render's logs for that line.
- **Some people never get reminded / their name shows in `unmatched_roster_names`** — their `team-members.json` entry doesn't match their Slack profile's Full Name closely enough (exact match after trimming whitespace and lowercasing, no fuzzy matching). Fix either side to match the other.
- **A PR preview can't reach the API ("failed to fetch")** — the CORS regex (see Credentials section above) doesn't match the current Vercel project name. Check `backend/app.py`'s `CORS_ORIGIN_REGEX` default against the actual preview URL shown in the PR's Vercel comment.
