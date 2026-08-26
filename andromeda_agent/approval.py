"""The approval gate.

Ported from the TypeScript runtime's `checkToolPolicy`, keeping its vocabulary
and — more importantly — its *ordering*. The ordering is the whole design:

  A hard refusal must never be reachable by a softer answer further down.

That is why the tier ceiling is read before the session grants rather than
after. A standing "always allow terminal" is a decision a person made about a
tool while watching their own turns; it is not a decision that a narrowed
context may run shell commands, and reading it first would let an afternoon of
ordinary approvals quietly reopen every ceiling in the table.

Two invariants carried over verbatim:

  1. Consent is established before the thing that needs it is created, and it
     is *stated* — the prompt shows the command, not a paraphrase of it.
  2. A narrowed policy is never more permissive than the one it came from.
     `Policy.narrow()` can only subtract; there is no widening path.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from andromeda_tools import RiskTier, ToolSpec, tier_rank

from .allowlist import Allowlist
from .specialists import Specialist

Decision = Literal["allowed", "needs_approval", "denied"]
ApprovalMode = Literal["auto", "ask", "deny"]

MODES: tuple[ApprovalMode, ...] = ("auto", "ask", "deny")

MCP_PREFIX = "mcp__"


def _is_mcp(name: str) -> bool:
    return name.startswith(MCP_PREFIX)

# Tiers that always stop for a person in `ask` mode, whatever else is true.
ALWAYS_CONFIRM: frozenset[RiskTier] = frozenset({"destructive", "irreversible"})


@dataclass(frozen=True)
class Policy:
    mode: ApprovalMode = "ask"
    enabled: frozenset[str] = field(default_factory=frozenset)
    # The ceiling. Nothing above this tier runs, at any mode, with any grant.
    max_tier: RiskTier = "irreversible"
    # Per-tool decisions the user set deliberately (config, flags).
    overrides: dict[str, Decision] = field(default_factory=dict)
    # "Allow for the rest of this session", granted at a prompt.
    session_grants: frozenset[str] = field(default_factory=frozenset)
    # The belt, when this policy belongs to a delegated child.
    specialist: Specialist | None = None
    # Trust accumulated across sessions. Read after the belt, the ceiling and
    # any explicit override — never before them.
    allowlist: Allowlist | None = None

    def decide(self, spec: ToolSpec) -> Decision:
        # 1. A refusing mode answers everything.
        if self.mode == "deny":
            return "denied"

        # 2. A tool that is switched off is not a tool that can be approved.
        #
        # MCP tool names are not knowable when the defaults are written — they
        # come from whatever servers the user configured — so the family is
        # allowed by prefix. It is still gated: every one is `outbound`, so
        # `ask` stops for a person and a pipe never sees them.
        if spec.name not in self.enabled and not _is_mcp(spec.name):
            return "denied"

        # 3. The specialist belt, before everything below it. A child runs in
        #    `auto` mode, so a tool the belt rejects must come back `denied`
        #    and not `needs_approval` — the difference between a Writer that
        #    cannot send and a Writer that sends after a pause.
        if self.specialist is not None and not self.specialist.admits(spec):
            return "denied"

        # 4. The ceiling. Before overrides and before grants, deliberately:
        #    this is the rule that must not be reachable by a softer answer.
        if tier_rank(spec.risk_tier) > tier_rank(self.max_tier):
            return "denied"

        # 5. What the user set on purpose beats what they accumulated. This
        #    is the deliberate deviation from the hosted order, which reads the
        #    allowlist first: an override is a statement, an entry is a habit.
        override = self.overrides.get(spec.name)
        if override is not None:
            return override

        # 6. Learned trust, bound to the tier it was granted at. A tool that has
        #    become more dangerous since is back at the gate.
        if self.allowlist is not None:
            entry = self.allowlist.entry_for(spec.name, spec.risk_tier)
            if entry is not None:
                return "denied" if entry.trust_level == "always_deny" else "allowed"

        # 7. A standing grant from this session.
        if spec.name in self.session_grants:
            return "allowed"

        # 8. Dangerous work stops for a person regardless of mode — except in
        #    `auto`, which is the explicit "do not ask me" setting.
        if spec.risk_tier in ALWAYS_CONFIRM and self.mode != "auto":
            return "needs_approval"

        if self.mode == "ask" and spec.risk_tier != "safe_local":
            return "needs_approval"

        return "allowed"

    def grant_for_session(self, tool_name: str) -> "Policy":
        """Record an "allow for the rest of this session" answer.

        Still subject to the ceiling and to `enabled` on the next call, because
        `decide` reads both before it reads grants.
        """
        return replace(self, session_grants=self.session_grants | {tool_name})

    def narrow(
        self,
        *,
        mode: ApprovalMode | None = None,
        enabled: frozenset[str] | None = None,
        max_tier: RiskTier | None = None,
        specialist: Specialist | None = None,
    ) -> "Policy":
        """Derive a stricter policy. It can only subtract.

        Every argument is intersected or clamped against what this policy
        already allows, so a caller cannot hand a child more than it holds —
        including by passing a *laxer* mode or a *higher* ceiling. This is the
        one construction path for a delegated context; there is no other.
        """
        next_mode = self.mode
        if mode is not None:
            # Strictness order: auto < ask < deny. Only movement toward deny.
            next_mode = mode if MODES.index(mode) > MODES.index(self.mode) else self.mode

        next_enabled = self.enabled if enabled is None else (self.enabled & enabled)

        next_tier = self.max_tier
        if max_tier is not None and tier_rank(max_tier) < tier_rank(self.max_tier):
            next_tier = max_tier

        return Policy(
            mode=next_mode,
            enabled=next_enabled,
            max_tier=next_tier,
            overrides=dict(self.overrides),
            # Grants do not descend. A person approved that tool for the context
            # they were watching, not for one spawned later out of their sight.
            session_grants=frozenset(),
            # A belt already in place is kept even if none is passed: narrowing
            # must never be a way to shed one.
            specialist=specialist or self.specialist,
            # Learned trust does NOT descend, for the same reason grants do not:
            # a person taught this gate about their own turns, not about a lane
            # spawned later out of their sight.
            allowlist=None,
        )


@dataclass
class ApprovalRequest:
    spec: ToolSpec
    arguments: dict
    summary: str
    # So the prompt can offer to stop asking, without the surface having to
    # reach back into the policy for it.
    allowlist: Allowlist | None = None
    # Why this call is at the gate when the policy would have let it past —
    # set when a hook escalated it. Shown at the prompt: a question that
    # appears for no visible reason is a question people answer without
    # reading.
    reason: str | None = None


# What a prompt can answer.
Answer = Literal["once", "session", "always", "never", "no"]
