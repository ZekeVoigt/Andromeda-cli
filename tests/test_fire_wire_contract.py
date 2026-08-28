"""The signed payload, checked against the *other* implementation of it.

Two programs in two languages sign the same string, and nothing but agreement
makes a fire work. When they disagree the failure is a `401` on every fire from
a runner that looks perfectly healthy — the module docstring in `serve.py` calls
this out, and it has already cost one real debugging session.

Unit tests on either side cannot catch it: each is self-consistent. So this
reads the actual TypeScript out of `convex/cloudFire.ts`, runs it under Node,
and compares digests with Python's over inputs chosen to break the places the
two languages differ by default:

  * key order            — `JSON.stringify` preserves insertion order; Python's
                           `sort_keys` does not exist in JS at all
  * non-ASCII            — Python escapes to `\\uXXXX` unless told otherwise
  * whitespace           — both default to `", "` separators; both must not
  * nesting              — sorting has to be recursive, not top-level
  * empty containers     — `{}` and `[]` are not `None`

Written to `.mts` rather than `.mjs`: Node strips TypeScript annotations from
a `.ts`/`.mts` file natively (23+), which is what lets the *real* source run
here instead of a hand-maintained JavaScript copy of it.

Skipped, never silently passed, when Node is missing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from andromeda_agent import serve as serve_module

CONVEX = Path(__file__).resolve().parents[2] / "convex" / "cloudFire.ts"

# Chosen for the disagreements, not for coverage. Each one fails if a specific
# default is left in place on either side.
VECTORS = [
    {"b": 2, "a": 1},
    {"a": 1, "b": 2},
    {"who": "renée", "note": "naïve café"},
    {"outer": {"z": 1, "a": {"y": 2, "b": 3}}},
    {"list": [{"b": 1, "a": 2}, {"d": 3, "c": 4}]},
    {"empty_obj": {}, "empty_list": [], "zero": 0, "false": False},
    {"family": "messaging", "kind": "arrived", "where": {"id": "abc"}},
    [],
    {},
    "a bare string",
    42,
]


def _extract(name: str) -> str:
    """One function's source out of the Convex module.

    Read from the real file rather than copied here: a copy is a third
    implementation, and a third implementation is a third thing that can drift.
    """
    source = CONVEX.read_text(encoding="utf-8")
    match = re.search(
        rf"^function {name}\(.*?^}}$", source, re.MULTILINE | re.DOTALL
    )
    assert match, f"{name} not found in {CONVEX} — did it get renamed?"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_both_implementations_sign_the_same_bytes(tmp_path):
    script = tmp_path / "check.mts"
    script.write_text(
        'import { createHash } from "node:crypto";\n'
        + _extract("canonicalEvent")
        + "\n"
        + _extract("eventDigest")
        + "\n"
        "const vectors = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify(vectors.map((v) => "
        "[canonicalEvent(v), eventDigest(v)])));\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(script), json.dumps(VECTORS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    from_node = json.loads(out.stdout)

    for vector, (encoded, digest) in zip(VECTORS, from_node, strict=True):
        assert encoded == serve_module.canonical_event(vector), vector
        assert digest == serve_module.event_digest(vector), vector


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_no_event_produces_no_digest_on_either_side(tmp_path):
    """The rollout property. An empty digest is what keeps a time fire byte
    identical to every fire this system has signed since it was written, and
    that is what lets the two ends be deployed minutes apart."""
    script = tmp_path / "empty.mts"
    script.write_text(
        'import { createHash } from "node:crypto";\n'
        + _extract("canonicalEvent")
        + "\n"
        + _extract("eventDigest")
        + "\n"
        "console.log(JSON.stringify([eventDigest(undefined), eventDigest(null)]));\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["", ""]
    assert serve_module.event_digest(None) == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_whole_signed_string_matches(tmp_path):
    """Not just the digest — the joined payload, which is where a stray
    separator would hide. `u|j|t|5` and `u|j|t|5|` are different strings and
    only one of them verifies."""
    script = tmp_path / "payload.mts"
    script.write_text(
        'import { createHash } from "node:crypto";\n'
        + _extract("canonicalEvent")
        + "\n"
        + _extract("eventDigest")
        + "\n"
        "const [userId, jobId, fireAt, exp] = ['u', 'j', 't', 5];\n"
        "const build = (event) => {\n"
        "  const digest = eventDigest(event);\n"
        "  return digest\n"
        "    ? `${userId}|${jobId}|${fireAt}|${exp}|${digest}`\n"
        "    : `${userId}|${jobId}|${fireAt}|${exp}`;\n"
        "};\n"
        "console.log(JSON.stringify(["
        "build(undefined), build({kind: 'arrived', family: 'code'})]));\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    plain, with_event = json.loads(out.stdout)

    assert plain.encode() == serve_module._payload("u", "j", "t", 5)
    assert with_event.encode() == serve_module._payload(
        "u", "j", "t", 5, {"kind": "arrived", "family": "code"}
    )
