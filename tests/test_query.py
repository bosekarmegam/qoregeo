"""
The query expression language.

Two things matter here: that it parses what it promises, and that it refuses
everything else. A query string often arrives from a config file or a web
form, so the parser is the security boundary. There is no ``eval`` behind it,
and these tests keep it that way.
"""

from __future__ import annotations

import pytest

from qoregeo.exceptions import InvalidQueryError
from qoregeo.query import _as_number, compile_query, run_query, tokenise

from .conftest import point_feature

DATA = [
    point_feature(28.6, 77.2, name="Delhi", state="Delhi",
                  population="32900000", tier=1, note=None),
    point_feature(19.0, 72.8, name="Mumbai", state="Maharashtra",
                  population="20700000", tier=1, note="port"),
    point_feature(18.5, 73.8, name="Pune", state="Maharashtra",
                  population="7400000", tier=2, note=""),
    point_feature(17.6, 75.9, name="Solapur", state="Maharashtra",
                  population="951000", tier=3, note="inland"),
]


def names(expression):
    return [f["properties"]["name"] for f in run_query(DATA, expression)]


class TestComparisons:
    def test_greater_than(self):
        assert names("population > 1000000") == ["Delhi", "Mumbai", "Pune"]

    def test_scientific_notation(self):
        assert names("population > 1e6") == ["Delhi", "Mumbai", "Pune"]

    def test_less_than(self):
        assert names("tier < 2") == ["Delhi", "Mumbai"]

    def test_greater_or_equal(self):
        assert names("tier >= 2") == ["Pune", "Solapur"]

    def test_less_or_equal(self):
        assert names("tier <= 1") == ["Delhi", "Mumbai"]

    def test_equality_on_strings(self):
        assert names("state == 'Maharashtra'") == ["Mumbai", "Pune", "Solapur"]

    def test_double_quotes(self):
        assert names('state == "Maharashtra"') == ["Mumbai", "Pune", "Solapur"]

    def test_bare_words_are_strings(self):
        assert names("state == Maharashtra") == ["Mumbai", "Pune", "Solapur"]

    def test_string_equality_is_case_insensitive(self):
        assert names("state == 'maharashtra'") == ["Mumbai", "Pune", "Solapur"]

    def test_inequality(self):
        assert names("state != 'Delhi'") == ["Mumbai", "Pune", "Solapur"]

    def test_sql_style_inequality(self):
        assert names("tier <> 1") == ["Pune", "Solapur"]

    def test_numeric_strings_compare_as_numbers(self):
        # Text comparison would put "951000" above "20700000".
        assert "Solapur" not in names("population > 1000000")

    def test_missing_column_matches_nothing(self):
        assert names("nonexistent > 5") == []

    def test_backtick_quoted_field(self):
        assert names("`state` == 'Delhi'") == ["Delhi"]


class TestBooleanLogic:
    def test_and(self):
        assert names("population > 1e6 and state != 'Delhi'") == ["Mumbai", "Pune"]

    def test_or(self):
        assert names("tier == 1 or tier == 3") == ["Delhi", "Mumbai", "Solapur"]

    def test_not(self):
        assert names("not state == 'Delhi'") == ["Mumbai", "Pune", "Solapur"]

    def test_parentheses_group(self):
        assert names("(tier == 1 or tier == 3) and state == 'Maharashtra'") == [
            "Mumbai", "Solapur"
        ]

    def test_and_binds_tighter_than_or(self):
        # Without correct precedence this would return every row.
        assert names("tier == 3 or tier == 1 and state == 'Delhi'") == ["Delhi", "Solapur"]

    def test_nested_parentheses(self):
        assert names("((tier == 1))") == ["Delhi", "Mumbai"]

    def test_chained_and(self):
        assert names("tier == 1 and state == 'Maharashtra' and population > 1e6") == ["Mumbai"]

    def test_case_insensitive_keywords(self):
        assert names("tier == 1 AND state == 'Delhi'") == ["Delhi"]


