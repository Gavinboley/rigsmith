"""The guides in docs/, as pages the app can open.

The first hardware session, which local model to load, how tone matching
works - written as Markdown beside the code, and until now only readable
there. This renders the small part of Markdown they use (headings,
paragraphs, lists, code, tables, links, bold and italics), escaping
everything first, and serves each at ``/docs/<name>``.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

#: Page name -> file in docs/.
GUIDES = {
    "whats-new": "WHATS_NEW.md",
    "first-session": "FIRST_SESSION.md",
    "autopilot": "AUTOPILOT.md",
    "local-models": "LOCAL_MODELS.md",
    "tone-matching": "TONE_MATCHING.md",
    "reading-notes": "READING_NOTES.md",
    "protocol": "PROTOCOL.md",
}

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _slug_for(target: str) -> str | None:
    name = target.rsplit("/", 1)[-1].split("#", 1)[0]
    return next((slug for slug, file in GUIDES.items() if file == name), None)


def _inline(text: str) -> str:
    """Escape, then mark up code, links, bold and italics.

    Code spans are set aside first and put back last, so their insides stay
    literal and bold can still wrap one (**`headrushcore.local`**).
    """
    codes: list[str] = []

    def keep(match: re.Match) -> str:
        codes.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = html.escape(re.sub(r"`([^`]+)`", keep, text), quote=False)

    def link(match: re.Match) -> str:
        label, target = match.group(1), match.group(2)
        slug = _slug_for(target) if target.endswith(".md") or ".md#" in target else None
        href = f"/docs/{slug}" if slug else target
        if not re.match(r"^(https?://|/|#)", href):
            return label  # a path in the repo: no page to go to
        return f'<a href="{html.escape(href)}">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def render(markdown: str) -> tuple[str, str]:
    """``(title, body html)`` for a guide."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    body: list[str] = []
    title = "Guide"
    paragraph: list[str] = []
    listing: list[str] = []
    kind = ""

    def flush() -> None:
        nonlocal paragraph, listing, kind
        if paragraph:
            body.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph = []
        if listing:
            body.append(f"<{kind}>" + "".join(f"<li>{_inline(item)}</li>" for item in listing) + f"</{kind}>")
            listing = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            body.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
        elif re.match(r"^#{1,4} ", line):
            flush()
            level = len(line) - len(line.lstrip("#"))
            text = line[level:].strip()
            if level == 1 and title == "Guide":
                title = text
            body.append(f"<h{level}>{_inline(text)}</h{level}>")
        elif line.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            i -= 1
            if rows:
                head, *rest = rows
                body.append(
                    "<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                    + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in rest)
                    + "</tbody></table>"
                )
        elif re.match(r"^\s*[-*] ", line) or re.match(r"^\s*\d+\. ", line):
            ordered = bool(re.match(r"^\s*\d+\. ", line))
            if paragraph or (listing and kind != ("ol" if ordered else "ul")):
                flush()
            kind = "ol" if ordered else "ul"
            listing.append(re.sub(r"^\s*([-*]|\d+\.) ", "", line))
        elif listing and line.startswith("  ") and line.strip():
            listing[-1] += " " + line.strip()
        elif not line.strip():
            flush()
        else:
            if listing:
                flush()
            paragraph.append(line.strip())
        i += 1
    flush()
    return title, "\n".join(body)


CSS = """
:root { color-scheme: light dark; --bg: #fbfaf7; --text: #1d1f22; --dim: #5b6168; --line: #ddd8cf; --code: #f0ece4; --accent: #9a5b00; }
@media (prefers-color-scheme: dark) { :root { --bg: #111418; --text: #e6e9ee; --dim: #9aa4b2; --line: #2a313a; --code: #1b2027; --accent: #f0a020; } }
body { background: var(--bg); color: var(--text); font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; }
main { max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 28px; margin: 0 0 8px; } h2 { font-size: 20px; margin: 32px 0 8px; } h3 { font-size: 17px; margin: 24px 0 6px; }
a { color: var(--accent); } p, li { color: var(--text); } .nav { color: var(--dim); font-size: 14px; margin-bottom: 24px; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; font: 14px ui-monospace, "Cascadia Mono", Consolas, monospace; }
pre { background: var(--code); padding: 12px 14px; border-radius: 8px; overflow-x: auto; } pre code { padding: 0; background: none; }
table { border-collapse: collapse; width: 100%; font-size: 14px; margin: 12px 0; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
@media print { .nav { display: none; } }
"""


def page(slug: str) -> str | None:
    """The guide as a whole page, or None when there is no such guide here."""
    file = GUIDES.get(slug)
    if file is None or not (DOCS / file).exists():
        return None
    title, body = render((DOCS / file).read_text(encoding="utf-8"))
    others = " · ".join(f'<a href="/docs/{s}">{html.escape(_title(s))}</a>' for s in GUIDES if s != slug and (DOCS / GUIDES[s]).exists())
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
        f'<div class=nav><a href="/">rigsmith</a> · guides: {others}</div>{body}</main></body></html>'
    )


def _title(slug: str) -> str:
    try:
        first = (DOCS / GUIDES[slug]).read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return slug
    return first.lstrip("# ").strip() or slug
