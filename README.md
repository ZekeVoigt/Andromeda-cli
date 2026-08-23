# Andromeda CLI

A local-first agent harness for the terminal. The loop runs on your machine.

```bash
andromeda                            # REPL
andromeda "what is 2+2"              # one turn, then exit
git log -5 | andromeda "summarise"   # stdin is folded into the prompt
```

## Install

```bash
curl -fsSL https://ai-andromeda.com/install.sh | bash        # macOS, Linux, WSL
iex (irm https://ai-andromeda.com/install.ps1)               # Windows
```

Clones to `~/.andromeda-cli/checkout/`, builds a `uv` venv, and links
`~/.local/bin/andromeda`.
There is no wheel and no Homebrew formula on purpose — the CLI resolves bundled
assets from its checkout, so an installed-package build would be missing half of
itself.

```bash
andromeda update            # pull and reinstall
andromeda update --check    # what is available, change nothing
andromeda doctor            # what is and is not working
```

`update` is transactional: the revision is recorded before anything moves, and
if the dependency install fails the checkout is reset back. A failed update
leaves the install working rather than leaving new code against old packages —
which fails as an ImportError before the CLI can print why. It refuses outright
on a dirty checkout, because resetting over someone's edits is data loss, not an
update.

From a checkout:

```bash
cd cli
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/andromeda
```

## Two lanes

**`relay`** (default) — the hosted endpoint. It holds the provider key, enforces
the model allowlist, and reserves and settles credit per call. The CLI never
sees a provider key and never touches the ledger.

```bash
andromeda auth login <code>     # code comes from the app
andromeda auth status
```

Pairing mints a device token, stored `0600` in `~/.andromeda-cli/credentials.json`.
A terminal has no Clerk client and so cannot hold a session JWT; this is the
same credential the gateway daemon uses.

**`direct`** — BYOK against any OpenAI-compatible base. Your key, your bill, no
account needed.

```bash
export OPENROUTER_API_KEY=...
andromeda config set provider direct
```

## Tools

```bash
andromeda tools                     # what is on, and how each is gated
andromeda tools enable|disable <name>
```

| Tool | Tier | Default in `ask` mode |
|---|---|---|
| `read_file` `list_dir` `search_files` `todo` | `safe_local` | runs |
| `skill_load` `memory_search` `memory_store` `memory_forget` | `safe_local` | runs |
| `web_fetch` `web_search` `delegate` | `safe_local` | runs |
| `write_file` `patch` `terminal` | `destructive` | asks first |
| `browser_*` | `destructive` | asks first |

`skill_load`, the `memory_*` family and the `web_*` pair share their names,
schemas and tiers with
the desktop runtime's registry, and a test fails the build if they drift apart —
a tool that takes different arguments or is gated differently depending on which
surface you are on is a bug the user experiences as the agent being unreliable.

**Approval.** `ask` (default) stops before anything that changes the machine and
shows you what will happen — for `terminal` that is the command verbatim, never a
paraphrase. Answer `y` once, `a` for the rest of the session, or `n`. An
interrupted prompt counts as `n`; walking away from the keyboard is not consent.

`--approval auto` does not ask. `--approval deny` refuses every tool.

**Non-interactive runs are narrowed, not left to fail.** In a pipe there is
nobody to answer a prompt, so gated tools are not offered at all and the model
plans around what it actually has. `--approval auto` is the explicit way to hand
a script more.

## Where the agent can reach

The file tools are confined to the workspace root — the current directory, or
`--workspace <dir>`. Paths outside it are not reachable, not "reachable with
approval": symlinks are resolved before the check, so a link out of the root does
not work either.

**The shell is not confined.** `terminal` runs real shell commands, and
`cat ~/.ssh/id_rsa` is one of them. There is no blocklist, because a shell that
cannot be escaped is not a shell and any such list loses to `$(printf ...)`. What
contains it is the gate: `terminal` is `destructive`, so `ask` mode shows you the
command first and a non-interactive run never gets it. `--approval auto` is
granting the model your shell.

## MCP servers

One integration, and the CLI gains whatever anyone has published a server for.

```bash
andromeda mcp             # configured servers and their tools
andromeda mcp example     # a starter mcp.json
```

