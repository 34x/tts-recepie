#!/usr/bin/env python3
"""Extensible text-preprocessing pipeline for Piper TTS.

Runs a chain of small, independent cleanup steps before text reaches the
synthesizer. The intent is to remove markdown / HTML syntax so the voice
does not read it literally ("star star", "hash", "backtick", ...).

Each step is a plain function `(text: str) -> str`. Steps are listed in
PIPELINE in the order they run. Order matters: structural removals
(fenced code, tables) happen before inline token stripping.

Extending the pipeline is trivial:

1. Write a `def foo(text: str) -> str` function in this module.
2. Append `("foo", foo)` to PIPELINE.
3. (Optional) it is automatically controlled by the same env on/off
   mechanism; see _is_enabled().

Each step can be toggled independently via environment variables:

    PIPER_CLEAN=0            # disable the whole pipeline (default: 1/enabled)
    PIPER_CLEAN_MARKDOWN=0   # disable only the markdown group
    PIPER_CLEAN_HTML=0       # disable only the html group
    PIPER_CLEAN_WHITESPACE=0 # disable only the whitespace group
    # ... any step can be toggled by prefixing its name with PIPER_CLEAN_

A step is run only if *both* the pipeline is enabled AND the step itself
is enabled.
"""

import os
import re

# --------------------------------------------------------------------------
# Step helpers
# --------------------------------------------------------------------------

# Multi-line fenced code blocks: ``` ... ``` or ~~~ ... ~~~, with optional
# language tag. Dropped entirely -- code is not meant to be read aloud.
_FENCED_CODE = re.compile(
    r"^\s*```[^\n]*\n.*?^\s*```\s*$",
    flags=re.MULTILINE | re.DOTALL,
)
_FENCED_CODE_TILDE = re.compile(
    r"^\s*~~~[^\n]*\n.*?^\s*~~~\s*$",
    flags=re.MULTILINE | re.DOTALL,
)

# Inline code spans: `code`
_INLINE_CODE = re.compile(r"`[^`\n]*`")

# Markdown images: ![alt](url)  ->  alt
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# Markdown links: [text](url)  ->  text
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Bare URLs / angle-URLs <https://...>  ->  drop (or keep domain later)
_BARE_URL = re.compile(r"\b(?:https?://|ftp://)[^\s<>]+", flags=re.IGNORECASE)
_ANGLE_URL = re.compile(r"<[^<>]*://[^<>]*>")

# Headings: "# " .. "###### "
_HEADING = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)

# List markers at line start: "-", "*", "+", or "1." / "1)" etc.
_LIST_UNORDERED = re.compile(r"^\s*[-*+]\s+", flags=re.MULTILINE)
_LIST_ORDERED = re.compile(r"^\s*\d+[.)]\s+", flags=re.MULTILINE)
# Blockquote: "> "
_BLOCKQUOTE = re.compile(r"^\s*>\s?", flags=re.MULTILINE)
# Horizontal rule: --- / *** / ___ on a line by itself
_HRULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", flags=re.MULTILINE)

# Emphasis / bold / italic markers (asterisk, underscore).
# - Runs of 2+ (`**`, `***`, `__`, `___`) are always markdown emphasis.
# - A single marker is emphasis only at a word boundary; a single `_` or `*`
#   between word chars (snake_case, e.g. `variable_name`, or math `a*b`) is
#   preserved.
_MARKER_RUN = re.compile(r"\*{2,}|_{2,}")          # bold / strong / combined
_MARKER_SINGLE = re.compile(r"(?<!\w)[*_]|[*_](?!\w)")  # italic, word-bounded

# Table pipes and separator rows (rows made only of -, :, | and spaces).
_TABLE_PIPE = re.compile(r"\|")
_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", flags=re.MULTILINE
)

# Reasoning / thinking blocks: <thinking>...</thinking>, <|think|>...</|think|>,
# <|thinking|>...</|thinking|>. The whole block (including its content) is
# dropped -- it is reasoning, not speech.
_THINK_TOKEN = re.compile(
    r"</?\s*(?:thinking>|\|\s*(?:think|thinking)\s*\|>)",
    flags=re.IGNORECASE,
)
# Simple HTML tags: <tag> or </tag> (conservative: requires a letter after <).
_HTML_TAG = re.compile(r"<[/]?[a-zA-Z][^>]*>")
# Escaped newlines / indentation-injected soft breaks.
_NEWLINE = re.compile(r"\n{3,}")

