from __future__ import annotations

import httpx
import pytest
import respx

from oikb.connectors.confluence import ConfluenceConnector, parse_confluence_source
from oikb.sync import build_manifest_filter


def test_parse_hierarchical_source() -> None:
    assert parse_confluence_source("confluence:ABC?structure=hierarchical") == {
        "base_url": None,
        "space_key": "ABC",
        "structure": "hierarchical",
        "content": "pages",
        "format": "text",
    }


def test_parse_url_source_with_structure() -> None:
    assert parse_confluence_source(
        "https://company.atlassian.net/ABC?structure=hierarchical"
    ) == {
        "base_url": "https://company.atlassian.net",
        "space_key": "ABC",
        "structure": "hierarchical",
        "content": "pages",
        "format": "text",
    }


@pytest.mark.parametrize(
    "source",
    [
        "confluence:ABC?structure=invalid",
        "confluence:ABC?unexpected=value",
        "confluence:ABC?content=nope",
        "confluence:ABC?format=rtf",
    ],
)
def test_parse_rejects_invalid_structure(source: str) -> None:
    with pytest.raises(ValueError):
        parse_confluence_source(source)


@respx.mock
def test_resolves_space_key_to_id() -> None:
    lookup = respx.get("https://test.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "123456789", "key": "ABC"}]}
        )
    )
    pages = respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": []}))

    connector = ConfluenceConnector(
        space_key="ABC", base_url="https://test.atlassian.net", token="token"
    )
    try:
        assert connector.build_manifest() == []
    finally:
        connector.close()

    assert lookup.calls[0].request.url.params["keys"] == "ABC"
    assert pages.called


@respx.mock
def test_rejects_mismatched_space_lookup() -> None:
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "123456789", "key": "OTHER"}]}
        )
    )

    with pytest.raises(ValueError, match="Confluence space 'ABC' not found"):
        ConfluenceConnector(
            space_key="ABC",
            base_url="https://test.atlassian.net",
            token="token",
        )


def _pages() -> list[dict[str, object]]:
    return [
        {"id": "1", "title": "FAQ", "version": {"number": 1}},
        {
            "id": "2",
            "title": "Benefits",
            "parentId": "1",
            "version": {"number": 2},
        },
        {"id": "3", "title": "Internal Notes", "version": {"number": 1}},
    ]


@pytest.mark.parametrize(
    ("structure", "expected"),
    [
        ("flat", ["Benefits.txt", "FAQ.txt", "Internal Notes.txt"]),
        ("hierarchical", ["FAQ.txt", "FAQ/Benefits.txt", "Internal Notes.txt"]),
    ],
)
@respx.mock
def test_manifest_in_both_modes(structure: str, expected: list[str]) -> None:
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": _pages()}))

    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure=structure,
    )
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert [entry.display_path for entry in manifest] == expected


@respx.mock
def test_hierarchical_paths_work_with_filter_and_read_file() -> None:
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": _pages()}))
    content = respx.get("https://test.atlassian.net/wiki/api/v2/pages/2").mock(
        return_value=httpx.Response(
            200, json={"body": {"storage": {"value": "<p>Benefits</p>"}}}
        )
    )

    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure="hierarchical",
    )
    try:
        manifest = connector.build_manifest()
        selected = build_manifest_filter(include=["FAQ*"])(manifest)
        assert [entry.display_path for entry in selected] == [
            "FAQ.txt",
            "FAQ/Benefits.txt",
        ]
        assert connector.read_file("FAQ", "Benefits.txt") == b"Benefits"
    finally:
        connector.close()

    assert content.called


@respx.mock
def test_hierarchical_mode_disambiguates_duplicate_paths() -> None:
    pages = [
        {"id": "1", "title": "FAQ", "version": {"number": 1}},
        {"id": "2", "title": "FAQ", "version": {"number": 1}},
    ]
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": pages}))
    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure="hierarchical",
    )

    try:
        assert [e.display_path for e in connector.build_manifest()] == ["FAQ_1.txt", "FAQ_2.txt"]
    finally:
        connector.close()


