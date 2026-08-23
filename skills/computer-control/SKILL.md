---
name: computer-control
description: "Control the local computer: apps, files, shell, clipboard, screenshots, keyboard, mouse, and system state."
metadata:
  andromeda:
    emoji: "🖥️"
---

# Computer Control Skill
Use this skill when a task must happen on the user's actual computer rather than only in chat.

Prefer the most structured tool first:
- Inspect state with `analyze_local_screenshot`, `screenshot`, `frontmost_app`, `list_apps`, `system_info`, `read`, `ls`, `find`, or `grep`.
- Use `launch_app`, `focus_app`, and `quit_app` for desktop app control.
- Use `read_clipboard` and `write_clipboard` for copy/paste workflows.
- Use `exec` for deterministic local shell work, including OS-level automation when structured tools are not enough.
- Use `type_text`, `press_keys`, `mouse_click`, `mouse_double_click`, `mouse_right_click`, `move_mouse`, and `scroll_mouse` to drive the visible UI once the target app is focused and understood.

Operating rules:
1. Break work into small steps and verify after each major action.
2. When focus is unclear, inspect with `frontmost_app`, `analyze_local_screenshot`, or `screenshot` before typing or clicking.
3. Default to background-safe execution that does not steal focus or switch Spaces when structured browser or accessibility-node actions can do the job.
4. Browser reads are usually safer than browser writes during parallel work. Avoid browser actions that open, switch, or create tabs/windows unless the user explicitly wants browser control or a dedicated browser window is already in play.
5. Native desktop apps without dedicated integrations are still controllable: prefer `ui_tree_snapshot`, `click_ui_node`, `type_ui_node`, and dialog tools before raw keyboard or mouse input.
6. `focus_app`, `focus_window`, `type_text`, `press_keys`, and mouse tools are visible-computer takeover tools. Use them only when the task truly requires foreground control or the user explicitly wants that behavior.
7. If the human is working in parallel, avoid visible takeover tools unless there is no reliable structured/background-safe route left.
8. Do not answer that you can only open or launch an app when generic computer-control tools are available.
9. Prefer browser-specific tools for websites and app-specific tools for apps; use raw keyboard or mouse only when that is the most reliable option.
10. Before destructive or externally visible actions, require or wait for approval when policy says to.
11. Do not ask the user for screen coordinates or screenshot regions unless autonomous inspection has genuinely failed after trying browser/page-text tools, screenshots, scrolling, or focus changes yourself.

Common control loop:
1. `launch_app` or a structured/background-safe tool
2. `analyze_local_screenshot`, `screenshot`, `frontmost_app`, or `ui_tree_snapshot`
3. Structured app/browser action first; `type_text`, `press_keys`, or mouse tools only if necessary
4. `screenshot` or a structured read to confirm what changed
5. Repeat until the task is complete
