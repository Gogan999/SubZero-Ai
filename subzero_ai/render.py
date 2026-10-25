"""Markdown to safe HTML. Raw HTML in messages is never passed through."""

from __future__ import annotations

import html

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

_FORMATTER = HtmlFormatter(nowrap=True)


def _highlight(code: str, lang: str) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, _FORMATTER)


def _render_fence(self, tokens, idx, options, env) -> str:
    token = tokens[idx]
    lang = (token.info or "").strip().split()[0] if token.info else ""
    label = html.escape(lang or "text")
    body = _highlight(token.content, lang)
    return (
        '<div class="code"><div class="code-bar"><span>' + label + "</span>"
        '<button type="button" class="copy" data-copy>Copy</button></div>'
        "<pre><code>" + body + "</code></pre></div>\n"
    )


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


def markdown_html(text: str) -> str:
    return _MD.render(text or "")


def plain_html(text: str) -> str:
    return html.escape(text or "")


def pygments_css() -> str:
    light = HtmlFormatter(style="default").get_style_defs(".code pre")
    dark = HtmlFormatter(style="monokai").get_style_defs(".code pre")
    return light + "\n@media (prefers-color-scheme: dark) {\n" + dark + "\n}\n"