@respx.mock
def test_manifest_can_be_built_repeatedly() -> None:
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": _pages()}))
    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure="hierarchical",
    )

    try:
        first = connector.build_manifest()
        second = connector.build_manifest()
    finally:
        connector.close()

    assert first == second


@pytest.mark.parametrize("structure", ["flat", "hierarchical"])
@respx.mock
def test_v1_pagination_context_path_auth_and_content(structure, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_API_VERSION", "v1")
    monkeypatch.setenv("CONFLUENCE_USER", "")
    endpoint = "https://wiki.example/confluence/rest/api/content"
    route = respx.get(endpoint).mock(side_effect=[
        httpx.Response(200, json={
            "results": [{"id": "1", "title": "Parent", "version": {"number": 1}}],
            # The server caps the requested 250 to 1. The next link is authoritative.
            "limit": 1, "_links": {"next": "/rest/api/content?start=1&limit=1"},
        }),
        httpx.Response(200, json={"results": [{
            "id": "2", "title": "Child", "version": {"number": 3},
            "ancestors": [{"id": "1", "title": "Parent"}],
        }]}),
    ])
    content = respx.get(endpoint + "/2").respond(200, json={
        "body": {"storage": {"value": '<ac:plain-text-body><![CDATA[if x < 2:\n    run()]]></ac:plain-text-body>'}},
    })
    with ConfluenceConnector("ENG", base_url="https://wiki.example/confluence", token="pat", structure=structure) as connector:
        manifest = connector.build_manifest()
        child = next(e for e in manifest if e.filename == "Child.txt")
        assert child.path == ("Parent" if structure == "hierarchical" else "")
        assert connector.read_file(child.path, child.filename) == b"if x < 2:\n    run()"
    assert [call.request.url.params["start"] for call in route.calls] == ["0", "1"]
    assert route.calls[0].request.url.params["spaceKey"] == "ENG"
    assert route.calls[0].request.url.params["expand"] == "ancestors,version"
    assert content.calls[0].request.headers["Authorization"] == "Bearer pat"
    assert content.calls[0].request.url.params["expand"] == "body.storage"


@respx.mock
def test_v2_cursor_is_decoded_and_wiki_path_is_not_duplicated():
    route = respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/pages").mock(side_effect=[
        httpx.Response(200, json={"results": [{"id": "1", "title": "One"}], "_links": {"next": "?cursor=a%2Bb%3D"}}),
        httpx.Response(200, json={"results": [{"id": "2", "title": "Two"}]}),
    ])
    with ConfluenceConnector("123", base_url="https://test.atlassian.net/wiki", user="email", token="secret") as connector:
        assert len(connector.build_manifest()) == 2
    assert route.calls[1].request.url.params["cursor"] == "a+b="
    assert route.calls[0].request.headers["Authorization"] == "Basic ZW1haWw6c2VjcmV0"


@respx.mock
def test_colliding_names_are_stable_and_route_to_distinct_pages():
    pages = [
        {"id": "1", "title": "A/B"}, {"id": "2", "title": "A:B"},
        {"id": "3", "title": "A_B_1"},
    ]
    respx.get("https://wiki.example/wiki/api/v2/spaces/123/pages").mock(side_effect=[
        httpx.Response(200, json={"results": pages}),
        httpx.Response(200, json={"results": list(reversed(pages))}),
    ])
    for page in pages:
        respx.get(f"https://wiki.example/wiki/api/v2/pages/{page['id']}").respond(
            200, json={"body": {"storage": {"value": page["title"]}}},
        )
    with ConfluenceConnector("123", base_url="https://wiki.example", token="pat") as connector:
        manifest = connector.build_manifest()
        assert len({e.display_path for e in manifest}) == 3
        assert {connector.read_file(e.path, e.filename) for e in manifest} == {b"A/B", b"A:B", b"A_B_1"}
        assert connector.build_manifest() == manifest


