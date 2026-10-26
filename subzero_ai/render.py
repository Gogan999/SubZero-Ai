"""Markdown to safe HTML. Raw HTML in messages is never passed through.

Interactive explainers are the exception that proves the rule: a fenced block tagged `explainer` holds a
whole web page, which is shown only inside a sandboxed frame served with its own locked-down policy."""

from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

_FORMATTER = HtmlFormatter(nowrap=True)

EXPLAINER_TAG = "explainer"
EXPLAINER_MAX_BYTES = 500 * 1024

# What an explainer page may do: run its own scripts and styles, and load libraries from two CDNs.
# No requests to anywhere else (connect-src 'none'), and the sandbox gives it an origin of its own,
# so it can't read the app's cookies or act as the student, even when opened in its own tab.
EXPLAINER_CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net"
EXPLAINER_POLICY = (
    "default-src 'none'; "
    f"script-src 'unsafe-inline' 'unsafe-eval' {EXPLAINER_CDNS}; "
    f"style-src 'unsafe-inline' {EXPLAINER_CDNS}; "
    f"font-src data: {EXPLAINER_CDNS}; "
    "img-src data: blob:; media-src data: blob:; worker-src blob:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'"
)
EXPLAINER_CSP = EXPLAINER_POLICY + "; frame-ancestors 'self'; sandbox allow-scripts"


def _highlight(code: str, lang: str) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, _FORMATTER)


def _fence_lang(token) -> str:
    return (token.info or "").strip().split()[0].lower() if (token.info or "").strip() else ""


def _code_html(code: str, lang: str, extra: str = "") -> str:
    label = html.escape(lang or "text")
    return (
        f'<div class="code"{extra}><div class="code-bar"><span>' + label + "</span>"
        '<button type="button" class="copy" data-copy>Copy</button></div>'
        "<pre><code>" + _highlight(code, lang) + "</code></pre></div>\n"
    )


def _explainer_html(code: str, url: str) -> str:
    src = html.escape(url, quote=True)
    return (
        '<figure class="explainer" data-explainer>'
        '<div class="explainer-bar"><span class="explainer-label">Interactive explainer</span>'
        '<span class="explainer-actions">'
        '<button type="button" data-fullscreen>Full screen</button>'
        f'<a href="{src}?download=1" download>Download</a>'
        '<button type="button" data-view-code aria-expanded="false">View code</button>'
        "</span></div>"
        f'<iframe src="{src}" sandbox="allow-scripts" title="Interactive explainer" loading="lazy" '
        'referrerpolicy="no-referrer"></iframe>'
        + _code_html(code, "html", " hidden data-explainer-code")
        + "</figure>\n"
    )


def _render_fence(self, tokens, idx, options, env) -> str:
    token = tokens[idx]
    lang = _fence_lang(token)
    if lang == EXPLAINER_TAG:
        index = env.setdefault("explainer_index", 0)
        env["explainer_index"] = index + 1
        base = env.get("explainer_base")
        if base and len(token.content.encode("utf-8")) <= EXPLAINER_MAX_BYTES:
            return _explainer_html(token.content, f"{base}/{index}")
        note = ""
        if base:
            note = '<p class="fine">This explainer is over 500 KB, so it\'s shown as code instead.</p>\n'
        return note + _code_html(token.content, "html")
    return _code_html(token.content, lang)


def _render_code_block(self, tokens, idx, options, env) -> str:
    return _render_fence(self, tokens, idx, options, env)


def _render_link_open(self, tokens, idx, options, env) -> str:
    token = tokens[idx]
    token.attrSet("target", "_blank")
    token.attrSet("rel", "noopener noreferrer nofollow")
    return self.renderToken(tokens, idx, options, env)


def _render_image(self, tokens, idx, options, env) -> str:
    # Show images as links so a reply can't load outside images into a student's browser.
    token = tokens[idx]
    src = html.escape(token.attrGet("src") or "", quote=True)
    alt = html.escape(self.renderInlineAsText(token.children or [], options, env) or "image")
    return f'<a href="{src}" target="_blank" rel="noopener noreferrer nofollow">{alt}</a>'


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.add_render_rule("fence", _render_fence)
    md.add_render_rule("code_block", _render_code_block)
    md.add_render_rule("link_open", _render_link_open)
    md.add_render_rule("image", _render_image)
    return md


_MD = _build()


def markdown_html(text: str, *, explainer_base: str | None = None) -> str:
    """explainer_base is the URL that explainer N in this text is served under, as base/N.
    Without it, explainers are shown as code."""
    return _MD.render(text or "", {"explainer_base": explainer_base})


def explainers(text: str) -> list[str]:
    """The explainer pages in a reply, numbered in the same order markdown_html numbers them."""
    return [t.content for t in _MD.parse(text or "") if t.type == "fence" and _fence_lang(t) == EXPLAINER_TAG]


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def explainer_title(page: str) -> str:
    """A name for an explainer in the Artifacts gallery: its <title>, else its first heading. Plain text,
    so the template escapes it like any other text. Empty when the page has neither."""
    for pattern in (_TITLE_RE, _H1_RE):
        match = pattern.search(page or "")
        if match:
            text = " ".join(html.unescape(_TAG_RE.sub("", match.group(1))).split())
            if text:
                return text[:120]
    return ""


def explainer_figure(page: str, url: str) -> str:
    """The same framed explainer a chat shows, for the Artifacts page."""
    return _explainer_html(page, url)


def explainer_download(page: str) -> str:
    """A downloaded explainer opens straight from the computer, where the frame's policy doesn't apply,
    so the copy carries the same limits in a meta tag."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{html.escape(EXPLAINER_POLICY, quote=True)}">'
    for pattern in (r"<head(?:\s[^>]*)?>", r"<html(?:\s[^>]*)?>", r"<!doctype[^>]*>"):
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return page[:match.end()] + meta + page[match.end():]
    return meta + page


def plain_html(text: str) -> str:
    return html.escape(text or "")


def pygments_css() -> str:
    """Light colors, then dark ones for a dark device (unless the person picked Light) or a picked Dark,
    the same way app.css switches its tokens."""
    light = HtmlFormatter(style="default").get_style_defs(".code pre")
    dark_device = HtmlFormatter(style="monokai").get_style_defs(':root:not([data-theme="light"]) .code pre')
    dark_picked = HtmlFormatter(style="monokai").get_style_defs(':root[data-theme="dark"] .code pre')
    return light + "\n@media (prefers-color-scheme: dark) {\n" + dark_device + "\n}\n" + dark_picked + "\n"
