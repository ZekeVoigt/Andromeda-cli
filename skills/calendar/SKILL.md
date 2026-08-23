---
name: calendar
description: "Google Calendar operations via gcalcli: schedule, search, and availability."
metadata:
  andromeda:
    emoji: "📅"
    requires:
      bins: ["gcalcli"]
    install:
      - id: "uv"
        kind: "uv"
        package: "gcalcli"
        bins: ["gcalcli"]
---

# Calendar Skill
Use `gcalcli` for calendar actions.

- Agenda: `gcalcli agenda`
- Search: `gcalcli search "<query>"`
- Add event: `gcalcli add --title "<title>" --when "<time>" --duration <mins>`
