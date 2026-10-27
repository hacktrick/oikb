"""Confluence connector — sync a Confluence space to a Knowledge Base.

Supports Cloud REST API v2 (default) and Server/Data Center REST API v1.
Set CONFLUENCE_API_VERSION=v1 for Server/Data Center. Authentication uses
Basic auth with CONFLUENCE_USER, or Bearer auth when only a token is supplied.

Pages are exported as plain text by default. Blog posts are optional
(``content=blogs`` or ``content=all``). ``format=markdown`` needs the
``oikb[confluence]`` extra.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
from collections import Counter
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable

log = logging.getLogger(__name__)

# A link's visible label is the title of another page in the space, which syncs
# as its own file. Dropped with the link so a section index does not become a
# document that is nothing but titles already in the KB.
_LINK_BODY = re.compile(
    r"<ac:plain-text-link-body>.*?</ac:plain-text-link-body>", re.S | re.I
)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
# Where one block ends the next begins on its own line: a code block run
# together with the sentence before it reads as one thought.
_BREAK = re.compile(
    r"<br\s*/?>"
    r"|</(?:p|div|h[1-6]|li|ul|ol|tr|td|th|blockquote|pre|table|section)\s*>"
    r"|</ac:[\w.-]+\s*>",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
# Marks where a parked macro body goes back. Storage format cannot contain NUL.
_PARKED = re.compile("\x00(\\d+)\x00")
_INLINE_SPACE = re.compile(r"[^\S\n]+")


def _storage_to_text(storage_html: str) -> str:
    """Convert Confluence storage format (XHTML) to plain text."""
    if not storage_html:
        return ""

    text = _LINK_BODY.sub(" ", storage_html)

    # Macro bodies are literal text, so they are parked before the markup passes
    # run: `<[^>]+>` would otherwise treat `<![CDATA[...]]>` as one tag and
    # delete a code block or panel whole, and their entities are not escaped.
    parked: list[str] = []

    def _park(match: re.Match) -> str:
        parked.append(match.group(1))
        return f"\n\x00{len(parked) - 1}\x00\n"

    text = _CDATA.sub(_park, text)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)

    lines = (_INLINE_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)

    # Restored after the whitespace pass so a code block keeps its own line
    # breaks and indentation.
    return _PARKED.sub(lambda m: parked[int(m.group(1))].strip(), text).strip()


def _storage_to_markdown(storage_html: str) -> str:
    """Convert Confluence storage XHTML to Markdown.

    ``markdownify`` handles headings, lists, tables, and links. A short
    pre-pass turns code macros into fenced blocks and attachment images
    into filename references. Other macros keep their inner text.
    """
    from markdownify import markdownify

    normalized = _normalize_confluence_macros(storage_html)
    return markdownify(
        normalized,
        heading_style="ATX",
        bullets="-",
        code_language_callback=_code_language,
    ).strip()


def _code_language(el: Any) -> str:
    code = el.find("code")
    node = code if code is not None else el
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    for name in classes:
        if name.startswith("language-"):
            return name.removeprefix("language-")
    return ""


def _normalize_confluence_macros(storage_html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(storage_html or "", "html.parser")
    for macro in soup.find_all("ac:structured-macro", attrs={"ac:name": "code"}):
        _rewrite_code_macro(soup, macro)
    for image in soup.find_all("ac:image"):
        _rewrite_image_macro(soup, image)
    return str(soup)


def _rewrite_code_macro(soup: Any, macro: Any) -> None:
    language = ""
    for param in macro.find_all("ac:parameter"):
        if param.get("ac:name") == "language" and param.string:
            language = param.string.strip()
    body = macro.find("ac:plain-text-body")
    code_text = body.get_text() if body else ""
    pre = soup.new_tag("pre")
    code = soup.new_tag("code")
    if language:
        code["class"] = f"language-{language}"
    code.string = code_text
    pre.append(code)
    macro.replace_with(pre)


def _rewrite_image_macro(soup: Any, image: Any) -> None:
    filename = "image"
    attachment = image.find("ri:attachment")
    if attachment is not None and attachment.get("ri:filename"):
        filename = attachment["ri:filename"]
    image.replace_with(soup.new_tag("img", alt=filename, src=filename))


def _created_year(item: dict[str, Any]) -> str:
    created = item.get("createdAt") or (item.get("history") or {}).get("createdDate") or ""
    if len(created) >= 4 and created[:4].isdigit():
        return created[:4]
    return "unknown"


class ConfluenceConnector(BaseConnector):
    """Sync pages, and optionally blog posts, from a Confluence space.

    Args:
        space_key: Confluence space key (e.g. "ENG").
        base_url:  Confluence instance URL (or CONFLUENCE_URL env var).
        user:      Confluence user email (or CONFLUENCE_USER env var).
        token:     Confluence API token (or CONFLUENCE_TOKEN env var).
        structure: "flat" or "hierarchical" manifest paths.
        content:   "pages" (default), "blogs", or "all".
        fmt:       "text" (default) or "markdown".
        api_version: "v1" or "v2" (or CONFLUENCE_API_VERSION, default "v2").
    """

    def __init__(
        self,
        space_key: str,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
        structure: str = "flat",
        content: str = "pages",
        fmt: str = "text",
        api_version: str | None = None,
    ):
        if structure not in {"flat", "hierarchical"}:
            raise ValueError("structure must be 'flat' or 'hierarchical'")
        if content not in {"pages", "blogs", "all"}:
            raise ValueError("content must be 'pages', 'blogs', or 'all'")
        if fmt not in {"text", "markdown"}:
            raise ValueError("format must be 'text' or 'markdown'")
        if fmt == "markdown":
            try:
                import markdownify  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Markdown export requires markdownify. "
                    "Install with: pip install oikb[confluence]"
                ) from exc
        self.space_key = space_key
        self.structure = structure
        self.content = content
        self.format = fmt
        self._api_version = (api_version or os.environ.get("CONFLUENCE_API_VERSION", "v2")).lower()
        if self._api_version not in {"v1", "v2"}:
            raise ValueError("CONFLUENCE_API_VERSION must be 'v1' or 'v2'")

        self._base_url = (base_url or os.environ.get("CONFLUENCE_URL", "")).rstrip("/")
        self._user = user if user is not None else os.environ.get("CONFLUENCE_USER", "")
        self._token = token or os.environ.get("CONFLUENCE_TOKEN", "")

        if not self._base_url:
            raise ValueError(
                "Confluence URL required. Set via:\n"
                "  export CONFLUENCE_URL=https://company.atlassian.net"
            )
        if not self._token:
            raise ValueError(
                "Confluence API token required. Set via:\n"
                "  export CONFLUENCE_TOKEN=<api_token>"
            )

        headers = {"Accept": "application/json"}
        if not self._user:
            headers["Authorization"] = f"Bearer {self._token}"
        api_base = self._base_url
        if self._api_version == "v2" and not api_base.endswith("/wiki"):
            api_base += "/wiki"
        self._http = httpx.Client(
            base_url=api_base,
            auth=(self._user, self._token) if self._user else None,
            headers=headers,
            timeout=60.0,
        )

        # Resolve space key to numeric ID (v2 API requires ID).
        if self._api_version == "v2" and not self.space_key.isdecimal():
            try:
                resp = self._http.get(
                    "/api/v2/spaces", params={"keys": [self.space_key]}
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                matches = [
                    space
                    for space in results
                    if space.get("key", "").casefold() == self.space_key.casefold()
                ]
                if len(matches) != 1:
                    raise ValueError(f"Confluence space '{self.space_key}' not found")
                self.space_key = str(matches[0]["id"])
            except Exception:
                self._http.close()
                raise

        # display_path -> (kind, id). Kind selects the read_file endpoint.
        self._content_cache: dict[str, tuple[str, str]] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """List pages and/or blog posts in the space and build a manifest."""
        self._content_cache.clear()
        records: list[tuple[str, dict[str, Any]]] = []
        if self.content in {"pages", "all"}:
            records.extend(("page", page) for page in self._list_content("page"))
        if self.content in {"blogs", "all"}:
            records.extend(("blogpost", post) for post in self._list_content("blogpost"))

        pages_by_id = {
            str(raw["id"]): raw
            for kind, raw in records
            if kind == "page" and raw.get("id") is not None
        }
        built: list[tuple[str, dict[str, Any], ManifestEntry]] = []
        for kind, raw in records:
            try:
                built.append((kind, raw, self._entry(kind, raw, pages_by_id)))
            except (KeyError, TypeError) as exc:
                log.warning(
                    "Skipping malformed Confluence %s record: %s: %r",
                    "page" if kind == "page" else "blog post",
                    exc,
                    raw,
                )

        entries = [entry for _, _, entry in built]
        counts = Counter(entry.display_path for entry in entries)
        reserved = set(counts)
        for index, (kind, raw, entry) in enumerate(built):
            if counts[entry.display_path] > 1:
                # Rename every colliding title so API ordering cannot change identity.
                stem, ext = os.path.splitext(entry.filename)
                stem = f"{stem}_{raw['id']}"
                candidate = replace(entry, filename=f"{stem}{ext}")
                while candidate.display_path in reserved:
                    stem += "_"
                    candidate = replace(entry, filename=f"{stem}{ext}")
                entry = candidate
                reserved.add(entry.display_path)
            entries[index] = entry
            self._content_cache[entry.display_path] = (kind, str(raw["id"]))
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _list_content(self, kind: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 250}
        if self._api_version == "v1":
            endpoint = "/rest/api/content"
            expand = "ancestors,version" if kind == "page" else "history,version"
            params.update(
                spaceKey=self.space_key,
                type="page" if kind == "page" else "blogpost",
                start=0,
                expand=expand,
            )
        elif kind == "page":
            endpoint = f"/api/v2/spaces/{self.space_key}/pages"
        else:
            endpoint = f"/api/v2/spaces/{self.space_key}/blogposts"

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        label = "page" if kind == "page" else "blog post"
        while True:
            resp = self._http.get(endpoint, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data["results"]
            for item in results:
                item_id = str(item["id"])
                if item_id in seen:
                    raise ValueError(f"Repeated Confluence {label} in pagination: {item_id}")
                seen.add(item_id)
            items.extend(results)

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # Use only pagination parameters, never a server-provided host/path.
            parameter = "start" if self._api_version == "v1" else "cursor"
            next_value = dict(parse_qsl(urlsplit(next_link).query)).get(parameter)
            if not results or not next_value or str(params.get(parameter)) == next_value:
                raise ValueError("Invalid Confluence pagination link")
            if parameter == "start" and int(next_value) <= int(params["start"]):
                raise ValueError("Confluence pagination did not advance")
            params[parameter] = next_value
        return items

    def _entry(
        self,
        kind: str,
        item: dict[str, Any],
        pages_by_id: dict[str, dict[str, Any]],
    ) -> ManifestEntry:
        item_id = str(item["id"])
        title = item["title"]
        version = item.get("version", {}).get("number", 0)
        checksum = hashlib.sha256(f"{item_id}:v{version}".encode()).hexdigest()[:16]
        ext = ".md" if self.format == "markdown" else ".txt"
        filename = self._safe_name(title) + ext
        if kind == "page":
            path = self._page_path(item, pages_by_id)
            if self.content == "all":
                path = f"Pages/{path}" if path else "Pages"
        else:
            path = self._blog_path(item)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _blog_path(self, post: dict[str, Any]) -> str:
        if self.structure != "hierarchical":
            return "Blogs"
        return f"Blogs/{_created_year(post)}"

    def _page_path(
        self, page: dict[str, Any], pages_by_id: dict[str, dict[str, Any]]
    ) -> str:
        if self.structure != "hierarchical":
            return ""
        if self._api_version == "v1":
            return "/".join(self._safe_name(a.get("title")) for a in page.get("ancestors", []))

        ancestors: list[str] = []
        parent_id = page.get("parentId")
        seen: set[str] = set()
        while parent_id:
            parent_id = str(parent_id)
            if parent_id in seen:
                raise ValueError(f"Circular Confluence page hierarchy at page {page['id']}")
            seen.add(parent_id)
            parent = pages_by_id.get(parent_id)
            if not parent:
                break
            ancestors.append(self._safe_name(parent.get("title")))
            parent_id = parent.get("parentId")
        return "/".join(reversed(ancestors))

    @staticmethod
    def _safe_name(name: str | None) -> str:
        safe = re.sub(r'[<>:"/\\|?*]', "_", name or "Untitled").strip()
        return safe or "Untitled"

    @staticmethod
    def _entry_key(path: str, filename: str) -> str:
        return f"{path}/{filename}" if path else filename

    def read_file(self, path: str, filename: str) -> bytes:
        """Fetch a page or blog post and return text or Markdown bytes."""
        cached = self._content_cache.get(self._entry_key(path, filename))
        if not cached:
            raise FileNotFoundError(f"Page not found: {filename}")
        kind, item_id = cached

        if self._api_version == "v1":
            resp = self._http.get(f"/rest/api/content/{item_id}", params={"expand": "body.storage"})
        else:
            collection = "pages" if kind == "page" else "blogposts"
            resp = self._http.get(
                f"/api/v2/{collection}/{item_id}", params={"body-format": "storage"}
            )
        resp.raise_for_status()
        data = resp.json()

        storage = data.get("body", {}).get("storage", {}).get("value", "")
        text = (
            _storage_to_markdown(storage)
            if self.format == "markdown"
            else _storage_to_text(storage)
        )
        if not text:
            # Open WebUI extracts text as part of POST /files/ and answers 400
            # for a file it can get nothing out of, so uploading this would fail
            # the page on every run for as long as it exists.
            raise SourceFileUnavailable(
                "page has no text to sync (blank, or only a macro such as a "
                "children index)"
            )
        return text.encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_confluence_source(source: str) -> dict[str, str | None]:
    """Parse a confluence:SPACEKEY source string.

    Examples:
        confluence:ENG
        confluence:https://company.atlassian.net/ENG
        confluence:ENG?structure=hierarchical
        confluence:ENG?content=all&format=markdown
    """
    source = source.removeprefix("confluence:")
    is_url = source.startswith(("http://", "https://"))
    parsed = urlsplit(source if is_url else f"confluence://{source}")
    base_path = ""
    space_key = parsed.netloc
    if is_url:
        base_path, _, space_key = parsed.path.rstrip("/").rpartition("/")
    if not space_key or (not is_url and parsed.path):
        raise ValueError("Invalid Confluence source. Expected: confluence:SPACEKEY")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    unknown = set(params) - {"structure", "content", "format"}
    if unknown:
        raise ValueError(f"Invalid Confluence source. Unknown parameter: {min(unknown)}")
    structure = params.get("structure", "flat")
    if structure not in {"flat", "hierarchical"}:
        raise ValueError("Invalid Confluence source. Expected structure=flat or structure=hierarchical")
    content = params.get("content", "pages")
    if content not in {"pages", "blogs", "all"}:
        raise ValueError(
            "Invalid Confluence source. Expected content=pages, content=blogs, or content=all"
        )
    fmt = params.get("format", "text")
    if fmt not in {"text", "markdown"}:
        raise ValueError("Invalid Confluence source. Expected format=text or format=markdown")

    return {
        "base_url": f"{parsed.scheme}://{parsed.netloc}{base_path}" if is_url else None,
        "space_key": space_key,
        "structure": structure,
        "content": content,
        "format": fmt,
    }
