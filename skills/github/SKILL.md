---
name: github
description: "GitHub operations via gh CLI: issues, PRs, checks, runs, and API queries."
metadata:
  andromeda:
    emoji: "🐙"
    primaryEnv: "GITHUB_TOKEN"
    requires:
      bins: ["gh"]
    install:
      - id: "brew"
        kind: "brew"
        formula: "gh"
        bins: ["gh"]
        label: "Install GitHub CLI (brew)"
---

# GitHub Skill
Use `gh` for repository and workflow operations.

- List PRs: `gh pr list --json number,title,author,state`
- Review checks: `gh run list --limit 10`
- View issue details: `gh issue view <number>`
- Use structured output with `--json` and `--jq` when possible.