Config lives in `~/.andromeda-cli/mcp.json` and accepts both the `mcpServers`
key every other client writes and the `mcp_servers` snake_case variant, so a
config can be copied in from either spelling without editing. stdio and
streamable HTTP transports.

Tools arrive as `mcp__<server>__<tool>`, so their origin is legible and two
servers exposing `search` do not collide.

**Every MCP tool is `outbound`** — third-party code, configured by you, reaching
somewhere this harness knows nothing about. Tiering by what a server *claims* its
tool does would take the word of the thing being gated. So `ask` mode stops for
you, and a pipe never sees them.

A server that fails to start is recorded and reported, never fatal: the others
still work, and its stderr is in `andromeda mcp`.

## Background processes

`terminal` blocks and kills its process tree on timeout, which is right for
`wc -l` and useless for `npm run dev`.

```
terminal(command="npm run dev", background=true)   →  proc_4dae56ca
process(action="poll", session_id="proc_4dae")     →  what is new since last poll
```

One `process` tool with an action enum — `list`, `poll`, `log`, `wait`, `kill`,
`write`, `submit`, `close` — rather than eight near-identical tools, which would
be eight chances to pick the wrong one. Ids accept any unambiguous prefix.

Output is drained by a reader thread from the moment it starts. Reading lazily
on `poll` looks simpler and deadlocks: a child that fills the 64KB pipe buffer
blocks on write forever while nobody reads. `/ps` lists them, and leaving the
REPL kills them — a session should not end with a dev server holding a port.

## Skills

Read from the existing `skills/` directory — `skills/<name>/SKILL.md`, the same
format the desktop app uses. Discovery checks `ANDROMEDA_BUNDLED_SKILLS_DIR`,
then walks up from the workspace, then `~/.andromeda-cli/skills`.

Only names and one-line descriptions go into the prompt; bodies are loaded on
demand with `skill_load`. A skill whose required binaries are missing is marked
unavailable, and loading it says so before the instructions — otherwise the
agent follows steps that cannot work and reports a failure you have to decode.

`/skills` lists them in the REPL.

## Memory

`memory_store`, `memory_search` and `memory_forget`, backed by
`~/.andromeda-cli/memory/`. `standing` memories load into every prompt and are
capped; `episode` memories are recalled by search. Restating a known fact
consolidates rather than duplicating.

**One divergence from the desktop runtime, stated rather than hidden:** recall
here is *lexical* (term overlap), not semantic. `minScore` is the same range but
not the same meaning — a paraphrase the desktop side would recall may score zero
here. That is the cost of not shipping an embedding model with a terminal client.

## The web

`web_fetch` reduces a page to text with the standard library — no parsing
dependency. It refuses private, loopback and link-local addresses, **and
re-checks after redirects**, so a public URL that 302s to `169.254.169.254`
does not walk through the guard.

`web_search` needs a provider and is registered only when one is configured
(`BRAVE_SEARCH_API_KEY` or `TAVILY_API_KEY`). Without a key the tool does not
exist, rather than existing and always answering "not configured".

## The browser

```bash
andromeda browser install     # Playwright + Chromium, on demand
andromeda browser status
```

Lazy by design: until it is installed the `browser_*` tools are not registered,
so the model is never offered a browser it cannot open.

**Refs, never pixels.** Every read of a page is a structured outline of its
interactive elements, each with a short ref; every action names one:

```
Example Domain — https://example.com/

# Example Domain

[e1] link "Learn more" (https://iana.org/domains/example)
```

There is no screenshot tool and there will not be one. Reasoning about a page
from an image is slower, costs more, and is wrong in ways that are invisible — a
model that "sees" a button at the wrong coordinates clicks whatever is actually
there.

A field's label and its current value are reported separately, because a model
that cannot see what it typed types it again. Passwords are never shown.

Every `browser_*` tool is `destructive`, including the reads. Not because
reading a page changes this machine — it does not — but because a browser
carries signed-in sessions, and "click e7" is how an agent sends an email or
files an order. The tier describes the surface, not the keystroke.

Same private-network guard as `web_fetch`. To work against a local dev server,
`andromeda config set allow_private_network true` — a session setting, never a
tool argument, because a guard the model can switch off is not a guard.

## Delegation

