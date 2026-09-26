"""Markup-aware progressive text reveal for the Text window (e59s08).

`/vimix/<target>/contents` replaces the WHOLE text of a source, so a progressive
reveal is a client-side sequence of full-string sends. Given a document and a
mode, `steps()` returns the ordered strings to send:

- **1 word** (`word`): each word alone, carrying the tags that enclose it (a word
  inside ``<b>`` arrives bold);
- **1 word +** (`word_plus`): the line prefix up to each word;
- **1 line** (`line`): each line alone (a blank line sends an empty string);
- **1 line +** (`line_plus`): the paragraph prefix up to each line, resetting at a
  blank line (which sends an empty string and closes the paragraph).

Every fragment is tag-balanced (whatever is still open at the cut is closed), so
the engine never emits broken Pango markup. Pure, dpg-free, state-free: safe to
import and unit-test headless (HIGH-1). See
``specs/TEXT_SEND_MODES_SURVEY_LATEST.md`` and
``specs/epics/e59-text-window/e59s08-text-reveal-modes.md``.
"""

import re

MODE_WORD = "word"
MODE_WORD_PLUS = "word_plus"
MODE_LINE = "line"
MODE_LINE_PLUS = "line_plus"

# (mode, button label); the order is the button order in the Text window row.
REVEAL_MODES: tuple[tuple[str, str], ...] = (
    (MODE_WORD, "1 word"),
    (MODE_WORD_PLUS, "1 word +"),
    (MODE_LINE, "1 line"),
    (MODE_LINE_PLUS, "1 line +"),
)
REVEAL_LABELS: dict[str, str] = dict(REVEAL_MODES)
DEFAULT_REVEAL_MODE = MODE_LINE

# e60s01: the persisted Step Sequencer step tokens, 1:1 with the modes. The
# mapping lives beside the mode authority so the two cannot drift; the UI labels
# stay the REVEAL_MODES ones. Persisted in a step's `type` key (STEP_PERSISTED_KEYS).
STEP_TOKEN_BY_MODE: dict[str, str] = {
    MODE_WORD: "TextWord",
    MODE_WORD_PLUS: "TextWordPlus",
    MODE_LINE: "TextLine",
    MODE_LINE_PLUS: "TextLinePlus",
}
MODE_BY_STEP_TOKEN: dict[str, str] = {token: mode for mode, token in STEP_TOKEN_BY_MODE.items()}
TEXT_STEP_TOKENS: tuple[str, ...] = tuple(STEP_TOKEN_BY_MODE[mode] for mode, _label in REVEAL_MODES)


def step_label(token: str) -> str:
    """The UI label of a step type token: the reveal label, else the token itself."""
    mode = MODE_BY_STEP_TOKEN.get(token)
    return REVEAL_LABELS[mode] if mode is not None else token


# A Pango markup tag: `<name …>`, `</name>`. Text content never contains a raw
# `<` in valid markup, so a simple scan is enough.
_TAG_PATTERN = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>")


def tag_name(tag: str) -> str:
    """The element name of an open/close tag (`<span …>` -> `span`)."""
    match = _TAG_PATTERN.match(tag)
    return match.group(1) if match else ""


def open_tags_at(text: str, position: int) -> tuple[str, ...]:
    """The open tags active just before ``position`` (their raw open forms)."""
    stack: list[str] = []
    for match in _TAG_PATTERN.finditer(text):
        if match.end() > position:
            break
        raw = match.group(0)
        if raw.startswith("</"):
            name = match.group(1)
            for index in range(len(stack) - 1, -1, -1):
                if tag_name(stack[index]) == name:
                    del stack[index]
                    break
        else:
            stack.append(raw)
    return tuple(stack)


def closing_tags(open_tags: tuple[str, ...]) -> str:
    """The closing tags for an open-tag stack, innermost first."""
    return "".join(f"</{tag_name(tag)}>" for tag in reversed(open_tags))


def balance(fragment: str) -> str:
    """Close whatever is still open at the end of ``fragment``."""
    return f"{fragment}{closing_tags(open_tags_at(fragment, len(fragment)))}"


def _tag_mask(line: str) -> list[bool]:
    """True for every character that belongs to a tag (whitespace in it is inert)."""
    mask = [False] * len(line)
    for match in _TAG_PATTERN.finditer(line):
        for index in range(match.start(), match.end()):
            mask[index] = True
    return mask


def word_spans(line: str) -> list[tuple[int, int]]:
    """The (start, end) spans of the words in a line, tags excluded from splitting.

    A tag never starts or ends a word, so `he<b>ll</b>o` is ONE word while
    `a <b>b</b>` is two.
    """
    mask = _tag_mask(line)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, char in enumerate(line):
        if mask[index]:
            continue
        if char.isspace():
            if start is not None:
                spans.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        spans.append((start, len(line)))
    return spans


def word_unit(line: str, span: tuple[int, int]) -> str:
    """One word alone, wrapped in the tags that enclose it (markup-safe)."""
    start, end = span
    prefix = "".join(open_tags_at(line, start))
    suffix = closing_tags(open_tags_at(line, end))
    return f"{prefix}{line[start:end]}{suffix}"


def steps(document: str, mode: str) -> list[str]:
    """The ordered contents strings to send for a mode (empty list if unknown)."""
    lines = document.split("\n")
    if mode == MODE_WORD:
        return [word_unit(line, span) for line in lines for span in word_spans(line)]
    if mode == MODE_WORD_PLUS:
        return [balance(line[: span[1]]) for line in lines for span in word_spans(line)]
    if mode == MODE_LINE:
        return [balance(line) for line in lines]
    if mode == MODE_LINE_PLUS:
        return _paragraph_steps(lines)
    return []


def _paragraph_steps(lines: list[str]) -> list[str]:
    """Accumulate lines within a paragraph; a blank line sends "" and resets."""
    result: list[str] = []
    accumulated: list[str] = []
    for line in lines:
        if not line.strip():
            accumulated = []
            result.append("")
            continue
        accumulated.append(line)
        joined = "\n".join(accumulated)
        result.append(balance(joined))
    return result
