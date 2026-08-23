---
name: skill-creator
description: "Create, edit, improve, or audit skills. Use when creating a new skill from scratch, when asked to improve, review, or edit an existing SKILL.md file, or when the user describes a workflow that no existing skill handles. Triggers on: 'create a skill', 'make a skill', 'add a skill', 'teach the agent to do X', 'make it remember how to do X', 'improve this skill', 'edit the skill', 'clean up the skill', 'audit the skill'. Also triggers when the user describes a repeated workflow and no existing skill covers it."
metadata:
  andromeda:
    emoji: "\U0001F6E0\uFE0F"
    always: true
user-invocable: true
---

# Skill Creator

A skill for creating new skills and iteratively improving them.

## When to Use This Skill

- User explicitly asks to create, make, or add a skill
- User asks to improve, edit, review, audit, or clean up an existing SKILL.md
- User describes a workflow that no existing skill handles and wants it automated
- User says "teach the agent to do X" or "make it remember how to do X"
- User asks to override or specialize a bundled skill's behavior
- The system prompt marks a repeated workflow as a create/update opportunity, even if the user did not explicitly ask for a skill that turn

## Core Process

At a high level, creating a skill follows this sequence:

1. **Understand** what the user needs (capture intent)
2. **Plan** the skill structure (which tools, what scope)
3. **Create** the directory and SKILL.md file
4. **Validate** the skill is well-formed
5. **Test** with a real request
6. **Iterate** based on feedback

Figure out where the user is in this process and jump in from there. If they already have a draft, skip to validation. If they describe a workflow, start from understanding.

## Curiosity Loop

Treat skill creation as learning a route through a real environment, not writing a generic prompt. For live apps, browser portals, internal systems, and fragile UIs, be curious before freezing behavior into a skill.

1. **Map the surface.** Identify entry points, page landmarks, stable buttons/links, and what success visibly looks like.
2. **Ask one useful question when the map is incomplete.** Prefer questions that remove ambiguity before tool use, such as "Which classes should I check?" or "After I enter a class, is the grade under Grades or Progress?" Do not ask questions whose answer is already visible or discoverable.
3. **Probe before broad execution.** Try the first object/course/account, verify the result, then generalize to the rest.
4. **Capture corrections.** If the user says the route is wrong, treat that as ground truth. Stop repeating the old route and update the most specific skill.
5. **Record failed paths.** Add durable failures as `Known Bad Paths` or `Recovery`, not as vague memory.
6. **Leave open questions.** If the failure mode is not solved yet, write the next thing to try instead of pretending the skill is complete.

For weaker or cheaper models, bias toward explicit verification and one clarifying question. A good skill should make a smaller model behave like a careful operator: observe, act, verify, ask when the map is incomplete, and learn from correction.

## Repeated Workflow Rule

When the surrounding prompt says a workflow has repeated enough times to deserve a skill:

- Create a new skill if no specific existing skill covers that workflow
- Update an existing specific skill if one already covers the workflow and the current run revealed durable improvements
- Do not create duplicates just because the current phrasing differs slightly
- Do not stretch a broad generic skill to cover a narrower multi-system workflow if that would make the generic skill muddy. Example: keep `gmail` for general Gmail navigation/summarization, but create a separate browser-nested skill for `gmail` + `QuickBooks` bill-entry flows.
- Only write stable procedure into the skill
- Prefer a nested sub-skill under the relevant broad capability when the workflow is a specific web app, portal, or product. Example: put Course Portal under `skills/browser/halo/SKILL.md`, not inside the broad browser skill body.
- If a task succeeds or fails in a durable way, update the most specific existing skill rather than relying on memory alone.

Stable procedure belongs in skills:
- tool order
- navigation heuristics
- verification checkpoints
- durable output formats
- recurring blocker handling
- known bad paths and the preferred recovery route
- what to ask the user before acting when the UI state is ambiguous

Changing user facts do **not** belong in skills:
- current semester classes, professors, due dates, grades
- one-off assignments or rubric text
- exact invoice amounts, vendor balances, or single email contents
- temporary URLs, thread state, or transient page details

Put changing facts in memory, workflow bindings, or the live task response instead.

## Communicating with the User

Pay attention to context cues about the user's technical level. Some users are seasoned engineers; others are new to the concept. If in doubt, briefly explain terms. Be concrete and specific.

---

## Step 1: Capture Intent

Start by understanding what the user wants. The current conversation might already contain a workflow to capture. If so, extract from context first before asking questions.

