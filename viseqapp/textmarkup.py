"""Pango markup toolkit for the Text window (e59s01).

Pure helpers — no dpg, no state, no I/O (HIGH-1): every DearPyGui call stays in
the main-thread composition root, so this module is unit-testable headless and
safe to import anywhere.

Why escaping matters: vimix's ``TextSource`` renders its ``contents`` string
through GStreamer's ``textoverlay``, whose ``text`` property is handed straight
to ``pango_layout_set_markup`` (gstbasetextoverlay.c). ``&``, ``<`` and ``>``
are therefore markup metacharacters, and plain text must be escaped before it
is sent or it is silently consumed as markup.

Why an XML probe: Pango markup is a subset of XML (GMarkup), so
``xml.etree.ElementTree`` is a dependency-free structural check that catches the
common authoring errors (unbalanced tags, bare ``&``). It is deliberately NOT a
full Pango parser (no GTK/Pango bindings are available in the venv).
"""

import re
from dataclasses import dataclass
from xml.etree import ElementTree

# The Pango/GMarkup metacharacters, ampersand FIRST so a literal entity is not
# double-escaped (`&lt;` -> `&amp;lt;`, which renders as the literal `&lt;`).
PLAIN_TEXT_ESCAPES: tuple[tuple[str, str], ...] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
)
_UNESCAPE_PATTERN = re.compile(r"&(amp|lt|gt);")
_UNESCAPE_BY_NAME: dict[str, str] = {"amp": "&", "lt": "<", "gt": ">"}

# The wrapper used for the structural check. A short element name keeps the
# reported column of a line-1 error one small constant away from the document's
# own coordinate; that constant is subtracted back in `validate_pango_markup`.
_MARKUP_PROBE_OPEN = "<markup>"
_MARKUP_PROBE_CLOSE = "</markup>"
_MARKUP_PROBE_PREFIX_LEN = len(_MARKUP_PROBE_OPEN)

# Pango's keyword sizes (https://docs.gtk.org/Pango/pango_markup.html).
PANGO_SIZE_KEYWORDS: tuple[str, ...] = (
    "xx-small",
    "x-small",
    "small",
    "medium",
    "large",
    "x-large",
    "xx-large",
)
MARKUP_SAMPLE_TEXT = "text"
MARKUP_SPAN_CLOSE = "</span>"


@dataclass(frozen=True)
class MarkupIssue:
    """One structural problem found in a Pango markup document."""

    line: int
    column: int
    message: str


@dataclass(frozen=True)
class MarkupTemplate:
    """A one-click Pango tag template (label is the button text)."""

    key: str
    label: str
    open_tag: str
    close_tag: str


BASE_MARKUP_TEMPLATES: tuple[MarkupTemplate, ...] = (
    MarkupTemplate("bold", "B", "<b>", "</b>"),
    MarkupTemplate("italic", "I", "<i>", "</i>"),
    MarkupTemplate("underline", "U", "<u>", "</u>"),
    MarkupTemplate("strikethrough", "S", "<s>", "</s>"),
)


def escape_pango_text(text: str) -> str:
    """Escape ``&``, ``<`` and ``>`` so plain text renders literally."""
    escaped = text
    for plain, entity in PLAIN_TEXT_ESCAPES:
        escaped = escaped.replace(plain, entity)
    return escaped


def unescape_pango_text(text: str) -> str:
    """Reverse :func:`escape_pango_text`; other entities are left untouched."""
    return _UNESCAPE_PATTERN.sub(lambda match: _UNESCAPE_BY_NAME[match.group(1)], text)


def validate_pango_markup(text: str) -> MarkupIssue | None:
    """``None`` when ``text`` is well-formed Pango markup, else the first issue.

    Empty text is valid (vimix's wiki: "text can be empty").
    """
    if not text.strip():
        return None
    probe = f"{_MARKUP_PROBE_OPEN}{text}{_MARKUP_PROBE_CLOSE}"
    try:
        ElementTree.fromstring(probe)
    except ElementTree.ParseError as exc:
        return _issue_from_parse_error(exc)
    return None


