# Privacy Policy — Hydra Maintainer

Hydra Maintainer is a self-hosted GitHub App. This means:

- All data it collects (audit logs, PR health scores, contributor snapshots) is stored in a database that you control (SQLite or Postgres), on infrastructure you host.
- The developer of this app (Mohit Yadav / AnthropicBots) does not have access to, collect, or store any data from your repositories, issues, or pull requests.
- The app only communicates with the GitHub API (to read repository/PR/issue data and post comments/labels as configured) and, optionally, the Anthropic API if you enable AI-assisted review with your own API key.
- No data is shared with any third party beyond what is required for the app to function (GitHub API, and optionally Anthropic API if enabled).
- Webhook payloads are verified using HMAC-SHA256 signatures to ensure authenticity.

For questions, open an issue at https://github.com/AnthropicBots/hiero-bot-py/issues.
