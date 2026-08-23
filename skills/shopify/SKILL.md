---
name: shopify
description: "Shopify operations via shopify CLI and Admin API workflows."
metadata:
  andromeda:
    emoji: "🛒"
    requires:
      bins: ["shopify"]
    install:
      - id: "node"
        kind: "node"
        package: "@shopify/cli"
        bins: ["shopify"]
---

# Shopify Skill
Use `shopify` CLI for dev and deployment workflows.

- Theme dev: `shopify theme dev`
- Theme pull/push: `shopify theme pull`, `shopify theme push`
- Use Admin API calls when CLI output is insufficient.