def _issue_from_parse_error(exc: ElementTree.ParseError) -> MarkupIssue:
    """Convert an ElementTree error into a document-relative issue."""
    line, column = exc.position
    if line == 1:
        column = max(1, column - _MARKUP_PROBE_PREFIX_LEN)
    return MarkupIssue(line, max(1, column), str(exc))


def format_markup_issue(issue: MarkupIssue) -> str:
    """One-line status text for the Text window (line/column + reason)."""
    return f"Markup issue at line {issue.line}, column {issue.column}: {issue.message}"


def append_snippet(document: str, snippet: str) -> str:
    """Append ``snippet`` to the document, on its own line when there is text.

    DearPyGui 2.3.1 exposes no caret/selection API for an input_text, so the
    palette's insertion point is the end of the document — see e59s02.
    """
    if not document:
        return snippet
    separator = "" if document.endswith("\n") else "\n"
    return f"{document}{separator}{snippet}"


def wrap_markup(sample: str, open_tag: str, close_tag: str) -> str:
    """A complete, visible sample wrapped in a tag pair."""
    return f"{open_tag}{sample}{close_tag}"


def insert_at(document: str, position: int, snippet: str) -> str:
    """Insert ``snippet`` at ``position`` (clamped to the document bounds)."""
    index = max(0, min(int(position), len(document)))
    return f"{document[:index]}{snippet}{document[index:]}"


@dataclass(frozen=True)
class MarkupEdit:
    """A document edit: the new text and where the caret should sit after it."""

    document: str
    caret: int


def wrap_range(
    document: str, selection: tuple[int, int], open_tag: str, close_tag: str
) -> MarkupEdit:
    """Wrap ``document[start:end]`` in a tag pair (e59s05).

    The range is normalised and clamped to the document, so an inverted or
    out-of-bounds selection can never raise; the returned caret sits after the
    replacement.
    """
    start, end = sorted(selection)
    start = max(0, min(int(start), len(document)))
    end = max(0, min(int(end), len(document)))
    replacement = f"{open_tag}{document[start:end]}{close_tag}"
    return MarkupEdit(f"{document[:start]}{replacement}{document[end:]}", start + len(replacement))


def infer_edit_caret(previous: str, current: str) -> int | None:
    """Index where an edit happened, inferred from two consecutive values.

    DearPyGui 2.3.1 exposes no caret for an input_text (upstream issue #1650),
    so the Text window tracks the caret by diffing consecutive editor values:
    the longest common prefix and suffix bracket the change, and the caret sits
    after the inserted text (a pure deletion leaves it at the deletion point).
    ``None`` means "no text change": the caller keeps its last known caret, so a
    caret move with no typing is invisible to this method (documented limit).
    """
    if previous == current:
        return None
    shared = min(len(previous), len(current))
    prefix = 0
    while prefix < shared and previous[prefix] == current[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < shared - prefix
        and previous[len(previous) - 1 - suffix] == current[len(current) - 1 - suffix]
    ):
        suffix += 1
    inserted = len(current) - prefix - suffix
    return prefix + inserted


def insert_template(
    document: str, template: MarkupTemplate, sample: str = MARKUP_SAMPLE_TEXT
) -> str:
    """Append a template's complete sample to the document."""
    return append_snippet(document, wrap_markup(sample, template.open_tag, template.close_tag))


def span_tag(attribute: str, value: str) -> tuple[str, str]:
    """The ``<span attribute="value">`` open tag and its closing tag."""
    return (f'<span {attribute}="{value}">', MARKUP_SPAN_CLOSE)


def insert_span(
    document: str,
    attribute: str,
    value: str,
    sample: str = MARKUP_SAMPLE_TEXT,
) -> str:
    """Append a complete ``<span>`` sample for one attribute (colour, size…)."""
    open_tag, close_tag = span_tag(attribute, value)
    return append_snippet(document, wrap_markup(sample, open_tag, close_tag))


def pango_color(red: int, green: int, blue: int) -> str:
    """A ``#rrggbb`` string from three 0..255 channels (each clamped)."""
    return f"#{_clamp_channel(red):02x}{_clamp_channel(green):02x}{_clamp_channel(blue):02x}"


def _clamp_channel(value: int) -> int:
    return max(0, min(255, int(value)))
