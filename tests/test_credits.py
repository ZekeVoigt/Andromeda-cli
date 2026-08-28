"""Reading a balance off response headers.

Most of what is pinned here is a way of being wrong that costs a user
something, rather than a happy path. The expensive mistakes are all the same
shape: treating "the server did not say" as "the answer is zero".
"""

from __future__ import annotations

import pytest

from andromeda_agent import credits
from andromeda_agent.providers.base import Provider


def headers(**overrides) -> dict[str, str]:
    """A full set of headers, with short names for the fields under test.

    Overrides are given as `remaining_micros=...` and expanded to the real
    header name here. Doing that expansion wrong is how the first version of
    this file passed eight tests that were asserting nothing.
    """
    base = {
        "x-andromeda-credits-access": "active",
        "x-andromeda-credits-remaining-micros": "7500000",
        "x-andromeda-credits-grant-micros": "10000000",
        "x-andromeda-credits-adjustment-micros": "0",
        "x-andromeda-credits-used-micros": "2500000",
        "x-andromeda-credits-window-ends": "1756944000000",
    }
    for key, value in overrides.items():
        name = f"x-andromeda-credits-{key.replace('_', '-')}"
        assert name in base, f"unknown header override: {key}"
        base[name] = value
    return base


class TestParsing:
    def test_reads_a_full_balance(self):
        balance = credits.parse(headers())
        assert balance.known
        assert balance.remaining_micros == 7_500_000
        assert balance.grant_micros == 10_000_000
        assert balance.used_micros == 2_500_000
        assert balance.access == "active"

    def test_header_case_does_not_matter(self):
        balance = credits.parse({"X-Andromeda-Credits-Remaining-Micros": "42"})
        assert balance.remaining_micros == 42

    def test_no_headers_is_unknown_rather_than_empty(self):
        balance = credits.parse(None)
        assert not balance.known
        assert balance.remaining_micros is None
        assert not balance.depleted

    def test_a_malformed_number_is_dropped_not_coerced(self):
        # A header reading `NaN` or `1.5` is a server bug. Parsing it into
        # something confidently wrong is worse than showing nothing.
        for bad in ("NaN", "1.5", "", "lots"):
            balance = credits.parse(headers(remaining_micros=bad))
            assert balance.remaining_micros is None

    def test_an_unrecognised_access_value_is_treated_as_unsaid(self):
        # Not a third state. Anything but the two known values fails open.
        balance = credits.parse(headers(access="suspended"))
        assert balance.access == ""
        assert not balance.depleted


class TestDepletion:
    """The rule that exists because the obvious implementation is wrong."""

    def test_zero_remaining_is_not_depletion(self):
        # A window that has just rolled, or a plan mid-renewal, reads zero for
        # a moment while access is fine. Inferring depletion here tells someone
        # their account is empty when it is not.
        balance = credits.parse(headers(access="active", remaining_micros="0"))
        assert balance.remaining_micros == 0
        assert not balance.depleted

    def test_depletion_is_the_server_saying_so(self):
        balance = credits.parse(headers(access="depleted", remaining_micros="0"))
        assert balance.depleted

    def test_a_missing_access_header_never_means_depleted(self):
        # Fail open: an older deployment or a proxy stripping headers must cost
        # the read-out, not the session.
        raw = headers()
        del raw["x-andromeda-credits-access"]
        assert not credits.parse(raw).depleted

    def test_depletion_can_be_reported_with_credit_still_showing(self):
        # The two are independent signals. The server is the authority on
        # whether spending is allowed, whatever the number says.
        balance = credits.parse(headers(access="depleted", remaining_micros="500000"))
        assert balance.depleted


class TestTheDenominator:
    def test_a_top_up_is_not_lost_from_the_total(self):
        # Once purchased credit exists it lands in the adjustment. A gauge
        # drawn against the grant alone shows someone holding a top-up as
        # fully used.
        balance = credits.parse(
            headers(grant_micros="10000000", adjustment_micros="5000000")
        )
        assert balance.total_micros == 15_000_000

    def test_a_negative_adjustment_is_respected(self):
        balance = credits.parse(
            headers(grant_micros="10000000", adjustment_micros="-2000000")
        )
        assert balance.total_micros == 8_000_000

    def test_no_grant_means_no_denominator_rather_than_zero(self):
        raw = headers()
        del raw["x-andromeda-credits-grant-micros"]
        assert credits.parse(raw).total_micros is None


