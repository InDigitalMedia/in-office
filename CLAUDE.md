# Project shape

"In Office" — tracks where team members are working each day (Neal Street, Client Office, WFH, Working From Abroad, Holiday, Other), filled in from the web app or Slack.

- **Backend**: `backend/` — FastAPI + SQLModel, SQLite locally / Postgres in production. Wire contract is hand-written in `backend/schemas.py`, not shared/generated.
- **Frontend**: `frontend/` — TypeScript + Vite. Wire contract is hand-written in `frontend/src/types.ts` and `frontend/src/api.ts`, mirroring the backend independently.
- **Deployment**: five separate hand-duplicated run modes (Render prod, Docker, docker-compose, plist, `scripts/start.sh`) — no single source of truth for ports/env vars/paths.
- **Docs**: scattered across root `CHANGELOG.md`, `docs/CHANGELOG.md`, `docs/HOW_TO_USE.md`, `docs/QUICK_START.md`, `docs/RUNNING_GUIDE.md`, `HANDOVER.md`/`HANDOVER.html` — no single source of truth.

## Subagents that guard known risk areas

Use these proactively (see each agent's file in `.claude/agents/` for full detail) — they encode context that isn't otherwise written down:

- `api-contract-checker` — catches drift between `backend/schemas.py`/`app.py` and `frontend/src/types.ts`/`api.ts` when either side's request/response shape changes.
- `db-safety-reviewer` — checks entry upsert/delete logic, DB connection setup, the `Entry` schema, and migrations for data-loss risk before commit.
- `deploy-config-auditor` — checks cross-config consistency (ports, env vars, paths) across the five run modes whenever deploy/runtime config changes.
- `docs-syncer` — checks whether the changelogs/guides/`HANDOVER.md` need updating after a code or deployment change lands.
- `ui-ux-designer` — makes actual CSS/JSX changes for styling/layout work, never trading functionality for looks.

# Project conventions

- `frontend/public/clients.json` (`clients` array) and `frontend/public/team-members.json` (`teamMembers` array) must always be kept in alphabetical order (case-insensitive). When adding, removing, or renaming an entry in either file, re-sort the whole array rather than just inserting at the end.
