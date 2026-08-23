---
name: stripe
description: "Stripe operations via stripe CLI: customers, payments, invoices, subscriptions."
metadata:
  andromeda:
    emoji: "💳"
    primaryEnv: "STRIPE_SECRET_KEY"
    requires:
      bins: ["stripe"]
    install:
      - id: "brew"
        kind: "brew"
        formula: "stripe/stripe-cli/stripe"
        bins: ["stripe"]
---

# Stripe Skill
Use `stripe` CLI for operational tasks and diagnostics.

- List customers: `stripe customers list --limit 20`
- List payment intents: `stripe payment_intents list --limit 20`
- Inspect subscriptions: `stripe subscriptions list --limit 20`
- Use `stripe listen` for webhook troubleshooting.