class TestFormatting:
    def test_micros_become_credits_only_at_the_edge(self):
        # 7,500,000 micros is $7.50 on the wire and 7,500 credits on screen.
        # The dollar figure never appears; see the module docstring.
        assert credits.format_credits(7_500_000) == "7,500"
        assert credits.format_credits(0) == "0.000"

    def test_a_sub_credit_balance_does_not_render_as_empty(self):
        # Rounding 0.4 of a credit to "0" reads as nothing left.
        assert credits.format_credits(400) == "0.400"

    def test_unknown_formats_to_nothing(self):
        assert credits.format_credits(None) == ""

    def test_a_negative_balance_keeps_its_sign(self):
        assert credits.format_credits(-1_500_000) == "-1,500"

    def test_no_surface_renders_a_currency(self):
        """The commercial reason this module exists in this shape.

        A plan's dollar ceiling is a fact about margin. Every figure a person
        can see is in credits, and there is no code path back.
        """
        rendered = [
            credits.format_credits(7_500_000),
            credits.format_credits(400),
            credits.format_credits(-1_500_000),
            credits.summary(
                credits.parse(headers())
            ),
        ]
        for text in rendered:
            assert "$" not in text
        assert not hasattr(credits, "format_micros")

    def test_the_credit_rate_matches_the_billing_catalogue(self):
        """`USD_MICROS_PER_CREDIT` in `shared/billing/catalog.ts` is the
        definition; `MICROS_PER_CREDIT` here is a copy, and a copy that drifts
        would misprice every figure on the screen.
        """
        from pathlib import Path
        import re

        catalog = (
            Path(__file__).resolve().parents[2]
            / "shared"
            / "billing"
            / "catalog.ts"
        )
        if not catalog.exists():
            pytest.skip("the web app is not in this checkout")
        found = re.search(
            r"USD_MICROS_PER_CREDIT\s*=\s*([0-9_]+)", catalog.read_text()
        )
        assert found is not None
        assert int(found.group(1).replace("_", "")) == credits.MICROS_PER_CREDIT


class TestSummary:
    def test_says_nothing_when_the_balance_is_unknown(self):
        # The BYOK lane never sets these headers: there is no account here to
        # have a balance, and inventing "0 credits" would be a lie.
        assert credits.summary(credits.Balance()) == ""

    def test_reads_as_a_status_line(self):
        line = credits.summary(credits.parse(headers()))
        assert "7,500 of 10,000 credits" in line

    def test_the_denominator_comes_from_the_server_not_from_here(self):
        """What makes it adapt without a release.

        The grant is whatever this window says it is. Nothing in the CLI knows
        a plan's size, so an upgrade is visible on the very next reply and
        there is no local cache to go stale — which is exactly how a status bar
        ends up insisting on an old plan's ceiling.
        """
        small = credits.summary(
            credits.Balance(remaining_micros=100_000, grant_micros=100_000)
        )
        large = credits.summary(
            credits.Balance(remaining_micros=12_000_000, grant_micros=12_000_000)
        )

        assert "100 of 100 credits" in small
        assert "12,000 of 12,000 credits" in large

    def test_depletion_is_said_out_loud(self):
        line = credits.summary(credits.parse(headers(access="depleted")))
        assert "depleted" in line