`delegate` hands one self-contained piece of work to a narrowed helper and waits
for its report.

Lanes run **in the background by default**, at most three at once
(`MAX_CONCURRENT_LANES`). `delegate` returns a lane id immediately so the
next one starts alongside it; `subagents_wait` collects the reports. Three
40-second lanes cost 40 seconds, not two minutes.

`subagents_list`, `subagents_status` and `subagents_wait` take the hosted
registry's names *and* schemas exactly — unlike `delegate`, those three
contracts are ones this harness can honour in full.

Staleness is measured against **progress**, not start time: 450 seconds idle
outside a tool, 1,200 inside one, because a tool can legitimately be slow and a
model turn cannot. A lane fifteen minutes into honest work is not stalled.

The browser is held exclusively for the life of a browser lane, and the worker
slot is always taken *before* the surface — the other order deadlocks the moment
all three slots hold lanes waiting for the browser.

| Lane | Holds | Steps |
|---|---|---|
| `scout` | reads and the web; changes nothing | 12 |
| `browser` | the browser, plus reads. One at a time | 20 |
| `writer` | local reads only — no network at all | 10 |
| `verifier` | reads, and cannot store what it concludes | 12 |

The browser is a single-occupancy surface, so only the browser lane may touch
it. Two lanes driving one browser is worse than two in one mailbox, because
neither can see that it is happening.

The belts are ported from the desktop runtime's specialists, including the
property that makes them mean anything: **a belt is a hard denial, read before
anything else.** A lane runs without prompting, so a tool the belt rejects must
come back `denied` and not `needs_approval` — otherwise a Writer that "cannot
send" is really a Writer that sends after a pause.

A lane's policy is derived with `Policy.narrow()`, so it can only ever hold a
subset of what your session holds — an `allowedTools` naming something you lack
intersects away to nothing rather than granting it. No lane can delegate
further; depth stops at one, guarded three times over.

Each report carries evidence read off the lane's transcript, not its prose:

```
[scout · 3 steps · called read_file×2, list_dir]
```

That line exists because a lane with no shell will write `<shell>…</shell>` into
its answer, and a parent reading that as work is a parent reporting something
that never happened.

`/lanes` lists them in the REPL.

## Long conversations

The transcript is compacted before it hits the window, in two stages, in this
order because they cost very differently:

1. **Micro-compact.** The content of old tool results is replaced with a
   placeholder, keeping the two most recent intact. Free, instant, and usually
   enough: a transcript's weight is nearly always old file reads.
2. **Summarise.** If pruning was not enough, the older part of the conversation
   is summarised and replaced. Costs a model call, so it is only reached when
   it has to be.

The constants: a summary budget of 20% of the window, floor 2,000 tokens,
ceiling 10,000, and compaction starting at 75% — well before the wall, because
the summarisation call needs room to run.

The invariant that keeps it safe: an assistant message carrying `tool_calls`
and the `tool` messages answering them are **one unit**. Splitting them produces
a request the API rejects outright. Every operation moves whole units.

A gauge appears in the prompt once you are past a third of the window.

## Sessions

Every exchange is saved to `~/.andromeda-cli/sessions/` as it completes, not at
exit — the session that most needs recovering is the one that ended in a crash.

```bash
andromeda sessions                  # recent, newest first
andromeda sessions show <id>        # transcript; id prefixes work like git
andromeda sessions search <text>
andromeda --resume <id>             # pick one back up
andromeda --continue                # pick up the most recent
```

Resuming replays the transcript verbatim, including its original system message
— rewriting it would change the rules the earlier turns were produced under.

## Output

Answers are rendered as markdown in the terminal — headings, **bold**, real
tables, syntax-highlighted code — streaming into a live region so structure
forms as it arrives rather than reflowing at the end.

Numbers worth comparing get a chart. The model emits:

````
```chart
registry.py: 642
browser.py: 459
web.py: 276
```
````

and the terminal draws horizontal bars in eighth-block characters, so values
within a few percent of each other still look different — whole blocks alone
make everything in the same 1/32nd of the range render identically, and a chart
where different numbers look the same is worse than a list.

**A tty is not a pipe.** Redirected output is plain text with no escape codes,
so `andromeda "..." > notes.md` gives you markdown rather than a screenshot of
markdown.

