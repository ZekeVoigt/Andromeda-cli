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

**Nothing here ever renders a currency.** The wire is micros of USD because
that is what the meter charges in, and the screen is Andromeda Credits because
that is what the person bought. Those are not the same unit and the conversion
is deliberately one-way: a plan's dollar ceiling is a commercial fact about
margin, and putting it on a status bar publishes it to every user. `to_credits`
is the only bridge, and it points outward only.
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

#: USD micros in one Andromeda Credit. Mirrors `USD_MICROS_PER_CREDIT` in
#: `shared/billing/catalog.ts`, which is the definition; this is a copy because
#: the CLI does not import the web app's TypeScript. A test pins them together.
MICROS_PER_CREDIT = 1_000


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


# The decimal places a credit figure may be shown at, coarsest first.
#
# Whole credits are the normal case and the one people reason in. The finer
# steps exist because a credit is a thousand micros, so a cheap turn moves the
# balance by a fraction of one — and a display that could only show whole
# credits would report a balance as frozen while it is actively being spent.
# Three is the floor because it is exactly one micro; asking for more would
# render precision the wire does not carry.
PRECISIONS = (0, 1, 3)


def to_credits(micros: int | None) -> float | None:
    """USD micros to Andromeda Credits. The only bridge between the units.

    One-way on purpose. Credits are what a person holds and what a plan is sold
    in; the dollar figure underneath is a fact about margin, and no surface
    that a user can see is allowed to derive it back.
    """
    if micros is None:
        return None
    return micros / MICROS_PER_CREDIT


def format_credits(micros: int | None, decimals: int | None = None) -> str:
    """Micros to a credit string, at the display edge and nowhere else.

    `decimals` is chosen for you when it is not given: whole credits once there
    is at least one to show, finer below that so a small balance still visibly
    moves. Pass it explicitly to render several figures at one precision — see
    `format_pair`, which is where the rule that actually matters lives.

    No unit suffix. The caller decides whether this figure sits beside the word
    "credits" or inside a phrase that already said it, and baking it in here
    produced "1,000 credits of 12,000 credits".
    """
    if micros is None:
        return ""
    if decimals is None:
        decimals = 0 if abs(micros) >= MICROS_PER_CREDIT else 3
    negative = micros < 0
    credits = abs(micros) / MICROS_PER_CREDIT
    text = f"{credits:,.{decimals}f}"
    return f"-{text}" if negative else text


def format_pair(left: int | None, right: int | None) -> tuple[str, str]:
    """Two figures at the coarsest precision that still tells them apart.

    This exists because of a real report: a 100-credit grant with three
    thousandths of a credit spent renders as "100 of 100" at whole-credit
    precision, and reads as a balance that is not moving while the account is
    actively being spent. Credits are normally whole, so the fix is not to show
    three places always — it is to add places only when whole ones would hide
    the difference.

    Figures that are genuinely equal stay whole, because a window that has just
    renewed should say "12,000 of 12,000" and mean it.
    """
    if left is None or right is None:
        return format_credits(left), format_credits(right)
    for decimals in PRECISIONS:
        rendered = (
            format_credits(left, decimals),
            format_credits(right, decimals),
        )
        if left == right or rendered[0] != rendered[1]:
            return rendered
    return rendered


def summary(balance: Balance) -> str:
    """One line for a status bar. Empty when there is nothing honest to say.

    The denominator is whatever the server said this window's grant is, never a
    number this file knows — which is what makes it adapt on its own the moment
    a plan changes, with no release and no local cache to go stale.
    """
    if not balance.known:
        return ""

    total = balance.total_micros
    if total is None:
        line = f"{format_credits(balance.remaining_micros)} credits left"
    else:
        remaining, whole = format_pair(balance.remaining_micros, total)
        line = f"{remaining} of {whole} credits"

    renews = balance.renews_on()
    if renews:
        line += f" · renews {renews}"
    if balance.depleted:
        line += " · depleted"
    return line
