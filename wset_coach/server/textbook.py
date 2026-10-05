"""Search over the pre-extracted WSET textbook text.

~/wset/study/textbook_fulltext.txt is the 2016 Issue 1 textbook, already
OCR'd/extracted with page markers like:

    ===== PDF p12 | book p3 =====

so a match can be cited by book page, matching the coaching contract's
"cite figures/classifications" rule.
"""
import re
from pathlib import Path

from storage import ROOT, WsetError

TEXTBOOK_PATH = ROOT / 'study' / 'textbook_fulltext.txt'
PAGE_MARKER_RE = re.compile(r'===== PDF p(\d+) \| book p(-?\d+) =====')

_cache: dict = {}


def _load() -> tuple[str, list[tuple[int, str]]]:
    mtime = TEXTBOOK_PATH.stat().st_mtime if TEXTBOOK_PATH.exists() else None
    if _cache.get('mtime') == mtime and mtime is not None:
        return _cache['text'], _cache['markers']
    if not TEXTBOOK_PATH.exists():
        raise WsetError(f'Textbook text not found at {TEXTBOOK_PATH}.')
    text = TEXTBOOK_PATH.read_text(encoding='utf-8', errors='replace')
    markers = [(m.start(), m.group(2)) for m in PAGE_MARKER_RE.finditer(text)]
    _cache.update(mtime=mtime, text=text, markers=markers)
    return text, markers


def _book_page_at(offset: int, markers: list[tuple[int, str]]) -> str | None:
    page = None
    for marker_offset, book_page in markers:
        if marker_offset > offset:
            break
        page = book_page
    return page


def search_textbook(query: str, max_results: int = 8, context_chars: int = 500) -> list[dict]:
    query = query.strip()
    if not query:
        raise WsetError('query must not be empty.')
    if not 1 <= max_results <= 30:
        raise WsetError('max_results must be between 1 and 30.')
    text, markers = _load()
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    results = []
    last_end = -(context_chars + 1)  # sentinel far enough back that the first match is never skipped
    for match in pattern.finditer(text):
        start = match.start()
        if start - last_end < context_chars:  # skip near-duplicate hits from the same passage
            continue
        excerpt_start = max(0, start - context_chars // 2)
        excerpt_end = min(len(text), match.end() + context_chars // 2)
        excerpt = text[excerpt_start:excerpt_end].strip()
        results.append({
            'book_page': _book_page_at(start, markers),
            'excerpt': excerpt,
        })
        last_end = start
        if len(results) >= max_results:
            break
    return results