## The full-screen interface

```bash
andromeda --tui                    # this session
andromeda config set interface tui # from now on
andromeda --no-tui                 # back to the REPL for one run
```

A transcript that scrolls on its own, an activity lane that shows what is
running right now without pushing the answer off the screen, a status line with
the model, the approval mode and the context gauge, and prompts that take over
the screen instead of being printed into a stream that is still moving.

| Key | |
|---|---|
| `enter` | send — or queue, if a turn is already running |
| `ctrl+c` | interrupt the turn, then clear the draft — it never quits |
| `ctrl+d` | leave |
| `ctrl+l` | new conversation |
| `shift+enter` | new line (`alt+enter` / `ctrl+j` also work) |
| `ctrl+g` | write the prompt in `$EDITOR` |
| paste | every line lands in the field |
| `pgup` / `pgdn` | scroll the transcript |
| `↑` / `↓` | walk back through what you have asked |

The slash commands are the REPL's, unchanged — a test fails the build if the
two lists drift apart.

**Ctrl-C never quits**, matching the REPL — it interrupts a turn, then clears
the draft, and Ctrl-D is the way out. A terminal is not a private channel:
shell integrations clear the current line before typing into it, and a stray
`^C` from one of those should not end a session and everything in it.

**Why a session ended** is appended to `~/.andromeda-cli/tui.log`. A full-screen
app takes the terminal with it when it goes, so anything it printed on the way
out is wiped by the screen restore; a line on disk survives that.

**The composer is a real multi-line editor.** Paste as much as you like; every
line lands in the field. `enter` sends, `shift+enter` starts a new line
(`alt+enter` and `ctrl+j` also work, for terminals that cannot tell shift+enter
apart). The field grows with the text to ten lines and then scrolls, so a long
prompt never pushes the conversation off the screen.

`ctrl+g` opens `$EDITOR` for anything longer, and what you save fills the field.

It draws the same things the REPL does, because it runs the same renderer: the
markdown, the chart bars, the palette and the context meter are one module used
from two surfaces rather than two implementations that agree for a while.

**It is not the default**, and turning it on is deliberate. It takes over the
terminal and clears it on exit, which is the right trade when you are working
in it and the wrong one when you wanted three lines of output. `--tui` needs a
terminal on *both* stdin and stdout and refuses otherwise — a full-screen app
writes cursor moves, and a pipe must stay plain text. Passing `--tui` with a
prompt is refused rather than ignored, for the same reason `--resume` is.

**Prompts stop the agent.** When a tool needs approval, the turn is genuinely
blocked — the thread is parked, the composer is disabled, and the activity lane
says who is being waited on. Escape, `ctrl+c` and quitting the app all answer
*no*: walking away from the keyboard is not consent.

## Input you did not type

Editors and shell integrations type into new terminals — VS Code's Python
extension writes `source .../.venv/bin/activate` into any terminal where it
finds a venv. If that lands while Andromeda is starting, it would otherwise be
read as your first prompt, sent to the model, and come back as a command to
approve.

Two layers, because the injection can arrive either before or after the prompt
is drawn:

- Anything already in the terminal's buffer when a session starts is discarded.
- For the first few seconds after that, a line longer than a few characters that
  arrives **faster than it could be typed** — measured from its first character
  to Enter, not from when the prompt appeared — is ignored.

Both say what they ignored, and the line is still in your history. Type-ahead
and a paste in the first moments are lost with it; running a command you never
typed is the worse outcome.

**The permanent cure is at the source.** In VS Code, set
`"python.terminal.activateEnvironment": false` — the extension is what types
`source .../activate` into your terminals.

## Rewinding

A checkpoint is taken before every prompt, so rewinding lands where you were when
you asked — not after the answer you want to discard.

```
/history      the checkpoints you can go back to
/rewind       undo the last exchange
/rewind 3     go back to a numbered one
```

Rewinding discards the checkpoints taken after it. Keeping them would let a
second rewind jump *forward* into a transcript that no longer describes what
happened.

Checkpoints are saved with the session, so `--resume` brings the ability to
rewind back with it — the run you most want to undo is often the one you came
back to the next morning.

## Being asked, and not being asked

