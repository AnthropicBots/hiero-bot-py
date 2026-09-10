# Privacy Policy — Hiero Maintainer Bot

Hiero Maintainer Bot operates in two deployment modes: **Self-Hosted Mode** and **Hosted Multi-Tenant Mode**.

### 1. Self-Hosted Mode
- All data (audit logs, PR health scores, contributor snapshots) is stored in a database that you control (SQLite or PostgreSQL) on your own infrastructure.
- Maintainers do not have access to, collect, or store any data from your repositories or pull requests.
- The app only communicates with the GitHub API (and optionally Anthropic/OpenAI APIs if AI reviews are enabled with your API key).

### 2. Hosted / Multi-Tenant Mode
- When using the hosted SaaS platform, users sign in via **GitHub OAuth** (`read:org` scope).
- Data collected includes: GitHub user ID, username, email, avatar URL, organization memberships, and installation account scopes.
- OAuth user access tokens are encrypted at rest using Fernet symmetric encryption before being saved to the database.
- Multi-tenant data isolation ensures users only see repository and audit data for organization accounts where they have verified access.
- Payment processing is handled securely via Stripe. No credit card or financial details are stored on Hiero servers.

Webhook payloads are verified using HMAC-SHA256 signatures to ensure authenticity across all modes.

For questions or data deletion requests, open an issue at https://github.com/AnthropicBots/hiero-bot-py/issues.

