"""Reference implementation used only to validate evaluation cases."""

import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def mask_email(email):
    if email.count("@") != 1:
        raise ValueError("invalid email")
    local, domain = email.split("@")
    if not local or not domain or "." not in domain:
        raise ValueError("invalid email")
    if len(local) <= 2:
        masked = local[0] + "*" * (len(local) - 1)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return masked + "@" + domain


def parse_bool(value):
    if not isinstance(value, str):
        raise ValueError("expected text")
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    raise ValueError("unknown boolean")


def truncate(text, max_length):
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if len(text) <= max_length:
        return text
    suffix = "." * min(3, max_length)
    return text[:max_length - len(suffix)] + suffix