Key questions (skip what's already clear from context):

1. What should this skill enable the agent to do?
2. When should it trigger? (what user phrases or contexts)
3. What tools does it need? (check available tools in the system)
4. What's the expected output or behavior?
5. Are there edge cases or constraints?

If the user says "turn this into a skill" after demonstrating a workflow, extract the tools used, the sequence of steps, corrections they made, and input/output formats. Confirm before proceeding.

For on-demand operational skills, also extract:

- **Entry point:** where the agent should begin
- **Landmarks:** visible labels/pages that prove it is on the right surface
- **Action route:** clicks/forms/navigation that worked
- **Verification:** what the agent must read before claiming success
- **Known bad paths:** actions or URLs that failed
- **Recovery:** what to try after each known failure
- **Open questions:** what the next agent should ask or test if the route is still uncertain

## Step 2: Plan the Skill

Before writing, decide:

- **Which tools** will the skill use? Reference exact tool names from the system.
- **What scope?** A skill should do one thing well. If it's too broad, split it.
- **Does it need helper resources?** Scripts, reference docs, or templates.
- **Does it overlap** with an existing skill? If so, consider overriding instead.

When updating an existing skill:

- Read the current `SKILL.md` first.
- If you need to change multiple lines or a section boundary, prefer writing the full updated file after incorporating the changes instead of a brittle exact-string edit.
- Only use `edit` or targeted patching when you have already verified the exact old text exists.
- If a filesystem edit tool fails, do not pretend the update succeeded. Retry with a safer tool route or explicitly conclude that no change was needed.
- If the user explicitly says `stop after the skill write`, `do not open the browser/app yet`, or otherwise asks for skill authoring before live execution, do not continue into the live workflow after writing or updating the skill. Stop and return the skill path cleanly.

## Step 3: Create the SKILL.md

### Where to Create

- New skills go in `./skills/<skill-name>/SKILL.md` (workspace tier, highest priority)
- Every workspace-authored `SKILL.md` must stay somewhere under `./skills/...`; never create, move, or copy a `SKILL.md` outside that tree
- Never modify bundled skills directly
- To override a bundled skill, create a workspace skill with the same name

### Directory Structure

```
skills/<skill-name>/
+-- SKILL.md           <-- Required: the skill prompt
+-- scripts/           <-- Optional: executable helpers
+-- references/        <-- Optional: docs loaded on demand
+-- assets/            <-- Optional: templates, configs
```

Most skills need only the SKILL.md file. Only add subdirectories when there's a clear need.

### SKILL.md Format

Every SKILL.md has two parts: YAML frontmatter and a markdown body.

#### Required Frontmatter

```yaml
---
name: my-skill-name
description: "What this skill does AND when to use it. Both parts are critical."
---
```

- **name**: lowercase, hyphens, digits only. Max 64 chars. Must match the directory name.
  Prefer short, human-readable workflow names in kebab-case, usually 2-4 words, such as `halo-assignment-workflow`, `gmail-invoice-bills`, or `quickbooks-vendor-entry`.
  Do not use random suffixes, timestamps, transport/UI labels, test markers, duplicate words, or raw chunks of the user's phrasing unless the user explicitly asked for a disposable test skill.
- **description**: 1-3 sentences. Must include BOTH what the skill does AND when to trigger it. This is the primary triggering mechanism. Be specific but not so narrow it misses valid use cases. Lean slightly "pushy" to combat undertriggering.

#### Optional Frontmatter

```yaml
---
name: my-skill-name
description: "..."
metadata:
  andromeda:
    emoji: "icon"               # Visual identifier
    always: true                # Always load, skip eligibility checks (default: false)
    skillKey: "custom-key"      # Override key for config lookups
    primaryEnv: "API_KEY"       # Main env var name
    homepage: "https://..."     # Documentation URL
    os:                         # Platform filter
      - darwin
      - linux
    requires:                   # Eligibility requirements
      bins:                     # ALL must exist in PATH
        - some-cli
      anyBins:                  # At least ONE must exist
        - brew
        - apt
      env:                      # ALL env vars must be set
        - SOME_TOKEN
      config:                   # Config paths that must be truthy
        - channels.slack
    install:                    # Dependency installation methods
      - id: brew
        kind: brew              # brew | node | go | uv | download
        formula: some-cli
        bins: [some-cli]
        label: "Install via Homebrew"
        os: [darwin]
user-invocable: true            # Can user invoke via /command? (default: true)
disable-model-invocation: false # Hide from model entirely? (default: false)
andromeda-workflow-packs: pack-id              # Workflow pack integration
andromeda-workflow-pack-priority: preferred    # preferred | supplement
---
```

For most user-created skills, only `name` and `description` are needed. Add metadata fields only when there are actual requirements (binaries, env vars, platform restrictions).

#### Body Guidelines

The markdown body is the prompt that teaches the model. Rules:

1. **Use imperative form.** "Use `gh pr list`" not "You should use `gh pr list`"
2. **Be specific about tool names.** Reference exact tool names available in the system.
3. **Include examples.** Show actual commands, API calls, or tool invocations with realistic inputs.
4. **Stay under 500 lines.** Context is expensive. Say only what the model needs. If approaching this limit, use reference files with clear pointers.
5. **Define the workflow order.** "First do X, then Y, then Z"
6. **Include error handling.** "If X fails, try Y instead"
7. **Explain the why.** Rather than heavy-handed MUSTs, explain why a particular approach matters. Models respond better to reasoning than rigid commands.
8. **No README-style content.** This is instructions for models, not documentation for humans.
9. **Be general, not overfitted.** Write instructions that work across many inputs, not just the examples shown.
10. **Do not claim a file update succeeded unless the filesystem tool succeeded or you verified the file already matched the intended result.**
11. **Include a learning log for fragile workflows.** Use short sections like `Known Good Path`, `Known Bad Paths`, `Recovery`, and `Open Questions` when the skill is for a live UI or portal.
12. **Teach when to stop.** If the visible surface shows all relevant objects have been checked, the skill should say to answer with the verified result instead of searching imaginary extras.

For browser-native portal skills, include a dry-run or no-tools override when the workflow is commonly requested in both live and simulated form. If the user says `dry run`, `do not use tools`, `without tools`, `simulate`, or explicitly says not to open the browser/app, the skill should suppress browser/app tools and return the requested structure from the learned workflow instead of attempting live navigation.
If the user combines that with `stop after the skill write`, the current turn should end immediately after the create/update succeeds instead of continuing into the app or portal.

#### Progressive Disclosure

Skills use a three-level loading system:

1. **Metadata** (name + description) -- Always in context (~100 words)
2. **SKILL.md body** -- Loaded when the skill triggers (<500 lines ideal)
3. **Bundled resources** -- Read on demand (unlimited, scripts can execute without loading)

Keep the SKILL.md body lean. Move large reference content to `references/` files and point to them clearly.

### Writing the Description

The description is the most important field. It determines whether the skill triggers.

**Good description pattern:**
```
"[What it does]. Use when [trigger conditions]. Triggers on: [example phrases]."
```

**Example:**
```yaml
description: "Manage Jira tickets via the Jira API: create issues, update status, assign, comment, and query boards. Use when the user mentions Jira, sprint planning, ticket management, or issue tracking. Triggers on: 'create a Jira ticket', 'update the sprint', 'check my Jira board', 'assign this issue'."
```

**Common mistakes:**
- Too vague: "Helps with project management" (won't trigger correctly)
- Too narrow: "Creates Jira tickets" (misses updates, queries, etc.)
- Missing trigger context: "Jira operations via API" (no "when to use" guidance)

### Choosing the Directory and Name

Use the skill name to describe the durable workflow, not the current chat turn.

- Good top-level names: `halo-school`, `gmail-invoices`, `quickbooks-bills`
- Good nested names under a broad parent skill: `skills/browser/halo-school/SKILL.md`, `skills/browser/sharepoint-docs/SKILL.md`
- Bad names: `web-ui-root-skill-abc123`, `manual-skill-e2e-20260405`, `halo-web-ui-skill-web-ui-skill-mnl97gij`

Name rules:

1. Prefer the stable product or workflow nouns, not the testing context.
2. Avoid timestamps, random IDs, `tmp`, `test`, `e2e`, `copy`, `final`, `v2`, or repeated words unless the user specifically asked for a temporary test artifact.
3. If the workflow clearly belongs under a broad platform namespace, nest it there. Example: Course Portal inside browser can live at `skills/browser/halo-school/SKILL.md`.
   Browser-native portals and web apps such as Course Portal, Canvas, Blackboard, Moodle, Google Classroom, SharePoint, Gmail, and Outlook should usually nest under `skills/browser/<name>/SKILL.md`.
   If a generic platform skill already exists and the new workflow adds a second system or a specialized business process, keep the generic skill clean and create a more specific sibling under the same namespace instead of stuffing everything into the broad skill.
4. If nesting does not add clarity, keep the skill at the top level.
5. If the user asks for a real reusable skill, optimize for a name they would still understand in three months.
6. Never place a `SKILL.md` anywhere outside `./skills/...`, even temporarily. If you are given a bad path, correct it to a valid path under `./skills/`.

### Example: Complete Simple Skill

```markdown
---
name: docker-deploy
description: "Build and deploy Docker containers: Dockerfile creation, image builds, container management, and docker-compose workflows. Use when the user asks about Docker, containers, deployment, or mentions Dockerfiles, images, or compose files."
metadata:
  andromeda:
    emoji: "\U0001F433"
    requires:
      bins: ["docker"]
---

# Docker Deploy

Use this skill for all Docker-related tasks.

## Building
1. Check if a Dockerfile exists in the workspace root.
2. If not, create one based on the project type (detect from package.json, requirements.txt, go.mod, etc.).
3. Build with: `docker build -t <project-name> .`

## Running
- Single container: `docker run -d -p <port>:<port> <image>`
- Multi-service: use docker-compose if a compose file exists or create one.

## Common patterns
- If the user says "deploy", build and run.
- If the user says "containerize", create a Dockerfile.
- Always check for existing Docker configs before creating new ones.

## Error handling
- If `docker` is not running, tell the user to start Docker Desktop.
- If a port is in use, suggest an alternative.
```

## Step 4: Validate

After creating the SKILL.md, verify:

1. **Frontmatter parses correctly** -- Valid YAML between `---` fences
2. **Name field** -- Lowercase, hyphens/digits only, matches directory name, max 64 chars
   Also check that the name is human-meaningful and not a throwaway artifact like a timestamped smoke-test label.
3. **Description field** -- Non-empty, includes what AND when
4. **Body is non-empty** -- Has actual instructions
5. **File size** -- Under 256KB
6. **No conflicts** -- Name doesn't collide with an existing skill unintentionally (if intentional, that's an override)

Read the file back after writing to confirm it saved correctly.

## Step 5: Test

After creating the skill, the hot-reload watcher will detect the new file automatically. The skill appears in the catalog on the next turn.

Test by asking the user to try a prompt that should trigger the skill. Verify:
- The skill triggers when expected
- The instructions produce correct behavior
- The description doesn't cause false triggers on unrelated requests

## Step 6: Iterate

Based on testing and user feedback:

1. Edit the SKILL.md to address issues
2. The watcher picks up changes automatically
3. Re-test on the next turn
4. Repeat until the skill works reliably

Focus improvements on:
- **Generalize from feedback.** Don't overfit to specific examples.
- **Keep the prompt lean.** Remove instructions that aren't pulling their weight.
- **Explain the why.** Understanding beats rigid rules.
- **Look for repeated patterns.** If the model keeps doing the same setup work, bake it into the skill.

---

## Editing Existing Skills

When the user asks to improve or edit an existing skill:

1. Read the current SKILL.md file
2. Understand what needs to change (ask if unclear)
3. Edit the file in place (for workspace/personal skills)
4. For bundled skills: create a workspace override at `./skills/<same-name>/SKILL.md`
5. Validate the updated version
6. Test the changes

### Skill Hierarchy (Override Rules)

Skills load from multiple locations. Later sources override earlier ones by name:

| Priority | Location | Editable? |
|----------|----------|-----------|
| 1 (lowest) | Plugin/extra dirs | Depends |
| 2 | `skills/` (bundled) | No |
| 3 | `~/.andromeda/skills/` | Yes |
| 4 | `~/.andromeda/workspace/skills/` | Yes |
| 5 | `~/.agents/skills/` | Yes |
| 6 | `.agents/skills/` (project) | Yes |
| 7 (highest) | `./skills/` (workspace) | Yes |

A workspace skill named `slack` overrides the bundled `slack` skill. This lets you specialize behavior without touching the baseline.

---

## Anti-Patterns to Avoid

- **Don't create README.md, CHANGELOG.md, or docs** alongside SKILL.md. The skill IS the documentation.
- **Don't duplicate what another skill does.** Extend or override instead.
- **Don't put implementation code in SKILL.md.** It's instructions, not a script. Put scripts in `scripts/`.
- **Don't make descriptions too vague** ("helps with stuff") or too broad ("handles all tasks").
- **Don't create ugly throwaway names** with random IDs, timestamps, repeated words, or test-harness labels unless the user explicitly asked for a temporary test skill.
- **Don't use rigid ALWAYS/NEVER in all caps** unless truly critical. Explain the reasoning instead.
- **Don't create skills for one-off tasks.** Skills are for repeated workflows.
- **Don't exceed 500 lines.** Use reference files for large content.
- **Don't forget the "when to use" part** of the description. It's how triggering works.

---

## Quick Reference: Minimal Skill Template

```markdown
---
name: SKILL_NAME_HERE
description: "WHAT_IT_DOES. Use when TRIGGER_CONDITIONS. Triggers on: EXAMPLE_PHRASES."
---

# SKILL_TITLE_HERE

INSTRUCTIONS_HERE
```

Replace the placeholders, save to `./skills/SKILL_NAME_HERE/SKILL.md`, and the skill is live on the next turn.