class TestTheProvider:
    """The balance has to survive the trip through the SDK seam."""

    def _provider(self, response_headers, *, raw: bool = True) -> Provider:
        class Raw:
            headers = response_headers

            @staticmethod
            def parse():
                return iter(())

        class WithRaw:
            @staticmethod
            def create(**_kwargs):
                return Raw()

        class Completions:
            @staticmethod
            def create(**_kwargs):
                return iter(())

        if raw:
            Completions.with_raw_response = WithRaw()

        class Client:
            class chat:  # noqa: N801 - mirrors the SDK's shape
                completions = Completions()

        return Provider(name="relay", model="m", client=Client(), label="T")

    def test_a_balance_is_read_from_the_response(self):
        provider = self._provider(headers())
        list(provider.stream_turn([], max_tokens=10, temperature=0))
        assert provider.balance.remaining_micros == 7_500_000

    def test_a_provider_starts_with_an_unknown_balance(self):
        assert not self._provider(headers()).balance.known

    def test_an_sdk_without_raw_responses_still_streams(self):
        # The balance is a display. An SDK that cannot expose headers should
        # cost the read-out, never the session.
        provider = self._provider(headers(), raw=False)
        list(provider.stream_turn([], max_tokens=10, temperature=0))
        assert not provider.balance.known


class TestTheTwoSidesAgree:
    """The header names are a contract between two codebases.

    The server stamps them and this reads them, and nothing else connects the
    two. A rename on either side produces no error anywhere — the headers just
    stop being found, and the balance silently reads as unknown forever, which
    is a state this module is explicitly designed to tolerate. So it would
    never surface as a failure. Pinned here for the same reason the tool
    registries are.

    Skipped outside the monorepo, where the TypeScript side is not present.
    """

    def _ts_source(self):
        from tests import ts_registry

        root = ts_registry.repo_root()
        if root is None:
            pytest.skip("running outside the monorepo checkout")
        source = root / "lib/inference-relay/credits-headers.ts"
        if not source.is_file():
            pytest.skip("no relay credits module in this checkout")
        return source.read_text(encoding="utf-8")

    def test_every_header_this_reads_is_one_the_server_sends(self):
        source = self._ts_source()
        for name in (
            credits.HEADER_ACCESS,
            credits.HEADER_REMAINING,
            credits.HEADER_GRANT,
            credits.HEADER_ADJUSTMENT,
            credits.HEADER_USED,
            credits.HEADER_WINDOW_ENDS,
        ):
            suffix = name[len(credits.PREFIX):]
            assert suffix in source, f"{name} is not stamped by the relay"

    def test_the_prefix_matches(self):
        source = self._ts_source()
        assert f'"{credits.PREFIX}"' in source

    def test_the_two_access_values_match(self):
        source = self._ts_source()
        assert '"active" | "depleted"' in source


class TestPrecisionThatShowsMovement:
    """A small grant spent in fractions of a credit must not read as frozen.

    Reported from a live session: "$0.10 out of $0.10" while actively using it.
    Both figures were being rounded independently, so a real deduction was
    invisible in both. The unit is credits now; the trap is identical.
    """

    def test_equal_figures_stay_whole(self):
        assert credits.format_pair(100_000, 100_000) == ("100", "100")

    def test_places_are_added_only_when_whole_credits_would_hide_it(self):
        assert credits.format_pair(99_700, 100_000) == ("99.7", "100.0")

    def test_a_difference_whole_credits_can_express_stays_whole(self):
        assert credits.format_pair(6_124_000, 10_000_000) == ("6,124", "10,000")

    def test_precision_escalates_as_far_as_it_needs_to(self):
        left, right = credits.format_pair(99_999, 100_000)

        assert left != right
        assert left == "99.999"

    def test_both_figures_are_rendered_at_one_precision(self):
        """One place on one and three on the other is unreadable as a ratio."""
        left, right = credits.format_pair(99_700, 100_000)

        assert left.count(".") == right.count(".")
        assert len(left.split(".")[1]) == len(right.split(".")[1])

    def test_an_unknown_figure_does_not_force_a_precision(self):
        assert credits.format_pair(None, 100_000) == ("", "100")

    def test_the_summary_uses_it(self):
        line = credits.summary(
            credits.Balance(remaining_micros=99_700, grant_micros=100_000)
        )

        assert line.startswith("99.7 of 100.0 credits")

    def test_a_top_up_is_still_in_the_denominator(self):
        """The gauge is drawn against grant plus adjustment, at one precision."""
        line = credits.summary(
            credits.Balance(
                remaining_micros=149_700,
                grant_micros=100_000,
                adjustment_micros=50_000,
            )
        )

        assert "149.7 of 150.0 credits" in line
