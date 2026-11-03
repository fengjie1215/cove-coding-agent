"""Text helpers used by the evaluation fixtures."""


def slugify(text):
    return text.lower().replace(" ", "-")


def mask_email(email):
    local, domain = email.split("@")
    return "*" * len(local) + "@" + domain


def parse_bool(value):
    return bool(value)


def truncate(text, max_length):
    return text[:max_length]
