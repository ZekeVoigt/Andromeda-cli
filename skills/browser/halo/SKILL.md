---
name: halo
description: "Navigate Halo school portal: open Halo in the user's browser, confirm login, find courses, view assignments, and access rubrics/details. Use tasks involving Halo, courses, assignments, or rubrics. Triggers on: 'Halo', 'Halo course', 'Halo assignment', 'Halo rubric', 'Halo grades'."
---
# Halo Portal Navigation Skill

This skill provides instructions for navigating the Halo school portal using the user's browser.

## Core Workflow: Login -> Classes Dashboard -> One Course -> Course Gradebook -> Repeat

## Verified Control Pattern

For Halo, prefer the existing authenticated user browser session over opening new tabs:

1. List or focus existing tabs first:
```json
{ "tool": "browser", "action": "focus", "profile": "user", "query": "halo.gcu.edu" }
```
For grade checks, start from the main Halo/classes surface unless the user explicitly says a specific gradebook tab is already open and current.

2. Read the target page with structured browser text/snapshot, not full-screen image analysis:
```json
{ "tool": "browser", "action": "snapshot", "profile": "user" }
```

3. Act only from fresh refs/selectors returned by the latest snapshot. Re-snapshot after each meaningful click.

4. If the user must log in, choose an account, or complete 2FA, ask for that exact visible-browser handoff. After the user says they are done, reuse the same tab and continue reading/acting.

5. Do not repeatedly open Halo. Duplicate Halo tabs confuse targeting and feel broken. If a Halo tab already exists, focus/reuse it. If page data is unavailable after a valid focus and snapshot/read, report the concrete blocker.

6. Do not construct or retry direct `/student/gradebook` URLs as the primary path. Halo gradebook URLs are course/session-specific and stale or guessed gradebook URLs often land on an "Oops" page. If a gradebook URL fails once, go back to `https://halo.gcu.edu`, open the classes/dashboard view, click into each course, then open that course's gradebook from inside the course UI.

### 1. Open or Focus Halo

Always use `profile: "user"` to interact with the user's real browser session, ensuring access to their existing login and cookies.

To open Halo at the main portal URL:
```json
{ "tool": "browser", "action": "open", "profile": "user", "targetUrl": "https://halo.gcu.edu" }
```
To focus an existing Halo tab if it's already open:
```json
{ "tool": "browser", "action": "focus", "profile": "user", "query": "halo.gcu.edu" }
```

### 2. Confirm Login State and Handle Blockers

After opening or focusing, **always** capture an interactive snapshot to assess the current page state, looking for login prompts or the main dashboard.
```json
{ "tool": "browser", "action": "snapshot", "profile": "user", "interactive": true }
```
-   **If the snapshot indicates a login page (e.g., "Sign In", "Username", "Password" fields are present in the page text or refs):**
    -   Report a browser permission blocker, as Andromeda cannot perform automated login due to security protocols.
    -   Clearly instruct the user that they must manually log in to their Halo account in the browser.
    -   Wait for the user to confirm they have successfully logged in before attempting further navigation.
-   **If the snapshot indicates the main dashboard (e.g., "My Courses", "Dashboard", "Welcome" messages, or visible course cards):** Proceed to course navigation.

### 3. Navigate to Each Specific Course

Once on the main Halo dashboard, capture another interactive snapshot to identify available courses.
```json
{ "tool": "browser", "action": "snapshot", "profile": "user", "interactive": true }
```
-   **Identify Course Cards:** Look for interactive elements (refs) that represent individual courses. These typically have `role: "link"` or `role: "button"` and their `name` or `text` will contain course titles (e.g., "MKT-415 Digital Marketing").
-   **"GO TO CLASS" Buttons:** If present and associated with a specific course card, these are high-confidence targets. Prefer clicking these from the dashboard/classes page.
-   **General Course Links:** If direct buttons are absent, click on the course title link itself.
```json
{ "tool": "browser", "action": "act", "profile": "user", "request": { "kind": "click", "ref": "u<ref_id_for_course_card_or_button>" } }
```
After clicking a course, **always** capture a new interactive snapshot to verify navigation to the course's main page.

For multi-course grade requests, treat each course as a separate loop:
1. On the Halo dashboard/classes page, identify the target course card.
2. Click that course's "GO TO CLASS" or course title.
3. Verify the course page title/name.
4. Open Grades/Gradebook from inside that course.
5. Read and record the visible current grade.
6. Return to the dashboard/classes page and repeat for the next course.
7. Stop once every visible/requested course has been checked. Do not invent additional enrolled classes to inspect.

### 4. Access Assignments, Rubrics, or Grades within a Course

