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
