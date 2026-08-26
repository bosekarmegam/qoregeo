"""
qoregeo.query
=============
A tiny expression language for filtering features.

``geo.query("population > 1e6 and state != 'Delhi'")`` reads the way you would
say it out loud, which is the whole point — chaining half a dozen ``filter()``
calls does not.

This is a hand-written tokeniser and recursive-descent parser. It deliberately
does **not** use :func:`eval`: a query string often comes from a config file,
a CLI argument or a web form, and ``eval`` on any of those is a remote code
execution bug waiting to happen. The grammar below is the entire language, so
there is nothing else it can be made to do.

Grammar
-------
::

    expression  := term ( "or" term )*
    term        := factor ( "and" factor )*
    factor      := "not" factor | "(" expression ")" | comparison
    comparison  := field operator value
    operator    := == | != | > | >= | < | <= | contains | startswith
                 | endswith | in | is null | is not null
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from .exceptions import InvalidQueryError

Feature = Dict[str, Any]
Predicate = Callable[[Dict[str, Any]], bool]

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<op>==|!=|>=|<=|<>|>|<)
      | (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<comma>,)
      | (?P<number>-?\d+\.?\d*(?:[eE][-+]?\d+)?)
      | (?P<string>'[^']*'|"[^"]*")
      | (?P<backtick>`[^`]*`)
      | (?P<word>[A-Za-z_][A-Za-z0-9_.\- ]*?)(?=\s*(?:==|!=|>=|<=|<>|>|<|\(|\)|,|$|\s))
    )
    """,
    re.VERBOSE,
)

_KEYWORDS = {
    "and", "or", "not", "contains", "startswith", "endswith",
    "in", "is", "null", "none", "true", "false",
}

_COMPARISONS = {"==", "!=", ">=", "<=", "<>", ">", "<"}
_WORD_OPS = {"contains", "startswith", "endswith", "in"}


class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any) -> None:
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:
        return f"{self.kind}:{self.value!r}"


def tokenise(expression: str) -> List[_Token]:
    """Split a query string into tokens. Raises on anything unrecognised."""
    tokens: List[_Token] = []
    position = 0
    text = str(expression)

    while position < len(text):
        if text[position].isspace():
            position += 1
            continue

        match = _TOKEN_RE.match(text, position)
        if not match or match.end() == position:
            raise InvalidQueryError(
                expression, f"Unexpected character at position {position}: {text[position]!r}"
            )

        position = match.end()
        kind = match.lastgroup or ""
        raw = (match.group(kind) or "").strip()

        if kind == "op":
            tokens.append(_Token("op", "!=" if raw == "<>" else raw))
        elif kind == "lparen":
            tokens.append(_Token("lparen", "("))
        elif kind == "rparen":
            tokens.append(_Token("rparen", ")"))
        elif kind == "comma":
            tokens.append(_Token("comma", ","))
        elif kind == "number":
            tokens.append(_Token("number", float(raw)))
        elif kind == "string":
            tokens.append(_Token("string", raw[1:-1]))
        elif kind == "backtick":
            tokens.append(_Token("field", raw[1:-1]))
        elif kind == "word":
            lowered = raw.lower()
            tokens.append(
                _Token("keyword", lowered) if lowered in _KEYWORDS else _Token("word", raw)
            )

    return tokens


