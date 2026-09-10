# Security Policy

## Supported Versions

Security fixes are currently provided for the latest code on the default branch.

| Version | Supported |
|---|---|
| Default branch | ✅ |
| Older releases | ❌ |

## Reporting a Vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Use GitHub's private security advisory mechanism for this repository to report vulnerabilities confidentially. Include enough information for us to reproduce and assess the issue, such as:

- A description of the vulnerability and its security impact
- Affected component, endpoint, or configuration
- Steps to reproduce or a minimal proof of concept
- Relevant logs, requests, or configuration details
- Any suggested mitigation, if available

We will acknowledge a valid vulnerability report within **3 business days** and provide an initial assessment or mitigation plan within **7 business days**.

Please avoid publicly disclosing the vulnerability until a fix or mitigation is available and coordinated disclosure has been agreed.

## Threat Model

Hiero-bot-py runs as a self-hosted GitHub App. The primary trust boundaries are:

- **GitHub webhook sender:** Incoming webhook requests are untrusted until their HMAC signature and replay protections have been validated.
- **Dashboard/API operator:** The dashboard and REST API are administrative surfaces and must not be exposed publicly without authentication configured.
- **AI backend:** Optional AI providers receive data required for the configured review operation. The AI backend is treated as an external service and is not a trusted part of the application runtime.

### In scope

Security reports are in scope when they can affect:

- Authentication or authorization
- GitHub webhook signature verification
- Webhook replay protection
- Webhook secret rotation
- Dashboard or REST API exposure
- Rate-limit bypasses
- Cross-repository or cross-tenant data access
- Exposure of secrets, tokens, credentials, or other sensitive application data
- Unsafe handling of untrusted GitHub webhook or API input
- Security-impacting behavior in optional AI integrations

### Out of scope

The following are generally out of scope unless they demonstrate a concrete security impact:

- Vulnerabilities in GitHub, GitHub Marketplace, or other third-party services
- Vulnerabilities in third-party dependencies that do not affect this application's security
- Denial-of-service caused solely by resource exhaustion without a practical application-level exploit
- Issues requiring compromised maintainer credentials or access to the production host
- Social engineering or phishing attacks against maintainers or users
- Bugs that do not have a security impact

## Security Controls

The current security controls include:

- **HMAC webhook verification:** GitHub webhook signatures are verified using HMAC-SHA256 with constant-time comparison.
- **Replay protection:** GitHub delivery IDs are tracked for a limited TTL so previously processed deliveries are rejected.
- **Dual-secret rotation:** A current webhook secret and optional previous secret can be accepted simultaneously during zero-downtime rotation.
- **Dashboard authentication:** Dashboard/API access can be protected with HTTP Basic Authentication using constant-time credential comparison.
- **Rate limiting:** Webhook and API requests have separate configurable rate limits.
