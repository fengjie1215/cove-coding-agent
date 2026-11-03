"""Out-of-worktree hidden correctness checks. Never copy this file into a fixture."""

import importlib.util
import sys
from pathlib import Path


MODULES = {"num": "number_ops.py", "str": "strings.py", "rec": "records.py"}


def _raises(error_type, function, *args):
    try:
        function(*args)
    except error_type:
        return
    raise AssertionError(f"expected {error_type.__name__}")


def check(case_id: str, repo: Path, suite: str = "all"):
    if suite not in {"all", "common", "memory"}:
        raise ValueError(f"unknown suite: {suite}")
    module_name = MODULES[case_id.split("-", 1)[0]]
    source = repo / module_name
    spec = importlib.util.spec_from_file_location("eval_target", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if case_id not in {
        "num-clamp", "num-divide", "num-mean", "num-page", "str-slug", "str-email",
        "str-bool", "str-truncate", "rec-group", "rec-dedupe", "rec-sort", "rec-total",
    }:
        raise ValueError(f"unknown case: {case_id}")

    if suite in {"all", "common"}:
        if case_id == "num-clamp":
            assert module.clamp(-2, 0, 8) == 0
            assert module.clamp(4, 0, 8) == 4
            assert module.clamp(12, 0, 8) == 8
        elif case_id == "num-divide":
            assert module.safe_divide(7, 2) == 3.5
            assert module.safe_divide(7, 0, -1) == -1
        elif case_id == "num-mean":
            assert module.mean([1, 2, 6]) == 3
            assert module.mean([4]) == 4
        elif case_id == "num-page":
            assert module.paginate([1, 2, 3, 4, 5], 2, 2) == [3, 4]
            assert module.paginate([1, 2], 3, 2) == []
        elif case_id == "str-slug":
            assert module.slugify("A & B") == "a-b"
        elif case_id == "str-email":
            assert module.mask_email("alice@example.com") == "a***e@example.com"
        elif case_id == "str-bool":
            assert module.parse_bool("yes") is True
            assert module.parse_bool("0") is False
            assert module.parse_bool("False") is False
        elif case_id == "str-truncate":
            assert module.truncate("abcdefgh", 5) == "ab..."
            assert module.truncate("abcd", 2) == ".."
        elif case_id == "rec-group":
            data = [{"k": "b", "n": 1}, {"k": "a", "n": 2}, {"k": "b", "n": 3}]
            assert module.group_by(data, "k") == {"b": [data[0], data[2]], "a": [data[1]]}
        elif case_id == "rec-dedupe":
            data = [{"k": 2, "n": "first"}, {"k": 1}, {"k": 2, "n": "second"}]
            assert module.dedupe_by(data, "k") == data[:2]
        elif case_id == "rec-sort":
            data = [{"n": 2}, {"n": 1}]
            assert module.sort_records(data, "n") == [{"n": 1}, {"n": 2}]
        elif case_id == "rec-total":
            assert module.total_by([{"v": 1}, {"v": 2}], "v") == 3

    if suite in {"all", "memory"}:
        if case_id == "num-clamp":
            _raises(ValueError, module.clamp, 1, 5, 0)
        elif case_id == "num-divide":
            assert module.safe_divide(7, 0) is None
        elif case_id == "num-mean":
            _raises(ValueError, module.mean, [])
        elif case_id == "num-page":
            _raises(ValueError, module.paginate, [1], 0, 1)
            _raises(ValueError, module.paginate, [1], 1, 0)
            _raises(ValueError, module.paginate, [1], 1.5, 1)
        elif case_id == "str-slug":
            assert module.slugify("  Hello---World  ") == "hello-world"
            assert module.slugify("A   B") == "a-b"
        elif case_id == "str-email":
            assert module.mask_email("ab@example.com") == "a*@example.com"
            assert module.mask_email("a@example.com") == "a@example.com"
            _raises(ValueError, module.mask_email, "invalid")
        elif case_id == "str-bool":
            assert module.parse_bool(" YES ") is True
            _raises(ValueError, module.parse_bool, "maybe")
        elif case_id == "str-truncate":
            assert module.truncate("abc", 5) == "abc"
            _raises(ValueError, module.truncate, "abc", 0)
        elif case_id == "rec-group":
            data = [{"k": "b", "n": 1}, {"n": 2}]
            assert module.group_by(data, "k") == {"b": [data[0]]}
        elif case_id == "rec-dedupe":
            data = [{"k": 2, "n": "first"}, {"k": 1}, {"k": 2, "n": "second"}]
            before = [dict(item) for item in data]
            result = module.dedupe_by(data, "k")
            assert result is not data and data == before
        elif case_id == "rec-sort":
            data = [{"n": 2, "id": "a"}, {"n": 1, "id": "b"}, {"n": 2, "id": "c"}]
            result = module.sort_records(data, "n")
            assert [row["id"] for row in result] == ["b", "a", "c"]
            assert [row["id"] for row in data] == ["a", "b", "c"]
        elif case_id == "rec-total":
            assert module.total_by([{"v": 1.25}, {"v": 2.5}, {}], "v") == 3.75
            assert module.total_by([], "v") == 0


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: check.py CASE_ID REPO [all|common|memory]")
    try:
        check(sys.argv[1], Path(sys.argv[2]), sys.argv[3] if len(sys.argv) == 4 else "all")
    except Exception as exc:
        print(f"FAIL {sys.argv[1]}: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    print(f"PASS {sys.argv[1]}")
