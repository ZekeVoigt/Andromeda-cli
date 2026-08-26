---
name: email
description: "Email operations via himalaya CLI: list, read, send, and search mail."
metadata:
  andromeda:
    emoji: "📧"
    requires:
      bins: ["himalaya"]
    install:
      - id: "brew"
        kind: "brew"
        formula: "himalaya"
        bins: ["himalaya"]
---

# Email Skill
Use `himalaya` for mailbox workflows.

- List envelopes: `himalaya envelope list`
- Read message: `himalaya message read <id>`
- Send message: `himalaya message send`
- Search: `himalaya envelope list --query "<query>"`
