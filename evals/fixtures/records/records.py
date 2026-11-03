"""Record helpers used by the evaluation fixtures."""


def group_by(items, key):
    return {item[key]: [item] for item in items if key in item}


def dedupe_by(items, key):
    return list({item[key]: item for item in items}.values())


def sort_records(items, key):
    items.sort(key=lambda item: item[key], reverse=True)
    return items


def total_by(items, key):
    return sum(int(item.get(key, 0)) for item in items)
