"""Vision, and the auxiliary model behind it.

The conversation model is text-only, so this is not a matter of prompting it
better — the capability lives in a second model reachable only through this
tool.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from andromeda_agent import auxiliary
from andromeda_agent.errors import AgentError
from andromeda_agent.models import ALLOWED_MODEL_IDS, auxiliary_model
from andromeda_tools import Workspace, vision

# A one-pixel PNG, so the fixture is a real image rather than bytes that happen
# to have the right extension.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeAux:
    def __init__(self, answer: str = "a red square", raises: Exception | None = None):
        self.answer = answer
        self.raises = raises
        self.seen: list[tuple[str, str]] = []

    def ask(self, prompt, image=None, mime_type="", max_tokens=0):
        if self.raises is not None:
            raise self.raises
        self.seen.append((prompt, mime_type))
        return self.answer


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(PIXEL)
    return path


class TestModelSeparation:
    def test_the_vision_model_is_not_the_conversation_model(self):
        """The lock governs the model that reasons; this is a side call."""
        assert auxiliary_model("vision") not in ALLOWED_MODEL_IDS

    def test_an_unknown_purpose_has_no_model(self):
        assert auxiliary_model("telepathy") is None

    def test_a_provider_without_a_client_yields_no_auxiliary(self):
        class Bare:
            model = "x"

        assert auxiliary.build("vision", Bare()) is None

    def test_it_borrows_the_providers_client(self):
        """A second endpoint would be a second billing path."""

        class WithClient:
            client = object()

        built = auxiliary.build("vision", WithClient())
        assert built is not None
        assert built.client is WithClient.client


class TestReadImage:
    def test_it_loads_a_real_image(self, image):
        data, mime_type = auxiliary.read_image(image)
        assert data == PIXEL and mime_type == "image/png"

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(AgentError, match="does not exist"):
            auxiliary.read_image(tmp_path / "nope.png")

    def test_a_directory_is_refused(self, tmp_path):
        with pytest.raises(AgentError, match="is a directory"):
            auxiliary.read_image(tmp_path)

    def test_an_unsupported_type_names_what_works(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        with pytest.raises(AgentError, match="image/png"):
            auxiliary.read_image(path)

    def test_an_oversized_image_is_refused_before_it_is_sent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auxiliary, "MAX_IMAGE_BYTES", 10)
        path = tmp_path / "big.png"
        path.write_bytes(PIXEL)
        with pytest.raises(AgentError, match="too large"):
            auxiliary.read_image(path)


class TestAnalyze:
    def test_it_returns_the_description(self, workspace, image):
        result = vision.analyze(workspace, FakeAux(), "shot.png")
        assert result.ok and "a red square" in result.content

    def test_the_image_itself_never_enters_the_result(self, workspace, image):
        """Or a screenshot costs thousands of tokens on every later turn."""
        result = vision.analyze(workspace, FakeAux(), "shot.png")
        assert base64.b64encode(PIXEL).decode() not in result.content

    def test_a_prompt_is_passed_through(self, workspace, image):
        aux = FakeAux()
        vision.analyze(workspace, aux, "shot.png", prompt="What colour is it?")
        assert aux.seen[0][0] == "What colour is it?"

    def test_omitting_the_prompt_asks_for_a_full_description(self, workspace, image):
        aux = FakeAux()
        vision.analyze(workspace, aux, "shot.png")
        assert aux.seen[0][0] == vision.DEFAULT_PROMPT

    def test_the_mime_type_reaches_the_model(self, workspace, image):
        aux = FakeAux()
        vision.analyze(workspace, aux, "shot.png")
        assert aux.seen[0][1] == "image/png"

    def test_without_a_vision_model_it_says_so(self, workspace, image):
        result = vision.analyze(workspace, None, "shot.png")
        assert result.ok is False and "No vision model" in result.content

    def test_it_cannot_read_outside_the_workspace(self, workspace):
        result = vision.analyze(workspace, FakeAux(), "/etc/hosts")
        assert result.ok is False and "outside the workspace" in result.content

    def test_a_failing_model_is_a_result_not_a_raise(self, workspace, image):
        aux = FakeAux(raises=RuntimeError("upstream down"))
        result = vision.analyze(workspace, aux, "shot.png")
        assert result.ok is False and "upstream down" in result.content

    def test_an_empty_description_is_reported(self, workspace, image):
        result = vision.analyze(workspace, FakeAux(answer="  "), "shot.png")
        assert result.ok is False


class TestBeltInteraction:
    def test_no_specialist_lane_gets_vision(self):
        """It is `outbound`; every belt here admits only local reads."""
        from andromeda_agent.specialists import SPECIALISTS
        from andromeda_tools.spec import ToolSpec

        spec = ToolSpec(
            name="vision_analyze",
            description="",
            parameters={},
            risk_tier="outbound",
            category="read",
            run=lambda: None,
        )
        for belt in SPECIALISTS.values():
            assert not belt.admits(spec), belt.id
