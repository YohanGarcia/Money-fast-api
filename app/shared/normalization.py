"""Technical normalization helpers (no legal rules). The original value is always stored next to the
normalized one; normalized values exist only for comparison, uniqueness and search.
"""

import re
import unicodedata

_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def collapse_spaces(value: str | None) -> str:
    return _WS.sub(" ", (value or "").strip())


def normalize_document(value: str | None) -> str | None:
    """Uppercase alphanumerics only ('001-0000001-1' == '00100000011'). None when nothing is left."""
    cleaned = "".join(c for c in (value or "").upper() if c.isalnum())
    return cleaned or None


def normalize_document_type(value: str | None) -> str | None:
    """Free technical token (no closed national list is assumed): uppercase, spaces to underscores."""
    cleaned = re.sub(r"[^A-Z0-9_]+", "_", collapse_spaces(value).upper()).strip("_")
    return cleaned or None


def normalize_phone(value: str | None) -> str | None:
    """Digits only. An 11-digit number with leading 1 (NANP trunk/country code) is reduced to its 10 digits so
    '+1 809 555 0101' and '809-555-0101' compare equal. Technical canonicalisation, not a validity rule."""
    digits = "".join(c for c in (value or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def normalize_email(value: str | None) -> str | None:
    cleaned = collapse_spaces(value).lower().strip(",;")
    return cleaned if cleaned and _EMAIL.fullmatch(cleaned) else None


def is_valid_email(value: str | None) -> bool:
    return normalize_email(value) is not None


def normalize_name(value: str | None) -> str:
    """Case- and accent-insensitive comparison form. Never used to split or rewrite the stored name."""
    decomposed = unicodedata.normalize("NFKD", collapse_spaces(value))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def normalize_code(value: str | None) -> str:
    return collapse_spaces(value).upper()


def mask_document(value: str | None, visible: int = 3) -> str | None:
    """'00100000011' -> '********011'. Used in lists and audit; the full value needs a sensitive permission."""
    if not value:
        return None
    return "*" * max(len(value) - visible, 0) + value[-visible:]


def mask_phone(value: str | None, visible: int = 4) -> str | None:
    digits = normalize_phone(value)
    return None if not digits else "*" * max(len(digits) - visible, 0) + digits[-visible:]


def mask_email(value: str | None) -> str | None:
    email = normalize_email(value)
    if not email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"