class _Parser:
    """Recursive-descent parser producing a predicate over a properties dict."""

    def __init__(self, tokens: Sequence[_Token], expression: str) -> None:
        self.tokens = list(tokens)
        self.position = 0
        self.expression = expression

    # ── token helpers ───────────────────────────────────────────────────────

    def peek(self, offset: int = 0) -> Optional[_Token]:
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise InvalidQueryError(self.expression, "The expression ends unexpectedly.")
        self.position += 1
        return token

    def accept_keyword(self, word: str) -> bool:
        token = self.peek()
        if token and token.kind == "keyword" and token.value == word:
            self.position += 1
            return True
        return False

    # ── grammar ─────────────────────────────────────────────────────────────

    def parse(self) -> Predicate:
        if not self.tokens:
            raise InvalidQueryError(self.expression, "The expression is empty.")
        predicate = self.expression_rule()
        if self.position < len(self.tokens):
            leftover = self.tokens[self.position]
            raise InvalidQueryError(
                self.expression, f"Unexpected {leftover.value!r} after a complete expression."
            )
        return predicate

    def expression_rule(self) -> Predicate:
        left = self.term_rule()
        while self.accept_keyword("or"):
            right = self.term_rule()
            left = _either(left, right)
        return left

    def term_rule(self) -> Predicate:
        left = self.factor_rule()
        while self.accept_keyword("and"):
            right = self.factor_rule()
            left = _both(left, right)
        return left

    def factor_rule(self) -> Predicate:
        if self.accept_keyword("not"):
            inner = self.factor_rule()
            return lambda props: not inner(props)

        token = self.peek()
        if token and token.kind == "lparen":
            self.take()
            inner = self.expression_rule()
            closing = self.peek()
            if not closing or closing.kind != "rparen":
                raise InvalidQueryError(self.expression, "A '(' is never closed.")
            self.take()
            return inner

        return self.comparison_rule()

    def comparison_rule(self) -> Predicate:
        field_token = self.take()
        if field_token.kind not in ("word", "field"):
            raise InvalidQueryError(
                self.expression,
                f"Expected a column name but found {field_token.value!r}.",
            )
        field = str(field_token.value).strip()

        operator_token = self.peek()
        if operator_token is None:
            raise InvalidQueryError(
                self.expression, f"Column {field!r} is not compared to anything."
            )

        # `field is null` / `field is not null`
        if operator_token.kind == "keyword" and operator_token.value == "is":
            self.take()
            negate = self.accept_keyword("not")
            if not (self.accept_keyword("null") or self.accept_keyword("none")):
                raise InvalidQueryError(
                    self.expression, "Expected 'null' after 'is'."
                )
            return _is_null(field, negate)

        if operator_token.kind == "op":
            self.take()
            return _compare(field, str(operator_token.value), self.value_rule())

        if operator_token.kind == "keyword" and operator_token.value in _WORD_OPS:
            self.take()
            word = str(operator_token.value)
            if word == "in":
                return _in_list(field, self.list_rule())
            return _text_op(field, word, self.value_rule())

        raise InvalidQueryError(
            self.expression,
            f"{operator_token.value!r} is not an operator. "
            f"Use one of: == != > >= < <= contains startswith endswith in",
        )

    def value_rule(self) -> Any:
        token = self.take()
        if token.kind == "number":
            return token.value
        if token.kind in ("string", "word", "field"):
            return token.value
        if token.kind == "keyword":
            if token.value in ("true", "false"):
                return token.value == "true"
            if token.value in ("null", "none"):
                return None
            return token.value
        raise InvalidQueryError(self.expression, f"{token.value!r} is not a valid value.")

    def list_rule(self) -> List[Any]:
        token = self.peek()
        if not token or token.kind != "lparen":
            raise InvalidQueryError(
                self.expression, "'in' must be followed by a list: in ('a', 'b')"
            )
        self.take()

        values: List[Any] = []
        while True:
            token = self.peek()
            if token and token.kind == "rparen":
                self.take()
                break
            values.append(self.value_rule())
            token = self.peek()
            if token and token.kind == "comma":
                self.take()
                continue
            if token and token.kind == "rparen":
                self.take()
                break
            raise InvalidQueryError(self.expression, "The 'in' list is never closed.")

        if not values:
            raise InvalidQueryError(self.expression, "The 'in' list is empty.")
        return values


# ─────────────────────────────────────────────────────────────────────────────
# Predicate builders
# ─────────────────────────────────────────────────────────────────────────────

def _both(left: Predicate, right: Predicate) -> Predicate:
    return lambda props: left(props) and right(props)


def _either(left: Predicate, right: Predicate) -> Predicate:
    return lambda props: left(props) or right(props)


def _is_null(field: str, negate: bool) -> Predicate:
    def predicate(props: Dict[str, Any]) -> bool:
        value = props.get(field)
        blank = value is None or (isinstance(value, str) and value.strip() == "")
        return (not blank) if negate else blank

    return predicate


def _as_number(value: Any) -> Optional[float]:
    """Coerce to float, or None if the value simply isn't numeric."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _compare(field: str, operator: str, expected: Any) -> Predicate:
    """
    Build a comparison.

    Numbers compare numerically whenever *both* sides look numeric — CSV
    columns arrive as strings, and ``"9" > "10"`` being true would surprise
    everybody. Otherwise strings compare case-insensitively, matching
    ``filter()``.
    """

    def predicate(props: Dict[str, Any]) -> bool:
        actual = props.get(field)

        if actual is None:
            return operator == "!=" and expected is not None
        if expected is None:
            return operator == "!="

        left_num = _as_number(actual)
        right_num = _as_number(expected)

        left: Any
        right: Any
        if left_num is not None and right_num is not None:
            left, right = left_num, right_num
        else:
            left, right = str(actual).strip().lower(), str(expected).strip().lower()

        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        return False

    return predicate


def _text_op(field: str, operator: str, expected: Any) -> Predicate:
    needle = str(expected).strip().lower()

    def predicate(props: Dict[str, Any]) -> bool:
        value = props.get(field)
        if value is None:
            return False
        haystack = str(value).strip().lower()
        if operator == "contains":
            return needle in haystack
        if operator == "startswith":
            return haystack.startswith(needle)
        if operator == "endswith":
            return haystack.endswith(needle)
        return False

    return predicate


def _in_list(field: str, values: Sequence[Any]) -> Predicate:
    numeric = {_as_number(v) for v in values}
    textual = {str(v).strip().lower() for v in values}

    def predicate(props: Dict[str, Any]) -> bool:
        value = props.get(field)
        if value is None:
            return False
        as_number = _as_number(value)
        if as_number is not None and as_number in numeric:
            return True
        return str(value).strip().lower() in textual

    return predicate


# ─────────────────────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────────────────────

def compile_query(expression: str) -> Predicate:
    """
    Compile a query string into a predicate over a feature's properties.

    Compile once and reuse it when filtering repeatedly — parsing is cheap,
    but not free.
    """
    return _Parser(tokenise(expression), str(expression)).parse()


def run_query(features: Sequence[Feature], expression: str) -> List[Feature]:
    """Return the features whose properties satisfy ``expression``."""
    predicate = compile_query(expression)
    return [f for f in features if predicate(f.get("properties") or {})]
