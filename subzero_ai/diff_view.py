"""A Code session's patch as something a page can draw: one entry per file, each with coloured lines.

The input is what `Workspace.changes()` returns (a list of {"name", "kind", "diff"}, where "diff" is that file's
part of a `git diff` or a plain unified diff). Nothing here is HTML: the template escapes every line. Size limits keep
a patch from making a huge page; the download always has everything."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_FILES = 100  # files drawn; the rest are only counted
MAX_FILE_CHARS = 1_000_000  # one file's diff bigger than this isn't parsed at all
MAX_FILE_LINES = 600  # lines drawn per file
MAX_TOTAL_LINES = 4000  # lines drawn over all files
MAX_LINE_CHARS = 400  # a longer line is cut
OPEN_UP_TO_LINES = 60  # a file with more changed lines than this starts folded

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
SKIP_HEADERS = ("diff --git ", "index ", "--- ", "+++ ", "new file mode", "deleted file mode", "old mode", "new mode",
                "similarity index", "rename from ", "rename to ", "copy from ", "copy to ")


@dataclass
class DiffFile:
    name: str
    kind: str  # added, deleted, changed, renamed, binary or toobig
    label: str  # "New file", "Deleted", "Changed", "Renamed from x", "Binary file", ...
    added: int = 0
    removed: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list)  # (css class, text) with the +/- sign kept in the text
    cut: int = 0  # lines not drawn because of the limits
    folded: bool = False


def parse_file(change: dict[str, str], lines_left: int = MAX_TOTAL_LINES) -> DiffFile:
    """One file's entry. `lines_left` is how many lines the page may still draw over all files."""
    name, kind, text = change["name"], change.get("kind", "changed"), change.get("diff") or ""
    if text.startswith("Binary file "):
        return DiffFile(name, "binary", {"added": "New binary file", "deleted": "Deleted binary file"}.get(
            kind, "Binary file changed") + ". It isn't shown here; the download has it.")
    if len(text) > MAX_FILE_CHARS:
        return DiffFile(name, "toobig", "Too big to show here. The download has it.")
    renamed_from = _header(text, "rename from ")
    label = {"added": "New file", "deleted": "Deleted", "changed": "Changed"}.get(kind, "Changed")
    if renamed_from:
        label, kind = f"Renamed from {renamed_from}", "renamed"
    entry = DiffFile(name, kind, label)
    drawn_limit = min(MAX_FILE_LINES, max(0, lines_left))
    in_hunk = False  # after the first @@ line every +/- line is content, even one that starts with "--- "
    for raw in text.splitlines():
        if HUNK.match(raw):
            css, in_hunk = "hunk", True
        elif not in_hunk and raw.startswith(SKIP_HEADERS):
            continue
        elif raw.startswith("+"):
            css = "add"
        elif raw.startswith("-"):
            css = "del"
        elif raw.startswith("\\"):
            css = "meta"
        elif not raw.strip() and not entry.lines:
            continue
        else:
            css = ""
        if css == "add":
            entry.added += 1
        elif css == "del":
            entry.removed += 1
        if len(entry.lines) < drawn_limit:
            entry.lines.append((css, raw if len(raw) <= MAX_LINE_CHARS else raw[:MAX_LINE_CHARS] + " ..."))
        else:
            entry.cut += 1
    entry.folded = entry.added + entry.removed > OPEN_UP_TO_LINES
    if not entry.lines and kind != "renamed":
        entry.label += ". No lines changed (a permission change, or an empty file)."
    return entry


def _header(text: str, prefix: str) -> str:
    for line in text.split("\n", 12):
        if line.startswith(prefix):
            return line[len(prefix):].strip()[:200]
    return ""


def parse_changes(changes: list[dict[str, str]]) -> tuple[list[DiffFile], int]:
    """The files to draw and how many more there were. Total drawn lines are limited over all files."""
    files: list[DiffFile] = []
    left = MAX_TOTAL_LINES
    for change in changes[:MAX_FILES]:
        entry = parse_file(change, left)
        left -= len(entry.lines)
        files.append(entry)
    return files, max(0, len(changes) - MAX_FILES)


def edit_lines(old: str, new: str, limit: int = 400) -> list[tuple[str, str]]:
    """A short diff for an Edit step: the old text as removed lines, then the new text as added lines."""
    out = [("del", "- " + line) for line in old.splitlines()] + [("add", "+ " + line) for line in new.splitlines()]
    return [(css, text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " ...") for css, text in out[:limit]]