class TestTextOperators:
    def test_contains(self):
        assert names("name contains 'pur'") == ["Solapur"]

    def test_contains_is_case_insensitive(self):
        assert names("name contains 'PUR'") == ["Solapur"]

    def test_startswith(self):
        assert names("name startswith 'M'") == ["Mumbai"]

    def test_endswith(self):
        assert names("name endswith 'i'") == ["Delhi", "Mumbai"]

    def test_text_operator_on_missing_value(self):
        assert names("note contains 'x'") == []


class TestInLists:
    def test_string_list(self):
        assert names("state in ('Delhi', 'Maharashtra')") == [
            "Delhi", "Mumbai", "Pune", "Solapur"
        ]

    def test_numeric_list(self):
        assert names("tier in (1, 2)") == ["Delhi", "Mumbai", "Pune"]

    def test_single_element_list(self):
        assert names("tier in (3)") == ["Solapur"]

    def test_empty_list_is_rejected(self):
        with pytest.raises(InvalidQueryError):
            compile_query("tier in ()")

    def test_unclosed_list_is_rejected(self):
        with pytest.raises(InvalidQueryError):
            compile_query("tier in (1, 2")

    def test_list_without_parentheses_is_rejected(self):
        with pytest.raises(InvalidQueryError):
            compile_query("tier in 1")


class TestNullChecks:
    def test_is_null_catches_none_and_blank(self):
        assert names("note is null") == ["Delhi", "Pune"]

    def test_is_not_null(self):
        assert names("note is not null") == ["Mumbai", "Solapur"]

    def test_none_is_a_synonym(self):
        assert names("note is none") == ["Delhi", "Pune"]

    def test_is_without_null_is_rejected(self):
        with pytest.raises(InvalidQueryError):
            compile_query("note is banana")


class TestRejection:
    @pytest.mark.parametrize(
        "expression",
        [
            "",
            "   ",
            "population >",
            "population 5",
            "(tier == 1",
            "tier == 1)",
            "tier @ 1",
            "tier == 1 tier == 2",
            "and tier == 1",
            "> 5",
            "tier ==",
        ],
    )
    def test_malformed_expressions_raise(self, expression):
        with pytest.raises(InvalidQueryError):
            compile_query(expression)

    def test_error_message_teaches_the_syntax(self):
        with pytest.raises(InvalidQueryError) as exc:
            compile_query("tier @@ 1")
        assert "contains" in str(exc.value)

    def test_the_parser_does_not_eval(self):
        # A parser built on eval would happily run this.
        with pytest.raises(InvalidQueryError):
            compile_query("__import__('os').system('echo pwned')")

    def test_python_expressions_are_not_executed(self):
        with pytest.raises(InvalidQueryError):
            compile_query("1 + 1 == 2")

    def test_module_has_no_eval_call(self):
        from pathlib import Path

        import qoregeo.query as query_module

        source = Path(query_module.__file__).read_text(encoding="utf-8")
        assert "eval(" not in source
        assert "exec(" not in source


class TestTokeniser:
    def test_splits_operators(self):
        kinds = [t.kind for t in tokenise("a == 1")]
        assert kinds == ["word", "op", "number"]

    def test_recognises_keywords(self):
        assert tokenise("a == 1 and b == 2")[3].kind == "keyword"

    def test_strings_keep_their_spaces(self):
        assert tokenise("a == 'New Delhi'")[2].value == "New Delhi"

    def test_negative_numbers(self):
        assert tokenise("a > -5")[2].value == -5.0

    def test_rejects_stray_symbols(self):
        with pytest.raises(InvalidQueryError):
            tokenise("a == $")


class TestNumberCoercion:
    @pytest.mark.parametrize("value,expected", [(5, 5.0), ("5", 5.0), ("  5.5 ", 5.5),
                                                (True, 1.0), (2.5, 2.5)])
    def test_numeric_values(self, value, expected):
        assert _as_number(value) == expected

    @pytest.mark.parametrize("value", ["abc", "", None, [1]])
    def test_non_numeric_values(self, value):
        assert _as_number(value) is None


class TestReuse:
    def test_compiled_predicate_is_reusable(self):
        predicate = compile_query("tier == 1")
        assert predicate({"tier": 1}) is True
        assert predicate({"tier": 2}) is False

    def test_run_query_leaves_input_untouched(self):
        before = [dict(f) for f in DATA]
        run_query(DATA, "tier == 1")
        assert before == DATA
