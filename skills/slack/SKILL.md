---
name: slack
description: "Slack actions via built-in Slack tool for messaging and channel workflows."
metadata:
  andromeda:
    emoji: "💬"
    always: true
    requires:
      config: ["channels.slack"]
---

# Slack Skill
Slack is handled through the built-in tool surface.

Use actions like:
- `sendMessage`
- `readMessages`
- `editMessage`
- `deleteMessage`
- `react`
- `pinMessage` / `unpinMessage`
- `memberInfo`