Once inside a specific course page, capture another interactive snapshot.
```json
{ "tool": "browser", "action": "snapshot", "profile": "user", "interactive": true }
```
-   **Look for Navigation Links:** Identify links or buttons with `name` or `text` matching "Assignments", "Grades", "Rubrics", "Modules", or specific assignment titles. These often appear in a left-hand navigation menu or a central content area.
-   **Direct Assignment Links:** If a specific assignment is requested, look for its title as a clickable element.
```json
{ "tool": "browser", "action": "act", "profile": "user", "request": { "kind": "click", "ref": "u<ref_id_for_target_link>" } }
```
After clicking, **always** capture a new interactive snapshot to confirm arrival on the intended assignment details, rubric, or grades page.

### 5. Data Extraction and Summary

Once on the target page (e.g., assignment details, rubric, gradebook):
1.  Capture a final snapshot to get the full page text.
2.  Parse the `text` field from the snapshot result to extract requested information (e.g., assignment name, due date, rubric criteria, current grade).
3.  Present the extracted data to the user in a clear and concise format.

For grade checks, especially requests like "tell me my grade for both classes showing," prefer structured browser reads over screenshots:
-   Use `browser.snapshot` or page text from the logged-in `profile: "user"` tab to identify visible course names/cards and grade labels.
-   Do not assume multiple classes share one gradebook. Each course has its own gradebook/progress page.
-   If the user asks for two or more classes, collect each course grade separately and include the course name beside each grade.
-   If a course gradebook says no grade data is found, report exactly that as the verified state for that course and label it as uncertain/blank. Do not assume the course is wrong or search unrelated classes unless the dashboard shows another clearly relevant course.
-   If structured text is incomplete, scroll the gradebook/classes area and snapshot again.
-   Avoid full-screen screenshot image analysis for grades; it can exceed gateway payload limits. If vision is required, capture only the Halo window or the smallest visible grade-card region and use OCR/text extraction first.

## Known Good Path

- Start at `https://halo.gcu.edu` in `profile: "user"`.
- Use the visible/classes dashboard as the source of truth for enrolled courses.
- Enter each course from its card or "GO TO CLASS" button.
- Open Grades/Gradebook from inside that course page.
- Verify the course title and gradebook text before reporting the grade.
- Return to the classes/dashboard page before checking the next course.

## Known Bad Paths

- Do not guess direct `/student/gradebook` URLs. They can be stale or course/session-specific and may return an "Oops" page.
- Do not treat one failed gradebook URL as proof that Halo is down.
- Do not report "I'll check other classes you are enrolled in" after all visible/requested courses have already been checked.

## Open Questions

- If a course gradebook shows "No grade data found" but the course is visible and current, ask whether the user expects grades elsewhere in that course (for example Progress, Assignments, or a different grade tab) before searching unrelated classes.

## Heuristics and Best Practices

-   **Browser Profile:** ALWAYS use `profile: "user"` for all Halo interactions to leverage the user's active session.
-   **Snapshot Cadence:** ALWAYS capture a new `snapshot` (with `interactive: true`) after each major navigation step (open, focus, any click that causes a page load or significant UI change) to obtain updated page references and content.
-   **Ref-Based Interaction:** Prefer using interactive refs (`u<id>`) and page `text` from `browser.snapshot` for navigation and information extraction. Avoid coordinate-based clicks unless structured refs are genuinely unavailable.
-   **Permission Blockers:** Clearly report browser login blockers and require manual user intervention. Do not attempt to bypass login.
-   **Error Handling:** If a navigation or click action fails or lands on an unexpected page, re-snapshot and re-evaluate the available refs and page text before retrying or reporting a persistent blocker.
-   **User Corrections:** If the user says the current navigation strategy is wrong, treat that as ground truth. Stop retrying the same URL or action. Re-observe from the main Halo/classes page and follow the user's stated path.
-   **Page-Shape Cues:** When looking for courses, assignments, or grades, common cues include:
    -   Headings like "My Courses," "Dashboard," "Assignments," "Grades."
    -   List items or cards containing course titles.
    -   Navigation menus on the left or top of the page.
    -   Presence of "Sign In" forms for login detection.

## Dry-Run Request Format

For dry-run chat requests (where the user explicitly asks not to use tools or to simulate):
-   Do not use any `browser` tools.
-   Respond with the following exact sections, populating them with hypothetical information based on the request:
```
State: <Current hypothetical state, e.g., "Logged in, on Dashboard", "In MKT-415 Course">
Course: <Hypothetical Course Name, e.g., "MKT-415 Digital Marketing">
Page: <Hypothetical Current page, e.g., "Dashboard", "Assignments List", "T5 Digital Marketing Assignment Rubric">
Next Step: <What the agent would hypothetically do next, e.g., "Click 'GO TO CLASS' for MKT-415", "Summarize visible rubric details">
