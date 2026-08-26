"""Reading a skill before the agent does.

A skill is instructions that go into the model's context and a directory of
files it may open. Both halves are an attack surface, and the realistic version
here is not a malicious registry — this harness has no registry. It is a
`skills/` directory that arrived with a repository somebody cloned. Nobody
reads those, and the agent reads them every session.

So each skill is scanned before it is offered, and where it came from decides
how much it has to prove:

  **builtin**    ships with this install. Never scanned.
  **trusted**    in your own `~/.andromeda-cli/skills`. You put it there.
  **community**  found in a workspace. Somebody else may have.

The scan is regex over text, plus structural checks, plus a hunt for invisible
characters — the three things static analysis can actually do. It is not a
sandbox and does not pretend to be one: it raises the cost of the easy attacks
and reports what it found, in a form a person can read and disagree with.

Two rules are load-bearing:

  A blocked skill is never silently dropped. It is listed, with the finding
  that blocked it, because a capability that vanishes without explanation is
  one people work around by turning the whole feature off.

  Trust is bound to content. `skills trust` records the skill's hash, so an
  edit to a trusted skill is untrusted again — approval was for the text that
  was read, not for the name.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCANNER_VERSION = "skill-scan-v1"

# Severity that decides the verdict, and the order findings are shown in.
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

VERDICTS = ("safe", "caution", "dangerous")
VERDICT_INDEX = {name: index for index, name in enumerate(VERDICTS)}

# What each source is allowed to do at each verdict.
#                          safe      caution    dangerous
POLICY: dict[str, tuple[str, str, str]] = {
    "builtin": ("allow", "allow", "allow"),
    "trusted": ("allow", "allow", "block"),
    "community": ("allow", "block", "block"),
}


# ---------------------------------------------------------------------------
# The patterns
# ---------------------------------------------------------------------------

# (regex, id, severity, category, description)
#
# Severity is what the verdict reads: any `critical` makes a skill dangerous,
# any `high` makes it caution, and medium/low are informational — they appear
# in a report and never block anything on their own. That split is deliberate:
# a scanner whose medium findings block is a scanner people disable.
THREAT_PATTERNS: list[tuple[str, str, str, str, str]] = [
    # -- getting secrets out -------------------------------------------------
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)",
     "env_exfil_curl", "critical", "exfiltration",
     "curl with a secret environment variable in it"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)",
     "env_exfil_wget", "critical", "exfiltration",
     "wget with a secret environment variable in it"),
    (r"fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)",
     "env_exfil_fetch", "critical", "exfiltration",
     "fetch() with a secret environment variable in it"),
    (r"requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)",
     "env_exfil_requests", "critical", "exfiltration",
     "an HTTP call carrying a secret"),
    (r"httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)",
     "env_exfil_httpx", "critical", "exfiltration",
     "an HTTP call carrying a secret"),
    (r"base64[^\n]*env",
     "encoded_exfil", "high", "exfiltration",
     "base64 together with environment access"),
    (r"\$HOME/\.ssh|~/\.ssh",
     "ssh_dir_access", "high", "exfiltration", "reads the SSH directory"),
    (r"\$HOME/\.aws|~/\.aws",
     "aws_dir_access", "high", "exfiltration", "reads AWS credentials"),
    (r"\$HOME/\.gnupg|~/\.gnupg",
     "gpg_dir_access", "high", "exfiltration", "reads the GPG keyring"),
    (r"\$HOME/\.kube|~/\.kube",
     "kube_dir_access", "high", "exfiltration", "reads the Kubernetes config"),
    (r"\$HOME/\.docker|~/\.docker",
     "docker_dir_access", "high", "exfiltration",
     "reads the Docker config, which may hold registry credentials"),
    (r"\.andromeda-cli/credentials|~/\.andromeda-cli/\.env",
     "andromeda_credentials", "critical", "exfiltration",
     "names this program's own credentials file"),
    # `cat file` reads; `cat > file` writes. A setup doc telling you to write
    # your own keys into your own .env is the opposite of exfiltration.
    (r"cat\s+(?!>)[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)",
     "read_secrets_file", "critical", "exfiltration", "reads a secrets file"),
    (r"printenv|env\s*\|",
     "dump_all_env", "high", "exfiltration", "dumps every environment variable"),
    # A bare `os.environ` dump is suspicious; `os.environ.get("SOME_CONFIG")`
    # is a config read that sends nothing. The lookahead exempts the second
    # form unless the name it asks for is secret-shaped.
    (r"os\.environ\b(?!\s*\.get\s*\(\s*[\"'](?![^\"']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)))",
     "python_os_environ", "high", "exfiltration", "reads os.environ wholesale"),
    (r"os\.environ\s*\.get\s*\(\s*[\"'][^\"']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
     "python_environ_get_secret", "critical", "exfiltration",
     "reads a secret out of the environment"),
    (r"os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
     "python_getenv_secret", "critical", "exfiltration",
     "reads a secret out of the environment"),
    (r"process\.env\[",
     "node_process_env", "high", "exfiltration", "reads the Node environment"),
    (r"ENV\[.*(?:KEY|TOKEN|SECRET|PASSWORD)",
     "ruby_env_secret", "critical", "exfiltration",
     "reads a secret out of the Ruby environment"),
    # Not a flag like `--host 127.0.0.1`.
    (r"(?<![-/])\b(dig|nslookup|host)\s+[^\n]*\$",
     "dns_exfil", "critical", "exfiltration",
     "a DNS lookup built from a variable, which is how data leaves quietly"),
    (r">\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)",
     "tmp_staging", "critical", "exfiltration", "writes to /tmp then sends it"),
    (r"!\[.*\]\(https?://[^\)]*\$\{?",
     "md_image_exfil", "high", "exfiltration",
     "a markdown image whose URL is built from a variable"),
    (r"\[.*\]\(https?://[^\)]*\$\{?",
     "md_link_exfil", "high", "exfiltration",
     "a markdown link whose URL is built from a variable"),
    (r"(include|output|print|send|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|context)",
     "context_exfil", "high", "exfiltration",
     "asks the agent to hand over the conversation"),
    (r"(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://",
     "send_to_url", "high", "exfiltration", "asks the agent to send data to a URL"),

    # -- talking to the model rather than to you -----------------------------
    (r"ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions",
     "prompt_injection_ignore", "critical", "injection",
     "tells the agent to ignore its instructions"),
    (r"disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)",
     "disregard_rules", "critical", "injection",
     "tells the agent to disregard its rules"),
    (r"do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user",
     "deception_hide", "critical", "injection",
     "tells the agent to hide something from you"),
    (r"system\s+(?:\w+\s+)*prompt\s+(?:\w+\s+)*override",
     "sys_prompt_override", "critical", "injection",
     "tries to overwrite the system prompt"),
    (r"you\s+are\s+(?:\w+\s+)*now\s+",
     "role_hijack", "high", "injection", "tries to reassign the agent's role"),
    (r"pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+",
     "role_pretend", "high", "injection", "asks the agent to be something else"),
    (r"output\s+(?:\w+\s+)*(system|initial)\s+prompt",
     "leak_system_prompt", "high", "injection",
     "asks the agent to print its system prompt"),
    (r"(when|if)\s+no\s*one\s+is\s+(watching|looking)",
     "conditional_deception", "high", "injection",
     "asks the agent to behave differently when unobserved"),
    (r"act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don't\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)",
     "bypass_restrictions", "critical", "injection",
     "asks the agent to act without restrictions"),
    (r"(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)",
     "remove_filters", "critical", "injection",
     "asks the agent to answer without its safety rules"),
    (r"translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)",
     "translate_execute", "critical", "injection",
     "translate-then-run, a way around a check on the original text"),
    (r"<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->",
     "html_comment_injection", "high", "injection",
     "instructions hidden in an HTML comment"),
    (r"<\s*div\s+style\s*=\s*[\"'][\s\S]*?display\s*:\s*none",
     "hidden_div", "high", "injection", "an invisible HTML block"),
    (r"\bDAN\s+mode\b|Do\s+Anything\s+Now",
     "jailbreak_dan", "critical", "injection", "a known jailbreak formula"),
    (r"\bdeveloper\s+mode\b.*\benabled?\b",
     "jailbreak_dev_mode", "critical", "injection", "a known jailbreak formula"),
    (r"hypothetical\s+scenario.*(?:ignore|bypass|override)",
     "hypothetical_bypass", "high", "injection",
     "a hypothetical framing used to get around a rule"),
    (r"you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to",
     "fake_update", "high", "injection", "a fake announcement of new behaviour"),
    (r"for\s+educational\s+purposes?\s+only",
     "educational_pretext", "medium", "injection", "an educational pretext"),
    (r"new\s+(?:\w+\s+)*policy|updated\s+(?:\w+\s+)*guidelines|revised\s+(?:\w+\s+)*instructions",
     "fake_policy", "medium", "injection", "claims a new policy"),

    # -- destroying things ---------------------------------------------------
    (r"rm\s+-rf\s+/",
     "destructive_root_rm", "critical", "destructive", "deletes from the root"),
    (r"rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME",
     "destructive_home_rm", "critical", "destructive",
     "deletes the home directory"),
    (r">\s*/etc/",
     "system_overwrite", "critical", "destructive", "overwrites a system file"),
    (r"\bmkfs\b",
     "format_filesystem", "critical", "destructive", "formats a filesystem"),
    (r"\bdd\s+.*if=.*of=/dev/",
     "disk_overwrite", "critical", "destructive", "writes to a raw device"),
    (r"truncate\s+-s\s*0\s+/",
     "truncate_system", "critical", "destructive", "empties a system file"),
    (r"shutil\.rmtree\s*\(\s*[\"'/]",
     "python_rmtree", "high", "destructive",
     "removes a tree at an absolute path"),
    (r"chmod\s+777",
     "insecure_perms", "medium", "destructive", "makes something world-writable"),

    # -- staying ------------------------------------------------------------
    (r"authorized_keys",
     "ssh_backdoor", "critical", "persistence", "touches SSH authorized keys"),
    (r"/etc/sudoers|visudo",
     "sudoers_mod", "critical", "persistence", "edits sudoers"),
    (r"AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules",
     "agent_config_mod", "critical", "persistence",
     "writes an agent instruction file, which would outlive this session"),
    (r"\.andromeda-cli/config\.yaml|\.andromeda-cli/SOUL\.md",
     "andromeda_config_mod", "critical", "persistence",
     "writes this program's own configuration"),
    (r"\.claude/settings|\.codex/config",
     "other_agent_config", "high", "persistence",
     "writes another agent's configuration"),
    (r"\bcrontab\b",
     "persistence_cron", "medium", "persistence", "edits cron jobs"),
    (r"\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b",
     "shell_rc_mod", "medium", "persistence", "names a shell startup file"),
    (r"launchctl\s+load|LaunchAgents|LaunchDaemons",
     "macos_launchd", "medium", "persistence", "installs a launch agent"),
    (r"systemd.*\.service|systemctl\s+(enable|start)",
     "systemd_service", "medium", "persistence", "installs a service"),
    (r"/etc/init\.d/",
     "init_script", "medium", "persistence", "names an init script"),
    (r"ssh-keygen",
     "ssh_keygen", "medium", "persistence", "generates SSH keys"),
    (r"git\s+config\s+--global\s+",
     "git_config_global", "medium", "persistence", "changes global git config"),

    # -- opening a door ------------------------------------------------------
    (r"\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b",
     "reverse_shell", "critical", "network", "opens a listener"),
    (r"/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/",
     "bash_reverse_shell", "critical", "network", "a reverse shell"),
    (r"python[23]?\s+-c\s+[\"']import\s+socket",
     "python_socket_oneliner", "critical", "network",
     "a one-line socket connection"),
    (r"socket\.connect\s*\(\s*\(",
     "python_socket_connect", "high", "network", "connects a raw socket"),
    (r"0\.0\.0\.0:\d+|INADDR_ANY",
     "bind_all_interfaces", "high", "network", "listens on every interface"),
    (r"\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b",
     "tunnel_service", "high", "network", "opens a tunnel to the outside"),
    (r"webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com",
     "exfil_service", "high", "network",
     "names a service people use to collect exfiltrated data"),
    (r"pastebin\.com|hastebin\.com|ghostbin\.",
     "paste_service", "medium", "network", "names a paste service"),
    (r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}",
     "hardcoded_ip_port", "medium", "network", "a hardcoded address and port"),

    # -- hiding what it does -------------------------------------------------
    (r"echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)",
     "echo_pipe_exec", "critical", "obfuscation",
     "pipes text straight into an interpreter"),
    (r"base64\s+(-d|--decode)\s*\|",
     "base64_decode_pipe", "high", "obfuscation", "decodes and then runs"),
    (r"\beval\s*\(\s*[\"']",
     "eval_string", "high", "obfuscation", "eval() on a string"),
    (r"\bexec\s*\(\s*[\"']",
     "exec_string", "high", "obfuscation", "exec() on a string"),
    (r"compile\s*\(\s*[^\)]+,\s*[\"'].*[\"']\s*,\s*[\"']exec[\"']\s*\)",
     "python_compile_exec", "high", "obfuscation", "compiles code to run"),
    (r"getattr\s*\(\s*__builtins__",
     "python_getattr_builtins", "high", "obfuscation",
     "reaches builtins by name, which is how a check gets stepped around"),
    (r"__import__\s*\(\s*[\"']os[\"']\s*\)",
     "python_import_os", "high", "obfuscation", "imports os by name"),
    (r"chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+",
     "chr_building", "high", "obfuscation", "builds a string out of chr()"),
    (r"\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}",
     "hex_encoded_string", "medium", "obfuscation", "a hex-encoded string"),
    (r"\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}",
     "unicode_escape_chain", "medium", "obfuscation", "a chain of unicode escapes"),
    (r"codecs\.decode\s*\(\s*[\"']",
     "python_codecs_decode", "medium", "obfuscation", "decodes an encoded string"),
    (r"String\.fromCharCode|charCodeAt",
     "js_char_code", "medium", "obfuscation", "builds a string from char codes"),
    (r"atob\s*\(|btoa\s*\(",
     "js_base64", "medium", "obfuscation", "base64 in JavaScript"),
    (r"\[::-1\]",
     "string_reversal", "low", "obfuscation", "reverses a string"),

    # -- running things ------------------------------------------------------
    (r"os\.system\s*\(",
     "python_os_system", "high", "execution", "os.system(), an unguarded shell"),
    (r"os\.popen\s*\(",
     "python_os_popen", "high", "execution", "os.popen(), an unguarded shell"),
    (r"child_process\.(exec|spawn|fork)\s*\(",
     "node_child_process", "high", "execution", "runs a command from Node"),
    (r"Runtime\.getRuntime\(\)\.exec\(",
     "java_runtime_exec", "high", "execution", "runs a command from Java"),
    (r"subprocess\.(run|call|Popen|check_output)\s*\(",
     "python_subprocess", "medium", "execution", "runs a subprocess"),
    (r"`[^`]*\$\([^)]+\)[^`]*`",
     "backtick_subshell", "medium", "execution", "a command substitution"),

    # -- going up and out ----------------------------------------------------
    (r"/etc/passwd|/etc/shadow",
     "system_passwd_access", "critical", "traversal", "names the password files"),
    (r"\.\./\.\./\.\.",
     "path_traversal_deep", "high", "traversal", "climbs three or more levels up"),
    (r"/proc/self|/proc/\d+/",
     "proc_access", "high", "traversal", "reads /proc"),
    (r"\.\./\.\.",
     "path_traversal", "medium", "traversal", "climbs two levels up"),
    (r"/dev/shm/",
     "dev_shm", "medium", "traversal", "uses shared memory as a staging area"),

    # -- becoming root -------------------------------------------------------
    (r"setuid|setgid|cap_setuid",
     "setuid_setgid", "critical", "privilege", "sets a privilege bit"),
    (r"NOPASSWD",
     "nopasswd_sudo", "critical", "privilege", "a passwordless sudo rule"),
    (r"chmod\s+[u+]?s",
     "suid_bit", "critical", "privilege", "sets the SUID bit"),
    (r"\bsudo\b",
     "sudo_usage", "high", "privilege", "uses sudo"),

    # -- code arriving at runtime -------------------------------------------
    (r"curl\s+[^\n]*\|\s*(ba)?sh",
     "curl_pipe_shell", "critical", "supply_chain",
     "downloads and runs in one step"),
    (r"wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh",
     "wget_pipe_shell", "critical", "supply_chain",
     "downloads and runs in one step"),
    (r"curl\s+[^\n]*\|\s*python",
     "curl_pipe_python", "critical", "supply_chain",
     "downloads and runs in one step"),
    (r"(curl|wget|httpx?\.get|requests\.get|fetch)\s*[\(]?\s*[\"']https?://",
     "remote_fetch", "medium", "supply_chain", "fetches something at runtime"),
    (r"git\s+clone\s+",
     "git_clone", "medium", "supply_chain", "clones a repository at runtime"),
    (r"docker\s+pull\s+",
     "docker_pull", "medium", "supply_chain", "pulls an image at runtime"),
    (r"pip\s+install\s+(?!-r\s)(?!.*==)",
     "unpinned_pip_install", "medium", "supply_chain",
     "installs a package without pinning a version"),
    (r"npm\s+install\s+(?!.*@\d)",
     "unpinned_npm_install", "medium", "supply_chain",
     "installs a package without pinning a version"),

    # -- mining on somebody else's machine ----------------------------------
    (r"xmrig|stratum\+tcp|monero|coinhive|cryptonight",
     "crypto_mining", "critical", "mining", "a cryptocurrency miner"),
    (r"hashrate|nonce.*difficulty",
     "mining_indicators", "medium", "mining", "words that go with mining"),

    # -- credentials left in the skill itself -------------------------------
    (r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
     "embedded_private_key", "critical", "credential", "an embedded private key"),
    (r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}",
     "github_token_leaked", "critical", "credential", "a GitHub token"),
    (r"sk-ant-[A-Za-z0-9_-]{90,}",
     "anthropic_key_leaked", "critical", "credential", "an Anthropic key"),
    (r"sk-[A-Za-z0-9]{20,}",
     "openai_key_leaked", "critical", "credential", "an OpenAI-shaped key"),
    (r"AKIA[0-9A-Z]{16}",
     "aws_access_key_leaked", "critical", "credential", "an AWS access key"),
    (r"glpat-[A-Za-z0-9_\-]{20,}",
     "gitlab_token_leaked", "critical", "credential", "a GitLab token"),
    (r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}",
     "hardcoded_secret", "critical", "credential", "something shaped like a key"),
]

COMPILED = [
    (re.compile(pattern, re.IGNORECASE), pid, severity, category, description)
    for pattern, pid, severity, category, description in THREAT_PATTERNS
]

# Structural limits. A skill is instructions, not a program.
MAX_FILE_COUNT = 50
MAX_TOTAL_SIZE_KB = 1024
MAX_SINGLE_FILE_KB = 256

SCANNABLE_EXTENSIONS = frozenset(
    {
        ".md", ".txt", ".py", ".sh", ".bash", ".js", ".ts", ".rb",
        ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".conf",
        ".html", ".css", ".xml", ".tex", ".r", ".jl", ".pl", ".php",
    }
)

BINARY_EXTENSIONS = frozenset(
    {".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".com", ".msi", ".dmg",
     ".app", ".deb", ".rpm"}
)

SCRIPT_EXTENSIONS = frozenset({".sh", ".bash", ".py", ".rb", ".pl"})

# Characters that occupy no space on screen. A person reviewing a skill sees
# one text; the model reads another.
INVISIBLE_CHARS: dict[str, str] = {
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "⁢": "invisible times",
    "⁣": "invisible separator",
    "⁤": "invisible plus",
    "﻿": "byte-order mark",
    "‪": "left-to-right embedding",
    "‫": "right-to-left embedding",
    "‬": "pop directional formatting",
    "‭": "left-to-right override",
    "‮": "right-to-left override",
    "⁦": "left-to-right isolate",
    "⁧": "right-to-left isolate",
    "⁨": "first strong isolate",
    "⁩": "pop directional isolate",
}

IGNORE_FILENAMES = (".skillignore",)
NEVER_IGNORABLE = frozenset({"SKILL.md"})


@dataclass
class Finding:
    pattern_id: str
    severity: str
    category: str
    file: str
    line: int
    match: str
    description: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "match": self.match,
            "description": self.description,
        }


@dataclass
class ScanResult:
    name: str
    trust: str
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    content_hash: str = ""
    scanned_at: str = ""

    @property
    def allowed(self) -> bool:
        return decide(self) == "allow"

    @property
    def worst(self) -> Finding | None:
        """The finding that decided the verdict."""
        if not self.findings:
            return None
        return sorted(
            self.findings, key=lambda item: SEVERITY_ORDER.get(item.severity, 4)
        )[0]

    def summary(self) -> str:
        if not self.findings:
            return "nothing found"
        categories = sorted({finding.category for finding in self.findings})
        count = len(self.findings)
        plural = "" if count == 1 else "s"
        return f"{self.verdict} — {count} finding{plural} in {', '.join(categories)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trust": self.trust,
            "verdict": self.verdict,
            "content_hash": self.content_hash,
            "scanned_at": self.scanned_at,
            "findings": [finding.as_dict() for finding in self.findings],
            "scanner_version": SCANNER_VERSION,
        }


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_text(text: str, rel_path: str) -> list[Finding]:
    """Every pattern against every line, plus the invisible characters.

    Deduplicated per (pattern, line): one line matching one pattern is one
    finding, however many times it matches within the line.
    """
    findings: list[Finding] = []
    lines = text.split("\n")
    seen: set[tuple[str, int]] = set()

    for pattern, pid, severity, category, description in COMPILED:
        for number, line in enumerate(lines, start=1):
            if (pid, number) in seen:
                continue
            if not pattern.search(line):
                continue
            seen.add((pid, number))
            excerpt = line.strip()
            if len(excerpt) > 120:
                excerpt = excerpt[:117] + "..."
            findings.append(
                Finding(pid, severity, category, rel_path, number, excerpt, description)
            )

    for number, line in enumerate(lines, start=1):
        for char, name in INVISIBLE_CHARS.items():
            if char not in line:
                continue
            findings.append(
                Finding(
                    "invisible_unicode",
                    "high",
                    "injection",
                    rel_path,
                    number,
                    f"U+{ord(char):04X} ({name})",
                    f"an invisible character ({name}) — what you read is not "
                    f"what the model reads",
                )
            )
            break  # one per line is enough to say "look here"

    return findings


def scan_file(path: Path, rel_path: str = "") -> list[Finding]:
    rel_path = rel_path or path.name
    if path.suffix.lower() not in SCANNABLE_EXTENSIONS and path.name != "SKILL.md":
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_text(text, rel_path)


def scan_directory(directory: Path, name: str = "", trust: str = "community") -> ScanResult:
    """Scan every file in a skill directory."""
    name = name or directory.name
    findings: list[Finding] = []

    if directory.is_dir():
        ignore = load_ignore(directory)
        findings.extend(check_structure(directory, ignore))
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(directory).as_posix()
            if ignore(rel):
                continue
            findings.extend(scan_file(path, rel))
    elif directory.is_file():
        findings.extend(scan_file(directory, directory.name))

    return ScanResult(
        name=name,
        trust=trust,
        verdict=verdict_for(findings),
        findings=findings,
        content_hash=content_hash(directory),
        scanned_at=datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def check_structure(directory: Path, ignore: Callable[[str], bool]) -> list[Finding]:
    """Shape rather than content: size, count, binaries, escaping symlinks."""
    findings: list[Finding] = []
    count = 0
    total = 0

    for path in sorted(directory.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = path.relative_to(directory).as_posix()
        if ignore(rel):
            continue
        count += 1

        if path.is_symlink():
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(directory.resolve()):
                    findings.append(
                        Finding(
                            "symlink_escape", "critical", "traversal", rel, 0,
                            f"-> {resolved}",
                            "a symlink pointing out of the skill directory",
                        )
                    )
                elif not resolved.exists():
                    # Asked directly rather than caught: `resolve()` does not
                    # raise for a missing target on any Python this runs on, so
                    # a try/except here would be a branch that never fires.
                    findings.append(
                        Finding(
                            "broken_symlink", "medium", "traversal", rel, 0,
                            "broken symlink", "a link to something that is not there",
                        )
                    )
            except OSError:
                findings.append(
                    Finding(
                        "broken_symlink", "medium", "traversal", rel, 0,
                        "unreadable symlink", "a symlink that could not be read",
                    )
                )
            continue

        try:
            size = path.stat().st_size
            mode = path.stat().st_mode
        except OSError:
            continue
        total += size

        if size > MAX_SINGLE_FILE_KB * 1024:
            findings.append(
                Finding(
                    "oversized_file", "medium", "structural", rel, 0,
                    f"{size // 1024}KB",
                    f"one file of {size // 1024}KB (limit {MAX_SINGLE_FILE_KB}KB)",
                )
            )

        suffix = path.suffix.lower()
        if suffix in BINARY_EXTENSIONS:
            findings.append(
                Finding(
                    "binary_file", "critical", "structural", rel, 0,
                    f"binary: {suffix}",
                    "a binary in something that should be instructions",
                )
            )
        elif suffix not in SCRIPT_EXTENSIONS and mode & 0o111:
            findings.append(
                Finding(
                    "unexpected_executable", "medium", "structural", rel, 0,
                    "executable bit set",
                    "an executable file that is not a recognised script",
                )
            )

    if count > MAX_FILE_COUNT:
        findings.append(
            Finding(
                "too_many_files", "medium", "structural", "(directory)", 0,
                f"{count} files", f"{count} files (limit {MAX_FILE_COUNT})",
            )
        )
    if total > MAX_TOTAL_SIZE_KB * 1024:
        findings.append(
            Finding(
                "oversized_skill", "high", "structural", "(directory)", 0,
                f"{total // 1024}KB",
                f"{total // 1024}KB in total (limit {MAX_TOTAL_SIZE_KB}KB)",
            )
        )

    return findings


def load_ignore(directory: Path) -> Callable[[str], bool]:
    """A `.skillignore`, so development leftovers do not read as threats.

    `SKILL.md` can never be ignored — it is the file the model actually reads,
    and a skill that could exclude it from the scan would be a skill that
    excludes itself.
    """
    patterns: list[str] = []
    for name in IGNORE_FILENAMES:
        path = directory / name
        try:
            if path.is_file():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        except (OSError, UnicodeDecodeError):
            continue

    def ignore(rel: str) -> bool:
        rel_posix = Path(rel).as_posix()
        base = rel_posix.split("/")[-1]

        if base in NEVER_IGNORABLE:
            return False
        if base in IGNORE_FILENAMES:
            return True

        for raw in patterns:
            anchored = raw.startswith("/")
            pattern = raw.lstrip("/")
            directory_pattern = pattern.endswith("/")
            pattern = pattern.rstrip("/")
            if not pattern:
                continue

            if directory_pattern:
                if rel_posix == pattern or rel_posix.startswith(pattern + "/"):
                    return True
                if not anchored and f"/{pattern}/" in f"/{rel_posix}/":
                    return True
                continue

            if fnmatch.fnmatch(rel_posix, pattern):
                return True
            if anchored:
                continue
            if fnmatch.fnmatch(base, pattern):
                return True
            if "/" not in pattern and any(
                fnmatch.fnmatch(segment, pattern) for segment in rel_posix.split("/")
            ):
                return True
            if rel_posix.startswith(pattern + "/"):
                return True
        return False

    return ignore


def verdict_for(findings: Iterable[Finding]) -> str:
    """One critical is dangerous; one high is caution; the rest is noise.

    Medium and low never decide anything on their own — `subprocess.run` and
    `git clone` are ordinary things for a skill to describe, and a scanner
    that blocks on them is a scanner people turn off.
    """
    severities = {finding.severity for finding in findings}
    if "critical" in severities:
        return "dangerous"
    if "high" in severities:
        return "caution"
    return "safe"


def content_hash(path: Path) -> str:
    """A digest over relative paths and bytes.

    Paths are mixed in and sorted as POSIX strings, so swapping the contents of
    two files changes the hash and the answer does not depend on the operating
    system's idea of sort order.
    """
    digest = hashlib.sha256()
    if path.is_dir():
        entries = sorted(
            (item.relative_to(path).as_posix(), item)
            for item in path.rglob("*")
            if item.is_file()
        )
        for rel, item in entries:
            digest.update(rel.encode("utf-8") + b"\x00")
            try:
                digest.update(item.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
    elif path.is_file():
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()[:32]}"


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------


def trust_for(skill_path: Path, home: Path, bundled: Path | None) -> str:
    """Where a skill lives is the only provenance this harness has.

    There is no registry and no signature, so the question "who put this here"
    is answered by which directory it is in. A skill inside a workspace came
    with whatever was cloned into that workspace, and is treated accordingly.
    """
    resolved = skill_path.resolve()

    if bundled is not None:
        try:
            bundled_root = bundled.resolve()
            if resolved == bundled_root or bundled_root in resolved.parents:
                return "builtin"
        except OSError:
            pass

    try:
        home_skills = (home / "skills").resolve()
        if resolved == home_skills or home_skills in resolved.parents:
            return "trusted"
    except OSError:
        pass

    return "community"


def decide(result: ScanResult) -> str:
    """"allow" or "block", from the trust level and the verdict.

    `trusted-by-you` is not a source — it is a person's recorded decision about
    one exact version of one skill, so it allows everything. It is set only by
    `screen`, only against a matching content hash.
    """
    if result.trust == "trusted-by-you":
        return "allow"
    policy = POLICY.get(result.trust, POLICY["community"])
    return policy[VERDICT_INDEX.get(result.verdict, 2)]


def refusal(result: ScanResult) -> str:
    """Why a skill is not on offer, in one line a person can act on."""
    worst = result.worst
    detail = ""
    if worst is not None:
        detail = f" — {worst.description} ({worst.file}:{worst.line})"
    return (
        f"{result.name} was not loaded: {result.verdict} scan of a "
        f"{result.trust} skill{detail}"
    )


# ---------------------------------------------------------------------------
# What you decided about a skill, and remembering the scan
# ---------------------------------------------------------------------------

TRUST_FILENAME = "skill-trust.json"
CACHE_FILENAME = "skill-scan-cache.json"


def _read_json(path: Path, empty: dict[str, Any]) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(empty)
    return loaded if isinstance(loaded, dict) else dict(empty)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        # Losing the file costs a re-decision, never a wrong decision: an
        # approval that did not persist reads as "not approved".
        pass


def trust_path(home: Path) -> Path:
    return home / TRUST_FILENAME


def approvals(home: Path) -> list[dict[str, Any]]:
    entries = _read_json(trust_path(home), {"approved": []}).get("approved")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def approve(home: Path, result: ScanResult, path: Path) -> None:
    """Record that a person read this skill and accepted it.

    Bound to the content hash, so editing the skill withdraws the approval —
    what was accepted is the text that was read, not the name it goes by.
    """
    entry = {
        "name": result.name,
        "content_hash": result.content_hash,
        "path": str(path),
        "verdict": result.verdict,
        "approved_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    kept = [
        item
        for item in approvals(home)
        if not (item.get("name") == result.name and item.get("path") == str(path))
    ]
    _write_json(trust_path(home), {"approved": [*kept, entry]})


def withdraw(home: Path, name: str) -> int:
    """Drop every approval for a name. Returns how many went."""
    existing = approvals(home)
    kept = [item for item in existing if item.get("name") != name]
    _write_json(trust_path(home), {"approved": kept})
    return len(existing) - len(kept)


def approved_entry(home: Path, name: str, content_hash_value: str) -> dict[str, Any] | None:
    for entry in approvals(home):
        if entry.get("name") == name and entry.get("content_hash") == content_hash_value:
            return entry
    return None


def cache_path(home: Path) -> Path:
    return home / CACHE_FILENAME


def cached_scan(home: Path, name: str, content_hash_value: str) -> ScanResult | None:
    """A previous scan of exactly this content.

    Keyed on the hash, so a changed skill never reads a stale verdict — which
    is the only way a cache here could be worse than no cache.
    """
    entry = _read_json(cache_path(home), {}).get(content_hash_value)
    if not isinstance(entry, dict):
        return None
    if entry.get("scanner_version") != SCANNER_VERSION or entry.get("name") != name:
        return None
    try:
        findings = [Finding(**item) for item in entry.get("findings", [])]
    except TypeError:
        return None
    return ScanResult(
        name=name,
        trust=str(entry.get("trust", "community")),
        verdict=str(entry.get("verdict", "safe")),
        findings=findings,
        content_hash=content_hash_value,
        scanned_at=str(entry.get("scanned_at", "")),
    )


def remember_scan(home: Path, result: ScanResult, limit: int = 200) -> None:
    data = _read_json(cache_path(home), {})
    data[result.content_hash] = result.as_dict()
    if len(data) > limit:
        # Oldest first. An unbounded cache of every skill anyone ever cloned is
        # a file that only grows.
        ordered = sorted(
            data.items(), key=lambda item: str(item[1].get("scanned_at", ""))
        )
        data = dict(ordered[-limit:])
    _write_json(cache_path(home), data)


def scan_skill(
    path: Path, home: Path, bundled: Path | None = None, *, use_cache: bool = True
) -> ScanResult:
    """Scan one skill directory, with its trust level and any recorded approval."""
    trust = trust_for(path, home, bundled)
    name = path.name

    if trust == "builtin":
        # What shipped with the program is the program. Scanning it would be
        # this install auditing itself, and a finding would be unactionable.
        return ScanResult(
            name=name,
            trust=trust,
            verdict="safe",
            content_hash=content_hash(path),
            scanned_at=datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    digest = content_hash(path)
    result = cached_scan(home, name, digest) if use_cache else None
    if result is None:
        result = scan_directory(path, name=name, trust=trust)
        if use_cache:
            remember_scan(home, result)
    result.trust = trust
    return result


def screen(
    skills: dict[str, Any], home: Path, bundled: Path | None = None
) -> dict[str, ScanResult]:
    """Scan every discovered skill. Returns one result per skill, by name.

    Nothing is filtered here — the caller decides what to do with a block,
    because the REPL wants to *list* what was withheld and the prompt builder
    wants to leave it out. A function that quietly returned fewer skills than
    it was given would make the second easy and the first impossible.
    """
    results: dict[str, ScanResult] = {}
    for name, skill in skills.items():
        directory = Path(skill.path).parent
        result = scan_skill(directory, home, bundled)
        if not result.allowed and approved_entry(home, name, result.content_hash):
            # A person read this one and said yes. Recorded against the hash,
            # so an edit puts it back behind the gate.
            result.trust = "trusted-by-you"
        results[name] = result
    return results


def is_allowed(result: ScanResult) -> bool:
    return result.trust == "trusted-by-you" or result.allowed
