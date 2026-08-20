# In Office

A web app for tracking where team members are working each day — Neal Street, Client Office, WFH, Working From Abroad, Holiday, or Other — filled in from the web app or directly from Slack.

## Features

- 📅 **Week-view dashboard** — everyone's location grouped by day and location, with headcounts
- ✂️ **Split days** — a day can be split into morning/afternoon, each with its own location
- 💬 **Slack integration** — `/enter-week` slash command, daily reminder DMs, and a "who's in the office" channel digest, all without leaving Slack (see below)
- 🔒 **Admin tab** — password-gated Team Location Dashboard: filter by date range/team member/location, KPI cards (in-office/remote/away %), charts (by location, by day, weekly trend), a per-person breakdown table with CSV export, and a per-user attendance heatmap
- 👥 **Client and team-member rosters** (`frontend/public/clients.json`, `frontend/public/team-members.json`) — kept alphabetically sorted, see `CLAUDE.md`
- 📱 Mobile responsive, light/dark theme

## Quick Start

### Local development (no Docker)

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8001

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Visit http://localhost:5173 — backend API at http://localhost:8001 (docs at `/docs`).

### Docker

```bash
docker-compose up --build
```

- Frontend: http://localhost:5173
- Backend API: http://localhost:8001

### PM2 (keep both running continuously)

```bash
pm2 start ecosystem.config.js
pm2 status
pm2 logs
pm2 stop all
```

See [docs/RUNNING_GUIDE.md](docs/RUNNING_GUIDE.md) for the full PM2 workflow, including auto-start on boot.

## Environment Configuration

- **Frontend**: copy `frontend/env.example` to `.env` and adjust `VITE_API_BASE` if needed.
- **Backend**: no environment variables are required to run locally. Everything below is optional and only needed to enable specific production features:

| Variable | Enables |
|---|---|
| `ADMIN_SECRET` | Admin-only API endpoints |
| `ADMIN_TAB_PASSWORD` | The web app's password-gated Admin tab |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_GENERAL_CHANNEL_ID`, `SLACK_SCHEDULER_SECRET` | The Slack integration — see [docs/SLACK_INTEGRATION.md](docs/SLACK_INTEGRATION.md) |
| `DATABASE_URL` | PostgreSQL in production (falls back to local SQLite when unset; refuses to start on SQLite in production) |
| `ROSTER_URL`, `CLIENTS_URL` | Override where the backend fetches `team-members.json`/`clients.json` from (defaults to the live frontend deployment) |

## Slack Integration

Fill in your week and see who's at Neal Street without leaving Slack:

- **`/enter-week`** slash command opens a modal to fill your week (with per-day split support)
- **Daily reminder DMs** (morning + an afternoon "last call" nudge) to anyone who hasn't filled in yet
- **Daily office digest** to a channel — who's in today, and a separate one for tomorrow/next week
- **Week summary DM** after saving, showing who else is in each day that week

Full setup, credentials, ownership handover, and troubleshooting: [docs/SLACK_INTEGRATION.md](docs/SLACK_INTEGRATION.md).

## Documentation

- [docs/HOW_TO_USE.md](docs/HOW_TO_USE.md) — using the app
- [docs/RUNNING_GUIDE.md](docs/RUNNING_GUIDE.md) — running it continuously with PM2
- [docs/deployment/](docs/deployment/) — deployment guides
- [docs/SLACK_INTEGRATION.md](docs/SLACK_INTEGRATION.md) — Slack setup, credentials, and ownership
- [CHANGELOG.md](CHANGELOG.md) — notable changes
- [HANDOVER.md](HANDOVER.md) — account/ownership handover checklist

## Tech Stack

- **Backend**: Python 3.11, FastAPI, SQLModel, PostgreSQL/SQLite
- **Frontend**: React, TypeScript, Vite
- **Deployment**: Vercel (frontend) + Render (backend)
- **Scheduling**: cron-job.org triggers the Slack digest/reminder endpoints

## Development Commands

### Backend

```bash
ruff check . && black .
pytest -q
```

### Frontend

```bash
npm run lint && npm run format
```
