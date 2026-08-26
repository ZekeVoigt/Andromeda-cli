---
name: weather
description: "Weather and forecasts using wttr.in via curl."
metadata:
  andromeda:
    emoji: "🌤️"
    requires:
      bins: ["curl"]
---

# Weather Skill
Use `curl` with wttr.in.

- Quick weather: `curl "wttr.in/New+York?format=3"`
- Multi-day forecast: `curl "wttr.in/New+York?format=j1"`
