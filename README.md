<div align="center">
  <img src="‎.github/assets/mark2_orbit_ring.jpg" width="120" height="120" alt="hydra-maintainer logo" />

  # hydra-maintainer

  **A self-hostable, open-source automation platform for open-source maintainers.**

  PR health scoring · reviewer recommendation · contributor progression tracking · optional AI-assisted review · persistent audit trail · REST API

  [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
  [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
  [![Tests](https://img.shields.io/badge/tests-139%20passing-brightgreen)](tests/)
  [![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688)](https://fastapi.tiangolo.com/)
  [![GitHub Marketplace](https://img.shields.io/badge/GitHub-Marketplace-24292e?logo=github)](https://github.com/marketplace/hydra-maintainer)

  [Live dashboard](http://3.93.203.14:8000/) · [Project site](https://website-iota-liard-16.vercel.app/) · [Marketplace listing](https://github.com/marketplace/hydra-maintainer)
</div>

---

No SaaS lock-in. No vendor hosting your repository data. Your bot, your database, your rules.

## The problem

Every healthy open-source project eventually runs into the same bottleneck: a small number of unpaid maintainers doing manual, repetitive triage — checking DCO sign-off, screening first-time contributors, chasing stale issues, deciding who's ready to become a committer, recommending a reviewer who actually knows the file being changed. None of this is written down anywhere; it lives in a maintainer's head until they burn out and it's lost.

The tools that exist to help fall into two camps:

- **Commercial SaaS bots** (Mergify, CodeRabbit, Greptile, etc.) — capable, but closed-source. Your repository data, review history, and contributor metrics live on someone else's servers, behind a subscription that can change price or disappear.
- **Single-purpose OSS bots** (probot apps, stale-bot, welcome-bot) — free and self-hostable, but each solves one narrow problem with no shared data model, no persistent history, and nothing that helps a project reason about its own contributor pipeline over time.

Nobody has a self-hosted, open-source system that treats maintainer-ops as a whole — with a real database, an audit trail, and a data model for how contributors *grow* inside a project, not just whether a single PR passes a gate.

## What it does

`hydra-maintainer` is a GitHub App (FastAPI + SQLAlchemy async + Postgres/SQLite) that runs entirely on infrastructure you control. It was built for and tested against the [Hiero](https://hiero.org) (Linux Foundation Decentralized Trust) ecosystem, and generalizes to any GitHub-hosted open-source project via a single per-repo YAML config file.

| Capability | What it does |
|---|---|
| **Onboarding** | Detects first-time contributors, posts a welcome checklist, validates account age / public-repo count before `/assign`, round-robins mentor assignment |
| **PR quality gates** | DCO sign-off, GPG signature, test-file presence, linked-issue requirement, branch naming pattern, max file count — auto-labelled `quality: ✅` / `❌`, with a posted Quality Gate Report |
| **PR health scoring** | Every PR scored 0–100 across 6 configurable, weighted signals (tests, linked issue, description, DCO, approvals, diff size), posted as a live-updating scorecard |
| **Reviewer assignment** | Automatically requests reviewers on newly opened, non-draft PRs using a repo-local `.github/reviewers.yml` availability file and configurable `round-robin` or `random` selection |
| **Reviewer recommendation** | Suggests reviewers based on recent file-history overlap, logged with a reason and confidence score; posts a comment-only recommendation rather than requesting a review |
| **AI-assisted review** *(optional)* | Structured review (summary, verdict, line comments, severity) via the Anthropic SDK — disabled by default, never required |
| **Contributor progression** | Tracks merged PRs, reviews given, months active; computes eligibility for `junior-committer → committer → maintainer`; celebrates merge milestones (1st, 5th, 10th, 25th, 50th) |
| **Issue management** | Daily stale scan (cron), auto-unassign on inactivity, label-based escalation to specific teams |
| **Live dashboard** | Real-time metrics, score-distribution and signal pass-rate charts, audit log, contributor progression table — auto-refreshes every 30s |
| **REST API** (`/api/v1`) | Query every stored record — audit log, PR health, contributor snapshots, stale-action history, aggregate repo stats |
| **Persistent audit trail** | Every bot action (label, comment, assign, close) is written to Postgres/SQLite with a reason — nothing is silent, everything is queryable later |
| **Security** | HMAC-SHA256 webhook signature verification with constant-time comparison; the bot is completely silent in any repo without an explicit config file |

Ships with **139 unit + integration tests** (`pytest-asyncio`, `httpx`, `respx`, in-memory SQLite) covering every workflow module and the full REST API surface.

## Seen in the wild

`hydra-maintainer` runs live against real pull requests. A few things it posts automatically:

- A **PR health scorecard** breaking down exactly which signals passed and which didn't, with a plain-language nudge on what to fix.
- A **Quality Gate Report** blocking review requests until required gates (like a linked closing issue) are satisfied.
- **Reviewer suggestions** based on who last touched the changed files, posted as a lightweight comment rather than a forced review request.
- An **AI Code Review** pass — a scored, verdict-labelled summary — clearly marked as automated, always followed by human review.
- **`/assign` handling** with an instant confirmation comment and eligibility check.

## Why it's different

| | hydra-maintainer | Mergify | CodeRabbit / Greptile | probot / stale-bot / welcome-bot |
|---|---|---|---|---|
| License / hosting | **MIT, self-hosted** | Closed-source SaaS | Closed-source SaaS | Open source, but stateless |
| Where your data lives | Your own Postgres/SQLite | Vendor's servers | Vendor's servers | N/A (no persistence) |
| Contributor role progression | ✅ built-in model | ❌ | ❌ | ❌ |
| PR health scoring | ✅ weighted & configurable | Partial (merge rules only) | ❌ | ❌ |
| AI review | ✅ optional, off by default | ❌ | ✅ always-on, vendor-locked | ❌ |
| Audit trail + REST API | ✅ | Limited to their UI | ❌ | ❌ |
| Cost | Free, run anywhere | Paid tiers | Paid tiers | Free |

## Architecture

```
app/
├── main.py                     # FastAPI app + lifespan
├── config/
│   ├── schema.py                # Pydantic v2 config schema + validation
│   └── loader.py                # YAML loader with TTL cache
├── db/
│   ├── database.py              # Async SQLAlchemy engine
│   └── models.py                # AuditLog, PRHealthScore, ContributorSnapshot, StaleActionLog, ReviewerRecommendation
├── github/
│   ├── client.py                # Async GitHub App HTTP client
│   └── webhooks.py               # HMAC-verified webhook router
├── workflows/
│   ├── onboarding.py             # First-time contributor flows
│   ├── pullrequest.py            # Quality gates + AI review + reviewer rec.
│   ├── prhealth.py               # PR health scoring
│   ├── progression.py            # Role progression + issue recommendations
│   └── issuemanagement.py        # Stale scan + escalation
├── ai/
│   └── reviewer.py                # Anthropic SDK integration (pluggable)
├── scheduler/
│   └── jobs.py                    # APScheduler cron jobs
├── api/
│   └── routes.py                  # REST API endpoints
└── utils/
    ├── audit.py, logger.py, settings.py
dashboard/
└── templates/dashboard.html       # Live metrics dashboard
tests/
├── unit/                          # workflow + schema tests
└── integration/                   # httpx + in-memory SQLite API tests
```

## Slash commands

| Command | Who | Description |
|---|---|---|
| `/assign` | Anyone | Self-assign (eligibility checked) |
| `/unassign` | Anyone | Remove yourself from an issue |
| `/check-eligibility` | Contributors | View role progression breakdown |
| `/label <name>` | Committers+ | Add a label (role-gated) |
| `/help` | Anyone | Show all commands |

## REST API

Base path: `/api/v1`

| Endpoint | Description |
|---|---|
| `GET /health` | Service health check |
| `GET /audit` | Audit log (filter by owner, repo, action, login, since) |
| `GET /pr-health` | PR health score records (filter by score range, author) |
| `GET /pr-health/stats` | Aggregate stats for a repo |
| `GET /contributors` | Contributor snapshots (filter by role eligibility) |
| `GET /repos/stats` | Full repo summary |
| `GET /stale-log` | Stale action history |

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/mohityadav8/hiero-bot-py
cd hiero-bot-py
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET

# 3. Run
uvicorn app.main:app --reload

# 4. Open dashboard
open http://localhost:8000
```

### Docker

```bash
docker build -t hydra-maintainer .
docker run -p 8000:8000 --env-file .env hydra-maintainer
```

### Tests

```bash
python -m pytest tests/unit/ tests/integration/ -q          # 139 tests
python -m pytest tests/ --cov=app --cov-report=term-missing  # with coverage
```

### Security audit

Before pushing, check dependencies for known vulnerabilities:

```bash
pip install pip-audit
pip-audit -r requirements.txt -f json -o audit-report.json
python .github/scripts/check_audit_severity.py audit-report.json
```

CI runs the same check and fails the build on High/Critical CVSS findings. `pip-audit` has no built-in severity flag — filtering is done by the script above against each finding's CVSS score.

## Configuration

Add `.github/hiero-bot.yml` to any repo where the app is installed. Full reference: [`templates/hiero-bot.yml`](templates/hiero-bot.yml). The bot is completely silent if no config file exists — nothing runs by accident.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | ✅ | From GitHub App settings |
| `GITHUB_PRIVATE_KEY` | ✅ | RSA private key (use `\n` for newlines) |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Webhook secret set in App settings |
| `ANTHROPIC_API_KEY` | AI review only | Anthropic API key |
| `DATABASE_URL` | ❌ | Default: `sqlite+aiosqlite:///./hiero_bot.db` |
| `PORT` | ❌ | Default: `8000` |
| `LOG_LEVEL` | ❌ | debug/info/warn/error |
| `ENVIRONMENT` | ❌ | development / production |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | ❌ | Reserved for dashboard access control — defined but not yet enforced; tracked in the roadmap below |

## Ecosystem & standing

Originally built and validated against repositories in the Hiero ecosystem under Linux Foundation Decentralized Trust. The author is an active contributor to hiero-sdk-python and other Hiero-adjacent projects, and an ECWoC'26 Top Contributor. The config format is deliberately generic (any owner/repo, any team names) so the same bot can be installed on non-Hiero, GitHub-hosted FOSS projects without modification.

## Roadmap

This project is under active, ongoing development. Planned work includes multi-forge support (GitLab/Gitea/Forgejo), pluggable local/open-weight AI review backends, portable contributor-reputation credentials, and a security-hardened 1.0 release.

## Funding & support

`hydra-maintainer` is currently a solo, unfunded open-source effort, built and maintained alongside full-time study. It is actively seeking grant support to fund the roadmap above as sustained, full-time work rather than nights-and-weekends progress. If you're a maintainer, funder, or organization interested in supporting or piloting this project, please open an issue or reach out via the links below.

## Contributing

Issues and PRs are welcome. Please sign your commits (DCO) and include tests for new workflow logic — see `tests/unit/` for the existing patterns before adding new ones.

## Author

**Mohit Yadav** ([@mohityadav8](https://github.com/mohityadav8)) — CS undergraduate (BE-CSE, Chandigarh University, 2024–2028), open-source contributor across NVIDIA/aicr, SHAP, pgmpy, hiero-sdk-python, and OWASP Nest. Portfolio: [mohityadav8.github.io](https://mohityadav8.github.io)

## License

MIT License — see [LICENSE](LICENSE).
