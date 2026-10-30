"""Cache speed must not weaken real-source execution or test isolation."""

import ast
import os

import pytest

from conftest import load_source_node


def test_repeated_load_parses_once_but_returns_independent_nodes(tmp_path, monkeypatch):
    path = tmp_path / "component.py"
    source = "def target():\n    return ['original']\n"
    path.write_text(source)
    real_parse = ast.parse
    parsed = []

    def counted_parse(text, *args, **kwargs):
        if text == source:
            parsed.append(text)
        return real_parse(text, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted_parse)
    first = load_source_node(path, "target")
    expected = ast.dump(first, include_attributes=True)
    first.body[0].value.elts[0].value = "mutated"
    second = load_source_node(path, "target")
    assert ast.dump(second, include_attributes=True) == expected
    assert first is not second
    assert len(parsed) == 1


def test_same_size_same_timestamp_edit_invalidates_cache(tmp_path):
    path = tmp_path / "component.py"
    path.write_text("def target(): return 1\n")
    original = path.stat()
    assert load_source_node(path, "target").body[0].value.value == 1
    path.write_text("def target(): return 2\n")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size
    assert load_source_node(path, "target").body[0].value.value == 2
    path.write_text("def target(:\n")
    with pytest.raises(SyntaxError):
        load_source_node(path, "target")


def test_parent_selection_preserves_direct_scope_and_breadth_first_lookup(tmp_path):
    path = tmp_path / "component.py"
    path.write_text("def outer():\n    def target(): return 1\ndef target(): return 2\n")
    assert load_source_node(path, "target").body[0].value.value == 2
    assert load_source_node(path, "target", parent="outer").body[0].value.value == 1


def test_identical_names_in_different_files_remain_independent(tmp_path):
    for value in (1, 2):
        path = tmp_path / f"component{value}.py"
        path.write_text(f"def target(): return {value}\n")
        assert load_source_node(path, "target").body[0].value.value == value


def test_cached_nodes_execute_with_fresh_namespaces_and_import_variants(tmp_path):
    from test_tesla_local_settings import load_function

    path = tmp_path / "component.py"
    path.write_text("@decorate\ndef target():\n    from math import sqrt\n    return sqrt(value)\n")
    calls = []

    def decorate(function):
        calls.append(function)
        return function

    first = load_function(path, "target", {"decorate": decorate, "value": 9})
    second = load_function(path, "target", {
        "decorate": decorate, "value": 16, "sqrt": lambda value: value + 1,
    }, strip_imports=True)
    third = load_function(path, "target", {"decorate": decorate, "value": 25})
    assert (first(), second(), third()) == (3, 17, 5)
    assert len(calls) == 3
    assert len({id(function) for function in calls}) == 3