# Collapse runs of whitespace (excluding single newlines, to keep sentence
# structure intact).
_WS = re.compile(r"[ \t\u00a0\u2000-\u200a]+")

# Smart-quote / typographic helpers (optional).
_TYPO = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": " - ",
}


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def strip_code_blocks(text: str) -> str:
    """Remove fenced (``` / ~~~) and inline (`) code."""
    text = _FENCED_CODE.sub("", text)
    text = _FENCED_CODE_TILDE.sub("", text)
    text = _INLINE_CODE.sub("", text)
    return text


def strip_links(text: str) -> str:
    """Turn markdown links/images into plain text; drop bare URLs."""
    text = _MD_IMAGE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _ANGLE_URL.sub("", text)
    text = _BARE_URL.sub("", text)
    return text


def strip_headings(text: str) -> str:
    return _HEADING.sub("", text)


def strip_lists(text: str) -> str:
    text = _LIST_UNORDERED.sub("", text)
    text = _LIST_ORDERED.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _HRULE.sub("", text)
    return text


def strip_tables(text: str) -> str:
    text = _TABLE_SEPARATOR.sub("", text)
    text = _TABLE_PIPE.sub(" ", text)
    return text


def strip_emphasis(text: str) -> str:
    # Runs of 2+ are always emphasis; strip all of them.
    text = _MARKER_RUN.sub("", text)
    # Single markers only at word boundaries (so snake_case / math survive).
    text = _MARKER_SINGLE.sub("", text)
    return text


def strip_think_blocks(text: str) -> str:
    """Remove reasoning/thinking blocks entirely (tags and content).

    Matches the opening token and everything up to the matching closing
    token, then drops the whole span. Handles the common variants:
    <thinking>...</thinking>, <|think|>...</|think|>,
    <|thinking|>...</|thinking|>. Reasoner text is not speech, so the
    content is dropped, not read aloud.
    """
    # Paired block: opening token, then any content, up to the closing token.
    # Handles both HTML-style (<thinking>) and gemma/qwen pipe-style
    # (<|think|>, <|thinking|>), with their matching closers.
    open_tok = r"<(?:thinking>|\|\s*(?:think|thinking)\s*\|>)"
    close_tok = r"</(?:thinking>|\|\s*(?:think|thinking)\s*\|>)"
    block = re.compile(
        open_tok + r".*?" + close_tok,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = block.sub("", text)
    # Any orphaned single tag token left behind.
    text = _THINK_TOKEN.sub("", text)
    return text


def strip_html(text: str) -> str:
    return _HTML_TAG.sub("", text)


def normalize_entities(text: str) -> str:
    """Resolve common HTML entities and smart punctuation."""
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    for k, v in _TYPO.items():
        text = text.replace(k, v)
    return text


def collapse_whitespace(text: str) -> str:
    """Collapse repeated blank lines and runs of spaces (keep newlines)."""
    text = _NEWLINE.sub("\n\n", text)
    text = _WS.sub(" ", text)
    return text.strip()


# --------------------------------------------------------------------------
# Pipeline definition
# --------------------------------------------------------------------------

# Ordered pipeline. `name` is used for the PIPER_CLEAN_<NAME> env toggle and
# for the optional /health report. Add new steps here (see module docstring).
PIPELINE: list[tuple[str, callable]] = [
    ("code_blocks", strip_code_blocks),
    ("think_blocks", strip_think_blocks),
    ("links", strip_links),
    ("headings", strip_headings),
    ("lists", strip_lists),
    ("tables", strip_tables),
    ("emphasis", strip_emphasis),
    ("html", strip_html),
    ("entities", normalize_entities),
    ("whitespace", collapse_whitespace),
]

# Master switch. Disable everything with PIPER_CLEAN=0.
_ENABLED = os.environ.get("PIPER_CLEAN", "1") not in ("0", "false", "off")


def _is_enabled(name: str) -> bool:
    """A step runs unless PIPER_CLEAN_<NAME> is explicitly set to 0."""
    if not _ENABLED:
        return False
    val = os.environ.get(f"PIPER_CLEAN_{name.upper()}")
    if val is None:
        return True
    return val not in ("0", "false", "off")


def preprocess(text: str) -> str:
    """Run the configured cleanup pipeline over `text`."""
    if not text:
        return text
    for name, fn in PIPELINE:
        if _is_enabled(name):
            text = fn(text)
    return text


def active_steps() -> list[str]:
    """Names of steps that will actually run (for logging/diagnostics)."""
    return [name for name, _ in PIPELINE if _is_enabled(name)]
