"""Reference implementation used only to validate evaluation cases."""


def group_by(items, key):
    groups = {}
    for item in items:
        if key in item:
            groups.setdefault(item[key], []).append(item)
    return groups


def dedupe_by(items, key):
    seen = set()
    result = []
    for item in items:
        value = item[key]
        if value not in seen:
            seen.add(value)
            result.append(item)
    return result


def sort_records(items, key):
    return sorted(items, key=lambda item: item[key])


def total_by(items, key):
    return sum(item.get(key, 0) for item in items)
