from __future__ import annotations

import httpx
import pytest
import respx

from andromeda_tools import web


class TestHtmlToText:
    def test_strips_tags_and_keeps_prose(self):
        text = web.html_to_text("<p>Hello <b>there</b></p><p>Second</p>")
        assert "Hello there" in text and "Second" in text
        assert "<p>" not in text

    def test_drops_script_and_style_content(self):
        text = web.html_to_text(
            "<style>.a{color:red}</style><script>var x=1</script><p>Real</p>"
        )
        assert "Real" in text
        assert "color:red" not in text and "var x" not in text

    def test_entities_are_decoded(self):
        assert "AT&T" in web.html_to_text("<p>AT&amp;T</p>")

    def test_malformed_html_still_yields_text(self):
        assert "Content" in web.html_to_text("<p>Content<div><span>")


class TestPrivateAddressGuard:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "0.0.0.0"])
    def test_loopback_and_unspecified_are_private(self, host):
        assert web._is_private(host) is True

    def test_a_public_host_is_not_private(self):
        assert web._is_private("example.com") is False

    def test_an_unresolvable_host_is_not_treated_as_private(self):
        """A typo should fail as a network error, not as a security refusal."""
        assert web._is_private("nonexistent-host-xyz.invalid") is False


class TestFetch:
    @respx.mock
    def test_returns_readable_text(self):
        respx.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body><p>The content</p></body></html>",
            )
        )
        result = web.fetch("https://example.com/page")
        assert result.ok and "The content" in result.content

    @respx.mock
    def test_plain_text_passes_through(self):
        respx.get("https://example.com/a.txt").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/plain"}, text="raw text"
            )
        )
        assert "raw text" in web.fetch("https://example.com/a.txt").content

    @respx.mock
    def test_a_binary_type_is_refused(self):
        respx.get("https://example.com/x.png").mock(
            return_value=httpx.Response(200, headers={"content-type": "image/png"})
        )
        result = web.fetch("https://example.com/x.png")
        assert result.ok is False and "not text" in result.content

    @respx.mock
    def test_an_error_status_is_reported(self):
        respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))
        assert "HTTP 404" in web.fetch("https://example.com/missing").content

    @respx.mock
    def test_a_network_error_is_a_result_not_a_raise(self):
        respx.get("https://example.com/x").mock(side_effect=httpx.ConnectError("down"))
        assert web.fetch("https://example.com/x").ok is False

    def test_a_loopback_url_is_refused_before_any_request(self):
        result = web.fetch("http://localhost:8080/admin")
        assert result.ok is False and "private or local network" in result.content

    def test_a_non_http_scheme_is_refused(self):
        assert web.fetch("file:///etc/passwd").ok is False
        assert web.fetch("ftp://example.com").ok is False

    def test_an_empty_url_is_refused(self):
        assert web.fetch("   ").ok is False

    @respx.mock
    def test_a_redirect_to_a_private_address_is_refused(self):
        """The guard runs again on whoever actually answered."""
        respx.get("https://example.com/redirect").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        )
        respx.get("http://127.0.0.1/secret").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"}, text="secret")
        )
        result = web.fetch("https://example.com/redirect")
        assert result.ok is False
        assert "private network" in result.content
        assert "secret" not in result.content

    @respx.mock
    def test_a_long_page_is_truncated(self):
        respx.get("https://example.com/long").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="x" * (web.MAX_TEXT + 5000),
            )
        )
        result = web.fetch("https://example.com/long")
        assert result.metadata["truncated"] is True
        assert "truncated" in result.content


class TestSearch:
    def test_no_provider_says_which_keys_would_work(self, monkeypatch):
        for spec in web.PROVIDERS.values():
            monkeypatch.delenv(spec["env"], raising=False)
        result = web.search("anything")
        assert result.ok is False
        assert "BRAVE_SEARCH_API_KEY" in result.content

    @respx.mock
    def test_brave_results_are_rendered(self, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
        respx.get(web.PROVIDERS["brave"]["url"]).mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "A result",
                                "url": "https://example.com",
                                "description": "<b>snippet</b> text",
                            }
                        ]
                    }
                },
            )
        )
        result = web.search("query")
        assert result.ok
        assert "A result" in result.content and "https://example.com" in result.content
        assert "<b>" not in result.content

    @respx.mock
    def test_tavily_is_used_when_it_is_the_configured_one(self, monkeypatch):
        for spec in web.PROVIDERS.values():
            monkeypatch.delenv(spec["env"], raising=False)
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        respx.post(web.PROVIDERS["tavily"]["url"]).mock(
            return_value=httpx.Response(
                200, json={"results": [{"title": "T", "url": "https://t.example", "content": "c"}]}
            )
        )
        result = web.search("query")
        assert result.metadata["provider"] == "tavily"

    @respx.mock
    def test_no_results_is_a_success(self, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
        respx.get(web.PROVIDERS["brave"]["url"]).mock(
            return_value=httpx.Response(200, json={"web": {"results": []}})
        )
        assert web.search("nothing").ok is True

    def test_an_empty_query_is_refused(self, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
        assert web.search("  ").ok is False

    @respx.mock
    def test_a_provider_error_is_a_result_not_a_raise(self, monkeypatch):
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
        respx.get(web.PROVIDERS["brave"]["url"]).mock(return_value=httpx.Response(500))
        assert web.search("query").ok is False
