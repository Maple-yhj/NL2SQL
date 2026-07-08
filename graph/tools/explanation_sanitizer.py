from __future__ import annotations

import re
from typing import Any


_FALSE_PREVIEW_PATTERNS = (
    re.compile(r"\b(?:only|just)\s+(?:the\s+)?(?:first\s+)?\d+\s+(?:rows?|records?)\b", re.IGNORECASE),
    re.compile(r"\b(?:run|execute)\s+(?:the\s+)?original\s+query\b", re.IGNORECASE),
    re.compile(
        r"\b(?:remaining|other)\s+\d+\s+(?:rows?|records?|items?)\s+"
        r"(?:are\s+)?(?:not\s+)?(?:listed|shown|displayed)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:subset|preview|sample)\s+(?:of\s+)?(?:rows?|records?|data)\b", re.IGNORECASE),
    re.compile(r"\b(?:not all|partial)\s+(?:rows?|records?|data)\b", re.IGNORECASE),
    re.compile(r"\b(?:full|complete)\s+(?:list|rows?|records?|data|results?)\b", re.IGNORECASE),
    re.compile(r"\bget\s+(?:the\s+)?(?:full|complete|all)\s+\d+\s+(?:rows?|records?)\b", re.IGNORECASE),
    re.compile(r"\u53ea[\u663e\u5c55]\u793a.*?\u524d\s*\d+\s*[\u884c\u6761]"),
    re.compile(r"\u4ec5[\u663e\u5c55]\u793a.*?\u524d\s*\d+\s*[\u884c\u6761]"),
    re.compile(r"\u53ea[\u663e\u5c55]\u793a.*?\d+\s*[\u884c\u6761]"),
    re.compile(r"\u4ec5[\u663e\u5c55]\u793a.*?\d+\s*[\u884c\u6761]"),
    re.compile(r"\u5b9e\u9645\u5e94\u8fd4\u56de"),
    re.compile(r"\u5176\u4f59\s*\d+\s*(?:\u4e2a|\u884c|\u6761)?.*?\u672a\u5217\u51fa"),
    re.compile(r"\u672a\u5217\u51fa"),
    re.compile(r"\u5982\u9700\u5b8c\u6574"),
    re.compile(r"\u6267\u884c\u539f\u67e5\u8be2"),
    re.compile(r"\u83b7\u53d6\u5168\u90e8\s*\d+\s*[\u884c\u6761]"),
    re.compile(r"\u4ec5\u5217\u51fa"),
    re.compile(r"\u5176\u4f59.*?\u672a.*?\u5c55\u793a"),
    re.compile(r"\u672a\u5728\u7ed3\u679c\u4e2d\u5c55\u793a"),
    re.compile(
        r"\u5b8c\u6574(?:\u7684)?(?:\u524d\s*\d+\s*\u4e2a)?"
        r"(?:\u57ce\u5e02|\u8bb0\u5f55|\u6570\u636e|\u7ed3\u679c)?"
        r"(?:\u5217\u8868|\u6570\u636e|\u8bb0\u5f55|\u7ed3\u679c)"
    ),
)
_LEADING_RANK_LIST_RE = re.compile(
    r"(?:\u4ee5\u4e0b|\u4e0b\u9762).{0,80}(?:\u524d|top)\s*(\d+)",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[^\u3002\uff01\uff1f!?\.]+[\u3002\uff01\uff1f!?\.]?")
_TABLE_ROW_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[\.)\u3001])\s*(.+?)\s*$")
_TABLE_INTRO_RE = re.compile(r"(?:\u5982\u4e0b|\u5982\u4e0b\u6240\u793a|as follows)\s*[:\uff1a]?$", re.IGNORECASE)
_MEANINGFUL_TEXT_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")


def sanitize_explanation(
    explanation: str,
    *,
    row_count: int,
    rows: list[dict[str, Any]] | None = None,
) -> str:
    text = _strip_repeated_table_row_lines(explanation.strip(), rows=rows)
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_RE.findall(text)
        if sentence.strip()
    ]
    if not sentences:
        return text

    kept = [
        sentence
        for sentence in sentences
        if not contains_false_preview_claim(sentence, row_count=row_count)
    ]
    if kept:
        result = "".join(kept).strip()
        if rows is not None:
            result = _strip_dangling_table_intro(result)
            if not _MEANINGFUL_TEXT_RE.search(result):
                return ""
        return result
    if row_count <= 0:
        return ""
    if rows is not None:
        return ""
    return f"\u67e5\u8be2\u8fd4\u56de {row_count} \u884c\uff0c\u5b8c\u6574\u8fd4\u56de\u7ed3\u679c\u5df2\u5728\u4e0b\u65b9\u6570\u636e\u8868\u683c\u4e2d\u5c55\u793a\u3002"


def contains_false_preview_claim(sentence: str, *, row_count: int) -> bool:
    if any(pattern.search(sentence) for pattern in _FALSE_PREVIEW_PATTERNS):
        return True
    match = _LEADING_RANK_LIST_RE.search(sentence)
    if match is None:
        return False
    try:
        claimed_count = int(match.group(1))
    except ValueError:
        return False
    return row_count > 0 and claimed_count != row_count


def _strip_repeated_table_row_lines(
    text: str,
    *,
    rows: list[dict[str, Any]] | None,
) -> str:
    if not rows:
        return text
    tokens = _table_value_tokens(rows)
    if not tokens:
        return text

    lines = text.splitlines()
    repeated_flags = [
        _looks_like_repeated_table_row(line, tokens=tokens)
        for line in lines
    ]
    if sum(repeated_flags) < 2:
        return text

    kept = [
        line
        for index, line in enumerate(lines)
        if not repeated_flags[index] and not _is_table_intro_line(line)
    ]
    return "\n".join(kept).strip()


def _table_value_tokens(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    tokens: set[str] = set()
    for row in rows:
        for value in row.values():
            if value is None or isinstance(value, (list, dict)):
                continue
            text = str(value).strip().lower()
            if 2 <= len(text) <= 80:
                tokens.add(text)
    return tuple(sorted(tokens, key=len, reverse=True))


def _looks_like_repeated_table_row(line: str, *, tokens: tuple[str, ...]) -> bool:
    match = _TABLE_ROW_BULLET_RE.match(line)
    if match is None:
        return False
    body = match.group(1).strip().lower()
    for token in tokens:
        if body == token:
            return True
        if body.startswith(token) and body[len(token) : len(token) + 1] in {":", "\uff1a", "-", "\u2013", "\u2014"}:
            return True
    return False


def _strip_dangling_table_intro(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not _is_table_intro_line(line)
    ]
    return "\n".join(lines).strip()


def _is_table_intro_line(line: str) -> bool:
    return bool(_TABLE_INTRO_RE.search(line.strip()))
