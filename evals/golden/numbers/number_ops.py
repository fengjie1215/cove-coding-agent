"""Reference implementation used only to validate evaluation cases."""


def clamp(value, low, high):
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))


def safe_divide(a, b, default=None):
    return default if b == 0 else a / b


def mean(values):
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)


def paginate(items, page, size):
    if type(page) is not int or type(size) is not int or page < 1 or size < 1:
        raise ValueError("page and size must be positive integers")
    start = (page - 1) * size
    return items[start:start + size]