The agent can ask you a question mid-run with `clarify` rather than guessing —
a wrong assumption five steps in costs the whole run. In a pipe it refuses and
says so, because a default there is exactly the guess the tool replaces.

Approvals accumulate. At the prompt, `!` means "stop asking about this tool"; after
five plain approvals it offers.

```bash
andromeda approvals                 # what you have stopped being asked about
andromeda approvals forget <tool>
andromeda approvals clear
```

**A learned entry is bound to the tier it was granted at.** Trust `terminal`
while it is `destructive` and it stays trusted at `destructive` — if a tool's
tier ever rises, the entry stops applying and the gate is back. A permission
granted for one thing must not silently cover a more dangerous version of it.

Learned trust never widens itself: counts only drive a suggestion, promotion is
always your explicit answer. It cannot reopen a ceiling, a belt, or a disabled
tool, an explicit config override beats it, and it does not descend into a
delegated lane.

## Moving to another machine

```bash
andromeda backup out.tar.gz     # everything, INCLUDING the device token
andromeda export out.tar.gz     # everything except credentials
andromeda restore in.tar.gz     # --force to overwrite existing state
```

Two verbs rather than one flag, because they differ in exactly one way and that
way matters: a `backup` is for moving your own install and should be treated
like a password; an `export` is safe to share or commit. Collapsing them is how a
file that quietly holds a live credential ends up in a git repo.

Restore drops any archive member that would land outside your home, and any
symlink — extracting an archive someone handed you is precisely when that
matters.

## Scheduled jobs

The agent, without you. `andromeda cron` schedules work; `andromeda cron
install` makes something actually run it.

```bash
andromeda cron add "every 1h" "check the build and tell me if it broke"
andromeda cron add "0 9 * * 1-5" "summarise yesterday's commits" --deliver notify
andromeda cron add "in 2h" "check whether the deploy finished"     # once, then retires
andromeda cron list | show <id> | logs <id> | run <id> | rm <id>
andromeda cron install        # runs at login, restarts if it dies
andromeda cron service        # is it actually running?
andromeda cron daemon         # or in this terminal
```

`install` writes a launchd agent or a systemd user unit and hands the process
over — the daemon does not fork, because both supervisors want to own what they
supervise.

**It snapshots your `PATH`.** A user agent starts with roughly
`/usr/bin:/bin`, so without this a job calling `gh`, `rg` or anything from
Homebrew works when you run it by hand and fails when the scheduler runs it.
The snapshot is taken from the shell you install from, so install from a normal
login shell and re-run `install` after you change your `PATH`. `cron service`
prints how many entries were captured and says when they differ from your
current shell.

Exactly five variables travel — `PATH`, `LANG`, `ANDROMEDA_HOME`,
`ANDROMEDA_PROVIDER`, `OPENROUTER_API_KEY` — and if the last one is set the
command says so out loud, because a service file is a new copy of a credential.
The file is written `0600`.

**Consent is established at creation and stated in full.** A job carries the
approval mode it was created with, and that mode beats whatever the machine is
set to — one created read-only stays read-only even if you later set
`approval_mode: auto` for your own sessions. A corrupt or unrecognised mode
reads as `ask`, the narrow one: a damaged field must never widen what a job may
do.

**Every run is recorded, success or failure**, with its full output in
`~/.andromeda-cli/cron/output/<job>/`. A scheduler that only logs failures
leaves you unable to tell "ran and found nothing" from "never ran".

### Three ways to spend nothing

An agent turn is the most expensive part of the loop, so most of this section
is about not taking one.

**Watch something.** A cheap source runs first each tick and its output is
hashed. Unchanged means the agent does not run at all.

```bash
andromeda cron add "every 10m" "Something changed on the status page — say what." \
  --watch-url https://example.com/status
andromeda cron add "every 5m" "The queue depth changed — is it a problem?" \
  --watch queue-depth.sh
```

Comparison is **exact bytes** — no timestamp stripping, no whitespace
normalisation. Normalising means guessing which differences are meaningful, and
a guess that is wrong in the quiet direction is a monitor that never fires. Emit
stable output from your source. A source that *fails* is an error, never a
change, and the stored hash is left alone so a source that recovers to its
previous output still suppresses.

**Skip the agent entirely.** A classic watchdog needs no model at all.

