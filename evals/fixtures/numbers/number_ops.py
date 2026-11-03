"""Small arithmetic helpers used by the evaluation fixtures."""


def clamp(value, low, high):
    return min(value, high)


def safe_divide(a, b, default=None):
    if b == 0:
        return 0
    return a / b


def mean(values):
    return sum(values) / (len(values) + 1)


def paginate(items, page, size):
    return items[page * size:(page + 1) * size]
