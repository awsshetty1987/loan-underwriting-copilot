"""Masks personal data before any text is sent to GPT, and puts it back in GPT's answer.
PAN, Aadhaar, phone numbers, e-mail addresses, account numbers and the names we pass in become tokens such as
[PAN_1]; GPT works with the tokens and never sees the real values."""
import re

PATTERNS = [
    ("PAN", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    ("AADHAAR", re.compile(r"\b(?:\d{4}|[Xx]{4})[ -]?(?:\d{4}|[Xx]{4})[ -]?\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"(?<![\d.])(?:\+91[ -]?)?[6-9]\d{9}(?![\d.])")),
    ("ACCOUNT", re.compile(r"(?<![\d.,])\d{11,18}(?![\d.,])")),
]


def mask(text: str, names: list[str | None] = ()) -> tuple[str, dict[str, str]]:
    """(masked text, token -> original). names = people's names known in advance (from the plain-code reader)."""
    mapping: dict[str, str] = {}
    seen: dict[str, str] = {}

    def token_for(kind: str, value: str) -> str:
        if value not in seen:
            n = sum(1 for t in mapping if t.startswith(f"[{kind}_")) + 1
            seen[value] = f"[{kind}_{n}]"
            mapping[seen[value]] = value
        return seen[value]

    for name in sorted({n.strip() for n in names if n and len(n.strip()) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(name), lambda m: token_for("NAME", m.group(0)), text, flags=re.IGNORECASE)
    for kind, pattern in PATTERNS:
        text = pattern.sub(lambda m, k=kind: token_for(k, m.group(0)), text)
    return text, mapping


def unmask(text: str | None, mapping: dict[str, str]) -> str | None:
    if text is None:
        return None
    for token, value in mapping.items():
        text = text.replace(token, value)
    return text
