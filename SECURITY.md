# Security Policy

`hiero-bot-py` is a self-hosted GitHub App. It holds an installation token that
can comment, label, assign, and close issues in every repository the app is
installed on, and it stores a history of contributor activity in a database you
run. Both of those are worth protecting, so please read this before deploying it
somewhere it matters.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately through GitHub's [security advisory
form](https://github.com/AnthropicBots/hiero-bot-py/security/advisories/new).
If that is unavailable to you, open a regular issue containing only the words
"security report — please contact me" and a maintainer will arrange a private
channel. Do not include details in that issue.

Please include, as far as you can:

- what the vulnerability lets an attacker do,
- the version or commit you tested,
- reproduction steps or a proof of concept,
- whether you believe it is being exploited.

What to expect:

| Stage | Target |
|---|---|
| Acknowledgement of your report | 3 working days |
| Initial assessment and severity | 10 working days |
| Fix or documented mitigation for High/Critical | 30 days |
| Public advisory | After a fix ships, or 90 days, whichever is first |

This is a small, unfunded project maintained alongside other work. These are
honest targets, not a contractual SLA. If a deadline slips you will be told why
rather than left waiting.

Reporters are credited in the advisory unless they ask not to be. There is no
bug bounty.

## Supported versions

Security fixes land on `main` and in the most recent tagged release. Older tags
are not patched — self-hosters are expected to track `main` or the latest tag.

## Scope

**In scope**

- Webhook signature bypass, replay, or forgery.
- Anything that makes the bot act on a repository with no `.github/hiero-bot.yml`.
- Privilege confusion in slash commands (a non-committer causing a committer-only action).
- Leaking the installation token, webhook secret, private key, or AI API key into logs, comments, or API responses.
- SQL injection, SSRF, path traversal, or unsafe deserialization anywhere in the request path.
- Denial of service reachable by an unauthenticated caller.
- Anything that lets a repository's config file cause execution outside that repository's context.

**Out of scope**

- Findings that require the attacker to already hold the app's private key, webhook secret, or database credentials.
- Missing hardening on a deployment that ignores the checklist below — for example an internet-exposed dashboard with `DASHBOARD_PASSWORD` unset.
- Rate limits being per-process rather than cluster-wide. This is documented behaviour; see `app/utils/ratelimit.py`.
- Vulnerabilities in GitHub itself, or in a dependency with no exploitable path through this code. Report those upstream.
- Output quality of the optional AI review. A wrong review is a bug, not a vulnerability.

## Threat model

The bot's exposed surfaces are the webhook endpoint, the REST API, the
dashboard, and the per-repository config file. Each is treated as attacker
controlled.

### Webhook deliveries

Anyone can POST to `/webhook`. The endpoint therefore assumes the body is
hostile until proven otherwise:

- **Signature.** `X-Hub-Signature-256` is recomputed with HMAC-SHA256 over the
  raw body and compared with `hmac.compare_digest`, so comparison time does not
  leak how much of the digest matched. A request with no signature header is
  rejected outright.
- **Secret rotation.** Two secrets can be live at once
  (`GITHUB_WEBHOOK_SECRET` and `GITHUB_WEBHOOK_SECRET_OLD`), so a secret can be
  rotated in GitHub without dropping in-flight deliveries. Remove the old value
  once rotation is complete — a retired secret that stays configured is an
  extra key that still works.
- **Replay.** `X-GitHub-Delivery` IDs are remembered for 10 minutes, so a
  captured delivery cannot be resubmitted to repeat an action.
- **Clock skew.** Deliveries whose `Date` header is further than
  `WEBHOOK_MAX_SKEW_SECONDS` from now are refused. This is defence in depth
  behind the signature and replay checks, not a control in its own right.
- **Rate limiting.** A per-caller token bucket bounds how much work an
  unauthenticated client can force the process to do.

### Repository configuration

`.github/hiero-bot.yml` is written by whoever can push to the repository, which
is not necessarily someone you trust.

- The bot is **silent by default**: with no config file, nothing runs. This is
  the single most important property in the design — installing the app on an
  organisation does not opt every repository into automated comments.
- YAML is parsed with `yaml.safe_load`, so no object construction or arbitrary
  tags. Documents are size-limited and must have a mapping at the top level.
- Every value is validated against a pydantic schema before use. A config that
  fails validation disables the bot for that repository and is logged; it never
  half-applies.
- Config values name teams, labels, and file paths within the same repository.
  They are never used to build a request to another host.

### Tokens and secrets

- The GitHub App private key is read from the environment and used only to mint
  short-lived installation tokens. Tokens are cached in memory with their real
  expiry and never written to disk or to the database.
- Audit records store *what the bot did and why*, never credentials.
- Log lines are written with `%s` parameters rather than interpolated strings,
  and no code path logs a token, secret, or API key. If you find one, that is a
  reportable vulnerability.
- The optional AI review sends changed file contents and diffs to the
  configured model provider. It is **off by default**. Turning it on means
  accepting that your source is transmitted to that provider — do not enable it
  on a private repository without checking that provider's data policy.

### Dashboard and REST API

- Both sit behind HTTP Basic auth when `DASHBOARD_USERNAME` and
  `DASHBOARD_PASSWORD` are set, compared with `hmac.compare_digest`.
- **If those are unset, the dashboard and the whole API are unauthenticated.**
  Startup logs a warning in production, but nothing refuses to boot. Anyone who
  reaches the port can read your contributor metrics and audit history.
- The API is read-only. It exposes no endpoint that mutates state or reaches
  GitHub.
- All queries go through SQLAlchemy with bound parameters; no SQL is built by
  string concatenation.

## Hardening checklist for self-hosters

- [ ] Set `ENVIRONMENT=production`.
- [ ] Set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`, or keep the port off the public internet entirely.
- [ ] Terminate TLS in front of the app. Basic auth over plain HTTP hands over the password.
- [ ] Set `GITHUB_WEBHOOK_SECRET` to a high-entropy value (`openssl rand -hex 32`) and make it match the App settings.
- [ ] Set `TRUSTED_PROXY_HOPS` to the number of proxies actually in front of the app — leave it at `0` if there are none.
- [ ] Give the GitHub App the narrowest permission set your enabled workflows need.
- [ ] Run the container as a non-root user and mount the filesystem read-only apart from the database path.
- [ ] Put the database somewhere backed up; the audit trail is the record of everything the bot has done.
- [ ] Keep `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` unset unless AI review is deliberately enabled.
- [ ] Rotate the webhook secret and the App private key on a schedule you actually keep.

## Dependency and supply chain policy

- Runtime dependencies are pinned in `requirements.txt`.
- CI runs `pip-audit` on every push and fails the build on any finding with a
  High or Critical CVSS score. `pip-audit` has no severity flag of its own, so
  `.github/scripts/check_audit_severity.py` does the filtering against each
  finding's score.
- To reproduce locally:

  ```bash
  pip install pip-audit
  pip-audit -r requirements.txt -f json -o audit-report.json
  python .github/scripts/check_audit_severity.py audit-report.json
  ```

- Dependency updates that fix a known vulnerability are merged ahead of feature
  work.

## Data handling

See [PRIVACY.md](PRIVACY.md) for what is stored and for how long. In short: the
bot stores public GitHub activity metadata in a database you control, and no
data leaves your infrastructure except calls to the GitHub API — plus, if you
explicitly enable AI review, calls to your chosen model provider.
