---
name: gmail
description: "Navigate Gmail in the browser: open Gmail, confirm login state, locate messages, open threads, read visible email content, and use Gmail controls like reply, archive, star, or labels when requested. Use when the user mentions Gmail, inbox, email, messages, threads, attachments, or reading/interacting with mail in Gmail."
---

# Gmail Browser Skill

This skill teaches the agent to navigate and interact with Gmail in the user's browser session.

## Core Principles:
- **Always use `profile: "user"`** for all browser actions to interact with the user's real, logged-in Gmail session.
- **Always capture a `snapshot`** after each significant navigation step (opening Gmail, landing on Inbox, opening a thread) to get the updated page state and interactive elements.
- **Prefer `snapshot` refs and text content** over `analyze_local_screenshot` for extracting information from the page.
- **Clearly report browser permission or login blockers** to the user and await manual intervention when necessary (e.g., for login).

## Workflow:

### 1. Open Gmail and Confirm Login:
- Open Gmail using `profile: "user"` and the target URL `https://mail.google.com/`.
- Capture a `snapshot`.
- Check the snapshot `text` for login indicators (e.g., "Sign in", "Enter your password") or the visible presence of typical Gmail inbox content.
- If not logged in or a login/account picker is present, clearly state the blocker and ask the user to log in manually. **Await explicit user confirmation of successful login before proceeding.**
- After manual login, capture a new `snapshot` to re-evaluate the page state.

### 2. Land on Inbox or Mailbox:
- After confirming login and/or user intervention, inspect the `snapshot` to identify the current mailbox (e.g., "Inbox", "Sent Mail").
- If the desired mailbox is not active, use `act` with the appropriate `ref` to navigate to it.
- Capture another `snapshot` after navigation.

### 3. Recognize Thread Lists:
- From the `snapshot`, identify and list visible email threads. Each thread typically has a sender, subject, and possibly a snippet of the content.

### 4. Open a Thread:
- When asked to open a specific thread, use `act` with the `ref` corresponding to that thread.
- Capture a new `snapshot` after opening the thread.

### 5. Summarize Thread Content (Live Interaction):
- After opening a thread and capturing a snapshot, extract the following from the snapshot `text` (e.g., for invoice triage, look for vendor and amount cues):
  - **Sender:** The name or email of the sender.
  - **Subject:** The subject line of the email.
  - **Attachment Presence:** Indicate if attachments are present (e.g., "Attachment: Yes" or "Attachment: No").
  - **Key Information:** Extract specific details relevant to the task (e.g., "Vendor: [Name]", "Amount: [Value]" for invoice triage).
  - **Next Step Cues:** Suggest logical next actions (e.g., "Ready to reply", "Can archive", "Summarizing email body", "Ready for QuickBooks bill-entry").

## Error Handling:

- If `browser` tool reports permission issues (e.g., "Allow JavaScript from Apple Events" is not enabled), instruct the user on how to enable it.
- If a navigation or action fails, re-snapshot and re-evaluate the page state before retrying or escalating.
- If a "Do not use tools" or "dry-run" constraint is given for a task requiring live interaction (like invoice triage), explain why tools are necessary and ask the user to remove the constraint to proceed.
