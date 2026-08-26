"""Shell completion, generated from the parser rather than written by hand.

The verb list, the subcommands and the flags are read off the live argparse
tree at the moment the script is generated. A hand-maintained list would be
wrong the first time somebody adds a command and nobody notices, because the
symptom is a tab that does nothing — which reads as "completion is not
installed", not as "completion is out of date".

bash, zsh and fish. No dependency, and nothing to keep in step.
"""

from __future__ import annotations

import argparse
from typing import Any

SHELLS = ("bash", "zsh", "fish")

# Where a profile name is the natural completion after the verb.
PROFILE_ACTIONS = ("use", "delete")


def walk(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Subcommands and flags, recursively.

    `_choices_actions` is read rather than `choices`, because it holds one
    entry per canonical name — aliases are left out, which keeps a completion
    list from showing the same command three times.
    """
    flags: list[str] = []
    subcommands: dict[str, Any] = {}

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            seen: set[str] = set()
            for pseudo in action._choices_actions:
                name = pseudo.dest
                if name in seen:
                    continue
                seen.add(name)
                child = action.choices.get(name)
                if child is None:
                    continue
                info = walk(child)
                info["help"] = clean(pseudo.help or "")
                subcommands[name] = info
        elif action.option_strings:
            flags.extend(
                option for option in action.option_strings if option.startswith("-")
            )

    return {"flags": flags, "subcommands": subcommands}


def clean(text: str, limit: int = 60) -> str:
    """Help text, made safe to sit inside a shell string.

    Quotes and backslashes are removed rather than escaped: three shells with
    three escaping rules, and a description is not worth a quoting bug in
    somebody's shell startup.
    """
    return text.replace("'", "").replace('"', "").replace("\\", "")[:limit]


def generate(shell: str, parser: argparse.ArgumentParser) -> str:
    if shell == "bash":
        return bash(parser)
    if shell == "zsh":
        return zsh(parser)
    if shell == "fish":
        return fish(parser)
    raise ValueError(f"no completion for {shell!r}; try {', '.join(SHELLS)}")


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


def bash(parser: argparse.ArgumentParser) -> str:
    tree = walk(parser)
    verbs = " ".join(sorted(tree["subcommands"]))

    cases: list[str] = []
    for verb in sorted(tree["subcommands"]):
        info = tree["subcommands"][verb]
        if verb == "profile" and info["subcommands"]:
            actions = "|".join(PROFILE_ACTIONS)
            subcommands = " ".join(sorted(info["subcommands"]))
            cases.append(
                f"        profile)\n"
                f'            case "$prev" in\n'
                f"                profile)\n"
                f'                    COMPREPLY=($(compgen -W "{subcommands}" -- "$cur"))\n'
                f"                    return\n"
                f"                    ;;\n"
                f"                {actions})\n"
                f'                    COMPREPLY=($(compgen -W "$(_andromeda_profiles)" -- "$cur"))\n'
                f"                    return\n"
                f"                    ;;\n"
                f"            esac\n"
                f"            ;;"
            )
        elif info["subcommands"]:
            subcommands = " ".join(sorted(info["subcommands"]))
            cases.append(
                f"        {verb})\n"
                f'            COMPREPLY=($(compgen -W "{subcommands}" -- "$cur"))\n'
                f"            return\n"
                f"            ;;"
            )
        elif info["flags"]:
            flags = " ".join(sorted(set(info["flags"])))
            cases.append(
                f"        {verb})\n"
                f'            COMPREPLY=($(compgen -W "{flags}" -- "$cur"))\n'
                f"            return\n"
                f"            ;;"
            )

    body = "\n".join(cases)

    return f"""# Andromeda completion for bash.
# Add to ~/.bashrc:
#   eval "$(andromeda completion bash)"

_andromeda_profiles() {{
    local dir="$HOME/.andromeda-cli/profiles"
    local names="default"
    if [ -d "$dir" ]; then
        for entry in "$dir"/*/; do
            [ -d "$entry" ] && names="$names $(basename "$entry")"
        done
    fi
    echo "$names"
}}

_andromeda_completion() {{
    local cur prev
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"

    if [[ "$prev" == "-p" || "$prev" == "--profile" ]]; then
        COMPREPLY=($(compgen -W "$(_andromeda_profiles)" -- "$cur"))
        return
    fi

    if [[ $COMP_CWORD -ge 2 ]]; then
        case "${{COMP_WORDS[1]}}" in
{body}
        esac
    fi

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "{verbs}" -- "$cur"))
    fi
}}

complete -F _andromeda_completion andromeda
"""


# ---------------------------------------------------------------------------
# zsh
# ---------------------------------------------------------------------------


def zsh(parser: argparse.ArgumentParser) -> str:
    tree = walk(parser)

    verbs = "\n".join(
        f"                '{verb}:{clean(tree['subcommands'][verb].get('help', ''))}'"
        for verb in sorted(tree["subcommands"])
    )

    cases: list[str] = []
    for verb in sorted(tree["subcommands"]):
        info = tree["subcommands"][verb]
        if not info["subcommands"]:
            continue
        described = "\n".join(
            f"                        '{name}:{clean(info['subcommands'][name].get('help', ''))}'"
            for name in sorted(info["subcommands"])
        )
        if verb == "profile":
            cases.append(
                f"                profile)\n"
                f"                    case ${{line[2]}} in\n"
                f"                        {'|'.join(PROFILE_ACTIONS)})\n"
                f"                            _andromeda_profiles\n"
                f"                            ;;\n"
                f"                        *)\n"
                f"                            local -a profile_cmds\n"
                f"                            profile_cmds=(\n"
                f"{described}\n"
                f"                            )\n"
                f"                            _describe 'profile command' profile_cmds\n"
                f"                            ;;\n"
                f"                    esac\n"
                f"                    ;;"
            )
        else:
            safe = verb.replace("-", "_")
            listed = "\n".join(
                f"                    '{name}:{clean(info['subcommands'][name].get('help', ''))}'"
                for name in sorted(info["subcommands"])
            )
            cases.append(
                f"                {verb})\n"
                f"                    local -a {safe}_cmds\n"
                f"                    {safe}_cmds=(\n"
                f"{listed}\n"
                f"                    )\n"
                f"                    _describe '{verb} command' {safe}_cmds\n"
                f"                    ;;"
            )

    body = "\n".join(cases)

    return f"""#compdef andromeda
# Andromeda completion for zsh.
# Add to ~/.zshrc:
#   eval "$(andromeda completion zsh)"

_andromeda_profiles() {{
    local -a names
    names=(default)
    if [[ -d "$HOME/.andromeda-cli/profiles" ]]; then
        names+=($HOME/.andromeda-cli/profiles/*(N/:t))
    fi
    _describe 'profile' names
}}

_andromeda() {{
    local context state line
    typeset -A opt_args

    _arguments -C \\
        '(-)'{{-h,--help}}'[Show help and exit]' \\
        '(-)--version[Show the version and exit]' \\
        '(-)'{{-p,--profile}}'[Profile name]:profile:_andromeda_profiles' \\
        '1:command:->commands' \\
        '*::arg:->args'

    case $state in
        commands)
            local -a verbs
            verbs=(
{verbs}
            )
            _describe 'andromeda command' verbs
            ;;
        args)
            case ${{line[1]}} in
{body}
            esac
            ;;
    esac
}}

compdef _andromeda andromeda
"""


# ---------------------------------------------------------------------------
# fish
# ---------------------------------------------------------------------------


def fish(parser: argparse.ArgumentParser) -> str:
    tree = walk(parser)
    verbs = sorted(tree["subcommands"])
    joined = " ".join(verbs)

    lines = [
        "# Andromeda completion for fish.",
        "# Add to your config:",
        "#   andromeda completion fish | source",
        "",
        "function __andromeda_profiles",
        "    echo default",
        "    if test -d $HOME/.andromeda-cli/profiles",
        "        for entry in $HOME/.andromeda-cli/profiles/*/",
        "            basename $entry",
        "        end",
        "    end",
        "end",
        "",
        "# No file completion unless something asks for it.",
        "complete -c andromeda -f",
        "",
        "complete -c andromeda -f -s p -l profile -d 'Profile name'"
        " -xa '(__andromeda_profiles)'",
        "",
        "# Verbs",
    ]

    for verb in verbs:
        info = tree["subcommands"][verb]
        lines.append(
            f"complete -c andromeda -f "
            f"-n 'not __fish_seen_subcommand_from {joined}' "
            f"-a {verb} -d '{clean(info.get('help', ''))}'"
        )

    lines += ["", "# Their subcommands"]

    for verb in verbs:
        info = tree["subcommands"][verb]
        if not info["subcommands"]:
            continue
        lines.append(f"# {verb}")
        for name in sorted(info["subcommands"]):
            help_text = clean(info["subcommands"][name].get("help", ""))
            lines.append(
                f"complete -c andromeda -f "
                f"-n '__fish_seen_subcommand_from {verb}' "
                f"-a {name} -d '{help_text}'"
            )
        if verb == "profile":
            for action in PROFILE_ACTIONS:
                lines.append(
                    f"complete -c andromeda -f "
                    f"-n '__fish_seen_subcommand_from {action}; "
                    f"and __fish_seen_subcommand_from profile' "
                    f"-a '(__andromeda_profiles)' -d 'Profile name'"
                )

    lines.append("")
    return "\n".join(lines)
