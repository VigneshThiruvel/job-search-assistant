"""Text helpers shared across sources."""

import hashlib
import re
from functools import cache
from html.parser import HTMLParser

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        # List items only break before themselves, so consecutive items stay tight.
        if tag in _BLOCK_TAGS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


_LEFTOVER_TAG = re.compile(r"</?(p|div|span|br|li|ul|ol|h[1-6]|strong|em|b|i|a)\b[^>]*>", re.I)


def html_to_text(html: str) -> str:
    """Strip tags but keep paragraph/list structure as newlines.

    Some sources entity-escape their HTML (`&lt;p&gt;`): one pass only decodes
    the entities into literal tags, so a second pass is needed.
    """
    text = _html_to_text_once(html)
    if _LEFTOVER_TAG.search(text):
        text = _html_to_text_once(text)
    return text


def _html_to_text_once(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Gender/diversity markers carry no identity: "Data Analyst (m/w/d)" == "Data Analyst (f/m/x)".
_GENDER_MARKER = re.compile(r"\((?:[mwfdx]\s*[/|]\s*)+[mwfdx]\)|\((?:gn|all genders?)\)", re.I)
_NON_WORD = re.compile(r"[^\w]+")


def normalize_for_hash(value: str | None) -> str:
    """Lowercase, drop gender markers and punctuation, collapse whitespace."""
    if not value:
        return ""
    value = _GENDER_MARKER.sub(" ", value.lower())
    return _NON_WORD.sub(" ", value).strip()


def content_hash(title: str, company: str, location: str | None) -> str:
    """Stable fingerprint of a posting for spotting the same job across sources."""
    key = "|".join(normalize_for_hash(v) for v in (title, company, location))
    return hashlib.sha256(key.encode()).hexdigest()


@cache
def _detector() -> LanguageDetector:
    # Restricting candidates makes detection faster and more accurate.
    languages = (
        Language.ENGLISH,
        Language.GERMAN,
        Language.FRENCH,
        Language.DUTCH,
        Language.ITALIAN,
        Language.SPANISH,
        Language.POLISH,
        Language.DANISH,
        Language.CZECH,
    )
    return LanguageDetectorBuilder.from_languages(*languages).build()


def detect_language(text: str, max_chars: int = 2000) -> str | None:
    """ISO 639-1 code of the dominant language, or None if undetectable."""
    language = _detector().detect_language_of(text[:max_chars])
    return language.iso_code_639_1.name.lower() if language else None
