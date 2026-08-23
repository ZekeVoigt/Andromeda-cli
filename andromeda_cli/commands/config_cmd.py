"""Reading and writing the non-secret config."""

from __future__ import annotations

from .. import config as config_module
from .. import output


def show(key: str | None) -> int:
    values = config_module.load()
    if key is None:
        width = max(len(name) for name in values)
        for name in sorted(values):
            output.console.print(f"  [cyan]{name.ljust(width)}[/cyan]  {values[name]}")
        output.info(f"\n  {config_module.config_path()}")
        return 0

    if key not in values:
        output.fail(
            f"Unknown setting {key!r}.",
            f"Known: {', '.join(sorted(values))}",
        )
        return 2
    output.console.print(values[key], soft_wrap=True)
    return 0


def set_value(key: str, raw: str) -> int:
    try:
        value = config_module.set_value(key, raw)
    except config_module.ConfigError as exc:
        output.fail(str(exc))
        return 2
    output.ok(f"{key} = {value}")
    return 0


def where() -> int:
    output.console.print(str(config_module.home()), soft_wrap=True)
    return 0