@pytest.mark.parametrize("next_link", ["?limit=1", "?start=0", "?start=-1"])
@respx.mock
def test_invalid_pagination_fails_instead_of_returning_partial_manifest(next_link):
    respx.get("https://wiki.example/rest/api/content").respond(200, json={
        "results": [{"id": "1", "title": "One"}], "_links": {"next": next_link},
    })
    with ConfluenceConnector("ENG", base_url="https://wiki.example", token="pat", api_version="v1") as connector:
        with pytest.raises(ValueError, match="pagination"):
            connector.build_manifest()


@respx.mock
def test_empty_pages_remain_skipped():
    from oikb.connectors import SourceFileUnavailable
    respx.get("https://wiki.example/rest/api/content").respond(200, json={"results": [{"id": "1", "title": "Index"}]})
    respx.get("https://wiki.example/rest/api/content/1").respond(200, json={
        "body": {"storage": {"value": '<ac:structured-macro ac:name="children"/>'}},
    })
    with ConfluenceConnector("ENG", base_url="https://wiki.example", token="pat", api_version="v1") as connector:
        connector.build_manifest()
        with pytest.raises(SourceFileUnavailable):
            connector.read_file("", "Index.txt")


def test_source_context_path_and_invalid_version():
    assert parse_confluence_source("confluence:https://wiki.example/confluence/ENG")["base_url"] == "https://wiki.example/confluence"
    with pytest.raises(ValueError, match="API_VERSION"):
        ConfluenceConnector("ENG", api_version="v3")


@respx.mock
def test_parse_content_and_format():
    assert parse_confluence_source(
        "confluence:ENG?content=all&format=markdown&structure=hierarchical"
    ) == {
        "base_url": None,
        "space_key": "ENG",
        "structure": "hierarchical",
        "content": "all",
        "format": "markdown",
    }


def test_markdown_format_requires_extra(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fail_markdownify(name, *args, **kwargs):
        if name == "markdownify":
            raise ImportError("no markdownify")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_markdownify)
    with pytest.raises(ImportError, match=r"oikb\[confluence\]"):
        ConfluenceConnector("ENG", base_url="https://wiki.example", token="pat", fmt="markdown")


@respx.mock
def test_blogposts_use_year_folders_only_when_hierarchical():
    posts = [{
        "id": "9",
        "title": "Weekly Update",
        "createdAt": "2024-05-02T10:00:00.000Z",
        "version": {"number": 3},
    }]
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/blogposts").mock(
        return_value=httpx.Response(200, json={"results": posts})
    )

    flat = ConfluenceConnector(
        "123", base_url="https://test.atlassian.net", token="pat", content="blogs"
    )
    hierarchical = ConfluenceConnector(
        "123",
        base_url="https://test.atlassian.net",
        token="pat",
        content="blogs",
        structure="hierarchical",
    )
    try:
        assert [e.display_path for e in flat.build_manifest()] == ["Blogs/Weekly Update.txt"]
        assert [e.display_path for e in hierarchical.build_manifest()] == [
            "Blogs/2024/Weekly Update.txt"
        ]
    finally:
        flat.close()
        hierarchical.close()


@respx.mock
def test_all_content_keeps_pages_and_blogs_apart():
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/pages").mock(
        return_value=httpx.Response(200, json={"results": _pages()})
    )
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/blogposts").mock(
        return_value=httpx.Response(200, json={"results": [{
            "id": "9",
            "title": "Weekly Update",
            "createdAt": "2024-05-02T10:00:00.000Z",
            "version": {"number": 1},
        }]})
    )
    connector = ConfluenceConnector(
        "123",
        base_url="https://test.atlassian.net",
        token="pat",
        content="all",
        structure="hierarchical",
    )
    try:
        assert [e.display_path for e in connector.build_manifest()] == [
            "Blogs/2024/Weekly Update.txt",
            "Pages/FAQ.txt",
            "Pages/FAQ/Benefits.txt",
            "Pages/Internal Notes.txt",
        ]
    finally:
        connector.close()