```bash
andromeda cron add "every 10m" --no-agent --script disk-check.sh
```

The script is the job and its stdout is the report. **Empty stdout is silence** —
a watchdog that reports every time it finds nothing is a watchdog people mute.

**Let something else gather the facts.**

```bash
andromeda cron add "every 1h" "Anything here worth waking me for?" --script collect.sh
andromeda cron add "every 1h" "Turn the collector's findings into one paragraph." --after job_ab12
```

`--script` puts a script's output into the prompt; `--after` puts another job's
latest output into it, so one job can collect and another can reason.

Scripts live in `~/.andromeda-cli/scripts/` and are named, never pathed. A job
spec is data — it can be written by the agent — and a path in data that can
point anywhere is arbitrary code execution on a timer. `.sh`, `.bash` and `.py`;
an unknown extension is refused rather than fed to a guessed interpreter.

### What a job remembers

Every run starts fresh, which is what makes a job reproducible. The one thing
that carries over is a small notepad — for the cursor a polling job needs.

```bash
andromeda cron notepad <id>                    # what it is holding
andromeda cron notepad <id> set cursor 12345
andromeda cron notepad <id> clear
```

The job writes it through a `notepad` tool bound to that job. It is tiered
`safe_local` on purpose: a job in the default `ask` mode is narrowed to
read-only precisely because nobody is watching it, and a job that can be trusted
to remember its own cursor but not to run commands is the common case. It is
capped at 16KB a note and 64KB a job, because this text is prepended to every
prompt that job ever sends.

### Automations on offer

Andromeda proposes automations. It never creates one.

```bash
andromeda cron suggest              # what is on offer, and where it came from
andromeda cron suggest accept 1     # create it
andromeda cron suggest dismiss 1    # never offer it again
```

Four sources: a curated `catalog`, a `blueprint` shipped by a skill you
installed, `usage` — something you have asked for by hand three times, in your
own words, with a recurrence in it — and `integration`, an automation that
became possible because a capability appeared.

A dismissal **latches**: nothing is ever re-offered after you say no. The
backlog is capped at five, because a list of twenty things to decide about is a
list nobody opens. And a stored proposal is inert data — it is validated only
when you accept it, by the same `Schedule.add` every other path uses, so a
suggestion cannot smuggle in an unattended job by being written to disk.

Installing a skill that ships a `blueprint:` block registers a *suggestion*,
never a job. A skill you installed should not be able to schedule work on your
machine without being asked.

### Automations as a form

Nobody should have to type cron.

```bash
andromeda cron blueprint                  # what is available
andromeda cron blueprint show watch-url   # its fields
andromeda cron blueprint use watch-url url=https://example.com/status interval_min=30
```

A blueprint carries a fixed recurrence and parameterises only the parts you
have an opinion about — a time, a weekday set, an interval. An unknown field
name is **refused**, not ignored: a typo'd `tiem=07:15` that silently creates a
job at the default time works, is wrong, and says nothing.

### The agent can schedule its own follow-ups

Ask for something recurring and it will use the `cron` tool.

**An agent may propose autonomy. Only a person grants the unattended kind.**
This is the approval gate's "a child is never more permissive than its parent",
applied to time instead of to depth: a job the agent creates is read-only, the
tool has no argument that could ask for anything else, and widening is a
separate command a person types after reading what the job will do.

```bash
andromeda cron approve <id> --approval auto    # shows the prompt first
```

The agent cannot *run* a job either — that would be an agent turn nested inside
an agent turn with nothing supervising the pair. And a job whose prompt or
script contains a command that stops or restarts the scheduler is refused at
creation: under a supervisor that is a respawn loop, not a restart.

### Saying nothing

A job with nothing worth waking you for replies `[SILENT]` and delivers
nothing. The run is still recorded in full — this suppresses the message, never
the record. A job that reports "nothing to report" every hour trains you to
ignore it, and then it is worse than not existing.

### What was in flight when the machine went down

```bash
andromeda cron executions              # every attempt
andromeda cron executions --unresolved # the ones nobody recorded the end of
```

The run history says what a job produced. This says what was *attempted* —
including attempts a killed scheduler never finished. An attempt is written
before anything with a side effect and closed after, so "never ran" and "ran,
did the thing, and died before recording it" stop being the same empty history.

