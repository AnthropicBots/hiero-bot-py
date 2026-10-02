# ADR 001: Premium Subscription Billing Unit & Entitlements

## Status
Accepted

## Context
Hiero Maintainer Bot provides GitHub maintainer automation, health scoring, and team analytics. To support a sustainable commercial model while preserving open-source core features, we need a billing structure for premium features (e.g., advanced analytics, custom SLAs, AI-assisted review triggers).

## Decision
We adopt a **Per-Organization (Account-Level) Flat Subscription Model**:
1. **Billing Unit**: Subscriptions attach to a GitHub `Account` (Organization or User Installation ID).
2. **Tiers**:
   - `free`: Standard webhook automations, basic stale management, and core dashboard metrics.
   - `premium`: Unlimited PR health analytics history, priority webhook processing, custom role progression policies, and advanced AI reviewer recommendations.
3. **Provider**: Stripe Subscriptions via Stripe Checkout and Webhooks.

## Consequences
- Single subscription covers all repositories under an organization installation.
- Simple, transparent pricing without complex per-seat counting.
- Enforced at API layer via HTTP `402 Payment Required` responses for free accounts requesting premium endpoints.

## Premium entitlement

Accepted with issue #130.

Premium means an account whose `plan_tier` is `premium`.

- Stripe `customer.subscription.created` and `customer.subscription.updated` set that tier from `data.object.status`. `active` and `trialing` map to `premium`. Every other status (`incomplete`, `incomplete_expired`, `past_due`, `unpaid`, `canceled`, `paused`, or a missing status) maps to `free` immediately.
- `checkout.session.completed` still upgrades the account. That event means Checkout finished and does not carry a subscription status. A later subscription event corrects the tier.
- `customer.subscription.deleted` sets `free`.
- The features this covers are AI code review, reviewer recommendations, and automated reviewer assignment. Quality gates, PR health scoring, onboarding, progression, and issue management stay on the free tier.
- Webhook handlers skip those three steps when the installation has no account row or the row is not premium. They do not answer GitHub with HTTP 402, because a non-2xx delivery is retried.
- `require_premium_account` remains the HTTP 402 check for request handlers and uses the same `is_premium_account` predicate.
