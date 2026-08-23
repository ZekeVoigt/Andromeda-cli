---
name: browser
description: "Website and browser automation on the user's machine: Google search, navigation, forms, tabs, snapshots, ref-based clicking/typing, and data extraction from live browser pages."
metadata:
  andromeda:
    emoji: "🌐"
---

# Browser Skill
Use this skill for website tasks, Google searches, browser tabs, forms, data extraction, and web app automation.

## Profile Selection (Critical)

The `browser` tool has built-in profiles that control HOW the browser is accessed:

- **`profile: "user"`** — Controls the user's VISIBLE local browser (Google Chrome, Safari). Use this when:
  - The user says "open Chrome", "go to [site]", "check my [app]", "look at my grades", etc.
  - The user wants to interact with a site where they are already logged in (Course Portal, Gmail, Canvas, bank, etc.)
  - The user references "my browser", "my tabs", or any site requiring their existing session/cookies
  - The task involves extracting data from a page the user can see
  - **This is the default choice for most real-world user requests**

- **`profile: "andromeda"` (default if omitted)** — Launches a managed Chromium browser controlled by Andromeda. Use this when:
  - The task doesn't require the user's cookies or login sessions
  - You need a clean browser for automated testing or scraping public data
  - The user explicitly asks you to "open your own browser" or doesn't care which browser

- **`profile: "chrome"`** — Attaches to Chrome over CDP/relay for structured takeover. Use when CDP is configured.

**Rule: When the user mentions Chrome, their browser, or any site requiring their login, ALWAYS use `profile: "user"`.**

## Tool Preference Order
1. `web_search` or `web_fetch` — info-only tasks (no page interaction needed)
2. `browser` with `profile: "user"` — interact with user's real browser (click, type, read, extract)
3. `browser` with default profile — automated browser tasks not requiring user sessions
4. `analyze_local_screenshot` — read visible text from the user's screen
5. `app-control`, `keyboard-mouse`, `screenshot` — last resort fallback

## Core Workflow: Snapshot → Act → Snapshot

Every browser interaction follows this pattern:

### Step 0: Reuse Before Opening
When the user names a logged-in website or already has the target tab open, list/focus tabs before opening a new one. Duplicate tabs create targeting ambiguity and make the assistant feel out of control.

```json
{ "action": "focus", "profile": "user", "query": "site or page title" }
```

If a specific page is requested, use the most precise title/URL query available, such as `"Course Gradebook"` or `"/student/gradebook"`.

### Step 1: Open or Focus
```json
{ "action": "open", "profile": "user", "targetUrl": "https://example.com" }
```
Or if the site is already open:
```json
{ "action": "focus", "profile": "user", "query": "example.com" }
```
Or list tabs first:
```json
{ "action": "tabs", "profile": "user" }
```

### Step 2: Capture Snapshot (Get Interactive Refs)
```json
{ "action": "snapshot", "profile": "user" }
```
This returns a list of interactive elements (buttons, links, inputs) with refs like `u1`, `u2`, `u3`. Each ref has a role, name, and interaction type (click/type/select).

### Step 3: Act Using Refs
Click a button:
```json
{ "action": "act", "profile": "user", "request": { "kind": "click", "ref": "u5" } }
```
Type into a field:
```json
{ "action": "act", "profile": "user", "request": { "kind": "fill", "ref": "u3", "text": "hello" } }
```
Submit a form after typing:
```json
{ "action": "act", "profile": "user", "request": { "kind": "fill", "ref": "u3", "text": "hello", "submit": true } }
```
Press a key:
```json
{ "action": "act", "profile": "user", "request": { "kind": "press", "ref": "u2", "key": "Enter" } }
```
Select a dropdown option:
```json
{ "action": "act", "profile": "user", "request": { "kind": "select", "ref": "u7", "values": ["option1"] } }
```
Scroll an element into view:
```json
{ "action": "act", "profile": "user", "request": { "kind": "scrollIntoView", "ref": "u12" } }
```

### Step 4: Re-snapshot After Actions
After clicking a button, submitting a form, or navigating, ALWAYS capture a fresh snapshot to see the updated page state before taking the next action.

## Data Extraction Pattern

To extract data from a web page (grades, prices, schedules, etc.):
1. Navigate to the page: `action: "open"` with `profile: "user"`
2. Capture snapshot: `action: "snapshot"` — this returns the page text AND interactive refs
3. Read the `text` field from the snapshot result — it contains the visible page content
4. If data spans multiple pages, click "Next" or scroll, then snapshot again
5. Present the extracted data to the user in a clear format

For private portals and dashboard-style web apps, do not jump from a failed snapshot directly to full-screen `analyze_local_screenshot`. Full-screen image payloads can exceed the gateway request limit. Try browser page text, an interactive snapshot, tab focus plus snapshot, or page-specific navigation first. If vision is genuinely needed, capture the smallest useful target window or region and use OCR/text extraction before image analysis.

## Prerequisites (macOS)

For `profile: "user"` to work with Google Chrome:
- Chrome must have **"Allow JavaScript from Apple Events"** enabled
  - Open Chrome → View → Developer → check "Allow JavaScript from Apple Events"
  - Without this, Andromeda cannot read or interact with page content
- If interaction fails with an AppleScript error, tell the user to enable this setting

## Rules
1. If the user says "google", "look up", or "search" — prefer `web_search` unless they explicitly want the real browser opened.
2. ALWAYS use `profile: "user"` when the task involves the user's browser, their login sessions, or sites they need to be authenticated on.
3. ALWAYS snapshot before acting. Never guess selectors — use refs from the snapshot.
4. After major navigation or form submission, capture a fresh snapshot before the next action.
5. Prefer DOM/ref-based actions over coordinate-based mouse control.
6. If a site is already open in the user's tabs, use `action: "focus"` with a query to find it instead of opening a new tab.
7. Do not ask the user for x/y coordinates. Use snapshots to find interactive elements and act on refs.
8. When a workflow repeats or a tool failure teaches a durable lesson, update the most specific browser subskill after the live task is handled. Keep changing facts out of skills.
9. If the browser action surface returns an unknown action error, treat that as a gateway/tool contract bug, not a website failure. Record the exact action name and fix or report the gateway mismatch.