Abandoned attempts become `unknown`, never retried. `unknown` is the honest
state: the side effects may or may not have happened, which is the only thing
anyone can actually know. Recovery only marks an attempt abandoned when its
owner is *proved* gone — pid **and** process start time, because pids are
reused and calling a live attempt dead is how you get two copies.

### A job's own settings

```bash
andromeda cron add "0 6 * * *" "..." --thinking high --tools read_file,terminal \
  --skill triage --deliver webhook --deliver-to https://hooks.example/andromeda \
  --attach <session-id>
```

A job is not the session that created it. `--tools` can only ever **subtract** —
a job cannot name a tool you switched off and get it back. `--attach` appends
each run to a session, so a scheduled follow-up shows up in `--resume` beside
the conversation that asked for it.

### When things go wrong

- **Five failures in a row and a job stops trying.** A job whose credentials
  expired fails identically forever; the failure is already recorded the first
  time. `andromeda cron resume <id>` when it is fixed.
- **A missed run fires once, not once per interval slept through.** A laptop
  shut over six hourly ticks produces one run, and the history says how late it
  was.
- **One scheduler at a time**, enforced with a lock. Two daemons fire every job
  twice, and the second copy of a job that writes files is not a duplicate
  report, it is a second edit.
- **A tick is a heartbeat.** `andromeda cron list` and `andromeda doctor` both
  say when the scheduler last ran, because "3 jobs scheduled" reads as working
  and is exactly as true when nothing has ticked for a week.

## Evaluations

Unit tests answer "does this function do what I wrote". Evals answer "does the
agent still do the right thing" — which depends on a model that changes
underneath you, a prompt you edit, and a tool description you reword. None of
those break a unit test.

```bash
andromeda eval          # run them against the live model
andromeda eval list
andromeda eval --json   # machine-readable, for CI
```

Scenarios live in [`evals/`](../evals) as YAML: a workspace, a prompt, and
checks written against **observable outcomes** — files that exist, tools that
were called, text that appears. Never against the model's exact words, because
asserting on phrasing gives you a suite that fails on a synonym and passes on a
lie.

They cost money, which is the point: a mocked eval measures the mock.

## Configuration

`~/.andromeda-cli/config.yaml`. Defaults < file < `ANDROMEDA_*` env < flags.

Not `~/.andromeda` — that is the desktop app's data directory (its sqlite
stores, vault key and browser profiles), and the installer's checkout lands
there too.

```bash
andromeda config get              # all settings
andromeda config set model a/b
andromeda config path
```

Secrets live in a separate `credentials.json` and are never printed by any
command — which is why `config get` can dump everything without redaction.

`ANDROMEDA_HOME` relocates both files.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the run failed (not signed in, network, upstream) |
| 2 | usage error |
| 3 | out of credit |
| 130 | interrupted |

## Layout

```
andromeda_cli/     commands, REPL, config, session assembly, output, rendering
andromeda_agent/   the turn loop, the approval gate, provider lanes, errors,
                   and the autonomy layer: schedule, runner, monitor, notepad,
                   scripts, delivery
andromeda_tools/   the registry, executors, skills, memory, web, browser, MCP
andromeda_tui/     the full-screen surface: events, driver, widgets, prompts
tests/
install/
```

`andromeda_tui/` runs the agent in-process on a worker thread rather than
speaking a protocol to it. Splitting a TUI out over a JSON-RPC gateway pays off
when the same tree also renders a desktop app and a web dashboard; one client
does not pay for that. What it keeps is the seam — `events.py` is a
serialisable event vocabulary and blocking questions are answered by request id
— so a gateway is a later addition rather than a rewrite.

## Tests

```bash
.venv/bin/python -m pytest
```

## Status

M4 complete, plus compaction, concurrent lanes, a rendered terminal surface,
MCP, background processes, rewind, `clarify`, learned approvals, vision,
thinking-level control, an evals harness, the full-screen interface, and the
autonomy layer — monitored jobs, script jobs, chaining, notepads, self-
scheduling and a supervised scheduler. See
[`docs/andromeda-cli-plan.md`](../docs/andromeda-cli-plan.md) for what is next.
