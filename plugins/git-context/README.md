# git-context

Tells the model which branch you are on and whether the tree is dirty, once per
turn. Adds `git_status` so it can ask again after it commits, and `/branch` for
you.

Enabled with `andromeda plugins enable git-context`. It declares no
capabilities: everything it does is additive.

## Why it is here

It is the reference example as much as it is a feature. Two files, forty lines,
and it exercises three of the four registration families:

```python
ctx.register_hook("pre_llm_call", on_llm_call)   # put a fact in front of the model
ctx.register_tool("git_status", ...)             # let the model ask again
ctx.register_command("branch", ...)              # let the person ask
```

## The one decision worth copying

The branch goes in through `pre_llm_call`, **not** through
`register_system_prompt_section`.

A prompt section is part of the cached prefix of every request. The branch
changes during a session, so putting it there means invalidating that cache
every time somebody checks out — you would pay for the whole prompt again to
update eleven characters. `pre_llm_call` injects into the user turn instead:
one line, no cache.

The rule generalises. **A prompt section is for what does not change; a
`pre_llm_call` hook is for what does.**