@respx.mock
def test_bare_source_still_syncs_pages_at_root():
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/pages").mock(
        return_value=httpx.Response(200, json={"results": _pages()})
    )
    connector = ConfluenceConnector("123", base_url="https://test.atlassian.net", token="pat")
    try:
        assert [e.path for e in connector.build_manifest()] == ["", "", ""]
    finally:
        connector.close()


@respx.mock
def test_markdown_export_and_code_macro():
    pytest.importorskip("markdownify")
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/pages").mock(
        return_value=httpx.Response(200, json={"results": [{
            "id": "2", "title": "Benefits", "version": {"number": 1},
        }]})
    )
    storage = (
        "<h1>Title</h1><p>Hello</p>"
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[print(1)]]></ac:plain-text-body>"
        "</ac:structured-macro>"
        '<ac:image><ri:attachment ri:filename="diagram.png" /></ac:image>'
    )
    respx.get("https://test.atlassian.net/wiki/api/v2/pages/2").mock(
        return_value=httpx.Response(200, json={"body": {"storage": {"value": storage}}})
    )
    connector = ConfluenceConnector(
        "123", base_url="https://test.atlassian.net", token="pat", fmt="markdown"
    )
    try:
        manifest = connector.build_manifest()
        assert [e.display_path for e in manifest] == ["Benefits.md"]
        body = connector.read_file("", "Benefits.md").decode()
    finally:
        connector.close()
    assert body.startswith("# Title")
    assert "```python" in body
    assert "print(1)" in body
    assert "diagram.png" in body


@respx.mock
def test_malformed_record_is_skipped():
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/pages").mock(
        return_value=httpx.Response(200, json={"results": [
            {"id": "1", "title": "FAQ", "version": {"number": 1}},
            {"id": "2", "version": {"number": 1}},
        ]})
    )
    connector = ConfluenceConnector("123", base_url="https://test.atlassian.net", token="pat")
    try:
        assert [e.display_path for e in connector.build_manifest()] == ["FAQ.txt"]
    finally:
        connector.close()


@respx.mock
def test_v1_blogposts_read_created_year(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_API_VERSION", "v1")
    monkeypatch.setenv("CONFLUENCE_USER", "")

    def respond(request):
        if request.url.params.get("type") == "blogpost":
            return httpx.Response(200, json={"results": [{
                "id": "9",
                "title": "Weekly Update",
                "history": {"createdDate": "2023-01-04T00:00:00.000Z"},
                "version": {"number": 1},
            }]})
        return httpx.Response(200, json={"results": []})

    route = respx.get("https://wiki.example/rest/api/content").mock(side_effect=respond)
    respx.get("https://wiki.example/rest/api/content/9").respond(
        200, json={"body": {"storage": {"value": "<p>Hello</p>"}}}
    )
    with ConfluenceConnector(
        "ENG",
        base_url="https://wiki.example",
        token="pat",
        content="blogs",
        structure="hierarchical",
    ) as connector:
        manifest = connector.build_manifest()
        assert [e.display_path for e in manifest] == ["Blogs/2023/Weekly Update.txt"]
        assert connector.read_file("Blogs/2023", "Weekly Update.txt") == b"Hello"
    assert route.calls[0].request.url.params["type"] == "blogpost"
    assert route.calls[0].request.url.params["expand"] == "history,version"


@respx.mock
def test_cli_passes_source_auth_and_can_override_basic_env(monkeypatch):
    from oikb.cli import _resolve_connector
    monkeypatch.setenv("CONFLUENCE_USER", "cloud-user")
    route = respx.get("https://wiki.example/rest/api/content").respond(200, json={"results": []})
    with _resolve_connector("confluence:ENG", auth={
        "base_url": "https://wiki.example", "user": "", "token": "pat", "api_version": "v1",
    }) as connector:
        connector.build_manifest()
    assert route.calls[0].request.headers["Authorization"] == "Bearer pat"
