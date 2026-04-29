# 🤖 Hiero Maintainer Bot v2 (Python)

> A production-ready FastAPI GitHub App that automates maintainer workflows across Hiero repositories — with a live dashboard, persistent audit trail, and REST API.

[![CI](https://github.com/hiero/hiero-maintainer-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/hiero/hiero-maintainer-bot/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

---

## What's New in v2 (Python)

| Feature | Description |
|---------|-------------|
| **PR Health Scoring** | Automated 0–100 score for every PR across 6 weighted signals |
| **Reviewer Recommendation** | Suggests reviewers based on recent file-history overlap |
| **Live Dashboard** | Real-time metrics, charts, audit log, progression table |
| **REST API** | Full `/api/v1` query API for all stored data |
| **Persistent Storage** | SQLite (dev) / PostgreSQL (prod) via SQLAlchemy async |
| **Scheduled Jobs** | APScheduler cron for daily stale scans and cache flushes |
| **`/unassign` + `/label` commands** | New slash commands with role-based access |

---

## Workflows

### 🤝 Onboarding
- Detects first-time contributors and posts a welcome message with checklist
- Validates account age and public repo count before allowing `/assign`
- Round-robin mentor assignment from the mentors GitHub team

### 🔍 Pull Request Quality Gates
- DCO sign-off status check
- GPG signature verification
- Test file presence detection
- Linked issue (`Closes #N`) requirement
- Branch naming pattern enforcement
- Configurable max file count
- Auto-labels: `quality: ✅ passed` / `quality: ❌ needs work`
- Optional AI review via Anthropic (claude-sonnet-4)
- Reviewer recommendation based on file history

### 📊 PR Health Scoring *(new)*
Scores every PR 0–100 across six weighted signals:

| Signal | Default Weight |
|--------|---------------|
| Has test coverage | 25% |
| Has linked issue | 15% |
| Has description ≥50 chars | 15% |
| DCO signed | 20% |
| Has approvals | 15% |
| Diff size < 400 lines | 10% |

Labels: `health: 💚 healthy` / `health: 🔧 needs work`

### 📈 Progression
- Recommends next issues after each merged PR
- Celebrates milestones (1st, 5th, 10th, 25th, 50th merged PR)
- Announces role eligibility (Junior Committer → Committer → Maintainer)
- `/check-eligibility` command shows full stats breakdown

### 🧹 Issue Management
- Daily stale scan (via APScheduler cron at 02:00 UTC)
- Marks issues stale after N days of inactivity
- Closes stale issues after additional N days
- Auto-unassigns inactive contributors
- Label escalation: notify specific teams when label is applied

---

## Slash Commands

| Command | Who | Description |
|---------|-----|-------------|
| `/assign` | Anyone | Self-assign (eligibility checked) |
| `/unassign` | Anyone | Remove yourself from issue |
| `/check-eligibility` | Contributors | View role progression status |
| `/label <name>` | Committers+ | Add label (role-gated) |
| `/help` | Anyone | Show all commands |

---

## Dashboard

Visit `/` after starting the server to access the live dashboard:

- **Overview stats**: avg PR health, stale counts, contributors welcomed
- **Score distribution chart**: bar chart bucketing PR health scores
- **Signal pass-rates chart**: % of PRs passing each gate
- **PR health table**: per-PR breakdown with signal indicators
- **Audit log**: every bot action with reason and timestamp
- **Progression table**: contributor snapshots and role eligibility

Auto-refreshes every 30 seconds.

---

## REST API

Base path: `/api/v1`

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Service health check |
| `GET /audit` | Query audit log (filter by owner, repo, action, login, since) |
| `GET /pr-health` | PR health score records (filter by score range, author) |
| `GET /pr-health/stats` | Aggregate stats for a repo (avg, min, max) |
| `GET /contributors` | Contributor snapshots (filter by role eligibility) |
| `GET /repos/stats` | Full repo summary |
| `GET /stale-log` | Stale action history |

---

## Project Structure

```
app/
├── main.py                     # FastAPI app + lifespan
├── config/
│   ├── schema.py               # Pydantic v2 config schema
│   └── loader.py               # YAML loader with TTL cache
├── db/
│   ├── database.py             # Async SQLAlchemy engine
│   └── models.py               # AuditLog, PRHealthScore, ContributorSnapshot, ...
├── github/
│   ├── client.py               # Async GitHub App HTTP client
│   └── webhooks.py             # HMAC-verified webhook router
├── workflows/
│   ├── onboarding.py           # First-time contributor flows
│   ├── pullrequest.py          # Quality gates + AI review + reviewer rec.
│   ├── prhealth.py             # PR health scoring (new)
│   ├── progression.py          # Role progression + recommendations
│   └── issuemanagement.py      # Stale + escalation
├── ai/
│   └── reviewer.py             # Anthropic SDK integration
├── scheduler/
│   └── jobs.py                 # APScheduler cron jobs
├── api/
│   └── routes.py               # REST API endpoints
└── utils/
    ├── audit.py                # Persistent audit trail
    ├── logger.py               # Structured logging
    └── settings.py             # Pydantic-settings env config
dashboard/
└── templates/dashboard.html    # Live metrics dashboard
tests/
├── unit/                       # 60 unit tests
└── integration/                # 16 integration tests (httpx + SQLite)
```

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/hiero/hiero-maintainer-bot
cd hiero-maintainer-bot
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET

# 3. Run
uvicorn app.main:app --reload

# 4. Open dashboard
open http://localhost:8000
```

## Deployment (Docker)

```bash
docker build -t hiero-bot .
docker run -p 8000:8000 --env-file .env hiero-bot
```

## Tests

```bash
python -m pytest tests/unit/ tests/integration/ -q          # 76 tests
python -m pytest tests/ --cov=app --cov-report=term-missing  # with coverage
```

---

## Configuration

Add `.github/hiero-bot.yml` to any repo where the app is installed.
Full reference: [`templates/hiero-bot.yml`](templates/hiero-bot.yml)

The bot is **completely silent** if no config file exists.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | ✅ | From GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | RSA private key (use `\n` for newlines) |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Webhook secret set in App settings |
| `ANTHROPIC_API_KEY` | AI review only | Anthropic API key |
| `DATABASE_URL` | ❌ | Default: `sqlite+aiosqlite:///./hiero_bot.db` |
| `PORT` | ❌ | Default: `8000` |
| `LOG_LEVEL` | ❌ | `debug/info/warn/error` |
| `ENVIRONMENT` | ❌ | `development` / `production` |

---

## License

Apache License 2.0
