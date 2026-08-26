"""The credit balance, read off inference response headers.

The balance rides on the response of a real call rather than a separate billing
query, so it is always as of something that actually happened and there is no
polling interval to choose for a number that only moves when you spend.

Three rules are carried from the server side, and each one is a way of being
wrong that costs a user something:

**Depletion is the `access` header, never `remaining == 0`.** A window that has
just rolled reads zero for a moment while access is fine. Inferring depletion
from the number tells someone their account is empty when it is not, and the
report that follows looks exactly like a real outage.

**A missing header means unknown, not depleted.** Fail open. An older
deployment, or a proxy that strips headers it does not recognise, must degrade
to showing no balance rather than to refusing to work. `depleted` has to be
said out loud to be believed.

**Money is integer micros until the moment it is displayed.** Floats lose cents
at these magnitudes, and a formatted string invites parsing it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

PREFIX = "x-andromeda-credits-"

HEADER_ACCESS = f"{PREFIX}access"
HEADER_REMAINING = f"{PREFIX}remaining-micros"
HEADER_GRANT = f"{PREFIX}grant-micros"
HEADER_ADJUSTMENT = f"{PREFIX}adjustment-micros"
HEADER_USED = f"{PREFIX}used-micros"
HEADER_WINDOW_ENDS = f"{PREFIX}window-ends"

MICROS_PER_DOLLAR = 1_000_000


def _int(headers: Mapping[str, Any], name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        # `int` and not `float`: a fractional value here is a server bug, and
        # silently truncating it would hide it. Refuse the field instead.
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Balance:
    """What a response said about the account, in integer micros."""

    # None means the server did not say. Distinct from 0, which means empty.
    remaining_micros: int | None = None
    grant_micros: int | None = None
    adjustment_micros: int | None = None
    used_micros: int | None = None
    window_ends_at: int | None = None
    # "active", "depleted", or "" when the server did not say.
    access: str = ""

    @property
    def known(self) -> bool:
        return self.remaining_micros is not None

    @property
    def depleted(self) -> bool:
        """Only ever the server's word for it.

        Deliberately not `remaining_micros == 0`. That expression is the bug
        this property exists to make unavailable.
        """
        return self.access == "depleted"

    @property
    def total_micros(self) -> int | None:
        """The denominator for a gauge: the grant plus any adjustment.

        Summed only here, at the point of display, and never stored summed.
        Once purchased credit exists it lands in the adjustment, and a gauge
        drawn against the grant alone would show someone holding a top-up as
        fully used.
        """
        if self.grant_micros is None:
            return None
        return self.grant_micros + (self.adjustment_micros or 0)

    def renews_on(self) -> str:
        if not self.window_ends_at:
            return ""
        try:
            when = datetime.fromtimestamp(self.window_ends_at / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ""
        return when.astimezone().strftime("%b %-d")


def parse(headers: Mapping[str, Any] | None) -> Balance:
    """Read a balance out of response headers.

    Headers absent entirely gives an unknown balance, which every caller is
    expected to render as nothing at all rather than as zero.
    """
    if not headers:
        return Balance()

    # Header lookup is case-insensitive on real responses, but a plain dict in
    # a test is not, so normalise rather than depend on the transport's type.
    lowered = {str(key).lower(): value for key, value in headers.items()}

    access = str(lowered.get(HEADER_ACCESS) or "").strip().lower()
    if access not in {"active", "depleted"}:
        # An unrecognised value is not a third state. Treat it as unsaid, which
        # fails open rather than inventing a meaning for it.
        access = ""

    return Balance(
        remaining_micros=_int(lowered, HEADER_REMAINING),
        grant_micros=_int(lowered, HEADER_GRANT),
        adjustment_micros=_int(lowered, HEADER_ADJUSTMENT),
        used_micros=_int(lowered, HEADER_USED),
        window_ends_at=_int(lowered, HEADER_WINDOW_ENDS),
        access=access,
    )


# The decimal places a dollar figure may be shown at, coarsest first. Two is
# what money normally looks like; the rest exist because a small grant spent in
# fractions of a cent is still being spent, and a display that cannot show that
# reports a balance as frozen while it moves.
PRECISIONS = (2, 4, 6)


def format_micros(micros: int | None, decimals: int | None = None) -> str:
    """Micros to a dollar string, at the display edge and nowhere else.

    `decimals` is chosen for you when it is not given: two places for anything
    a cent can express, four below that. Pass it explicitly to render several
    figures at one precision — see `format_pair`, which is where the rule that
    actually matters lives.
    """
    if micros is None:
        return ""
    if decimals is None:
        decimals = 2 if abs(micros) >= 10_000 else 4
    negative = micros < 0
    dollars = abs(micros) / MICROS_PER_DOLLAR
    text = f"${dollars:,.{decimals}f}"
    return f"-{text}" if negative else text


def format_pair(left: int | None, right: int | None) -> tuple[str, str]:
    """Two figures at the coarsest precision that still tells them apart.

    This exists because of a real report: a `$0.10` grant with three hundredths
    of a cent spent renders as "$0.10 of $0.10" at two decimal places, and reads
    as a balance that is not moving while the account is actively being spent.
    Money is normally shown to the cent, so the fix is not to show six places
    always — it is to add places only when two places would hide the
    difference.

    Figures that are genuinely equal stay at two places, because a window that
    has just renewed should say "$0.10 of $0.10" and mean it.
    """
    if left is None or right is None:
        return format_micros(left), format_micros(right)
    for decimals in PRECISIONS:
        rendered = (
            format_micros(left, decimals),
            format_micros(right, decimals),
        )
        if left == right or rendered[0] != rendered[1]:
            return rendered
    return rendered


def summary(balance: Balance) -> str:
    """One line for a status bar. Empty when there is nothing honest to say."""
    if not balance.known:
        return ""

    total = balance.total_micros
    if total is None:
        line = f"{format_micros(balance.remaining_micros)} left"
    else:
        remaining, whole = format_pair(balance.remaining_micros, total)
        line = f"{remaining} of {whole}"

    renews = balance.renews_on()
    if renews:
        line += f" · renews {renews}"
    if balance.depleted:
        line += " · depleted"
    return line
