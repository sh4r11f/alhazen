"""The scene format's expression language, evaluated strictly.

Any numeric field in a scene can be ``{"expr": "..."}``. Those expressions are
a small language of their own — arithmetic, comparisons, a ternary, a fixed
set of maths functions, and deterministic noise — and this evaluates them.

**Never with Python's ``eval``.** A scene is data that arrives from a file,
and handing a file's contents to ``eval`` is handing it the interpreter.
Everything here is tokenised, parsed into a tiny AST, and walked; an unknown
identifier is an error at parse time rather than an attribute lookup on
something it should never have reached.

Parity with the studio's TypeScript is the point, and several of its
behaviours are *not* Python's:

- ``round`` rounds halves upward (JavaScript), where Python rounds halves to
  even: ``round(2.5)`` is 3 here and 2 in Python;
- ``%`` keeps the sign of the dividend (JavaScript), where Python's ``%``
  keeps the sign of the divisor; the language's own ``mod()`` is the
  non-negative one;
- unary minus binds tighter than ``**``, so ``-2 ** 2`` is 4, not −4;
- comparisons return 1 and 0, not True and False.

Each of those is pinned by a fixture generated from the TypeScript itself
(tests/fixtures/expr_parity.json).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from alhazen.errors import ConfigError
from alhazen.scenes.rng import rng, rng_n

# ---------------------------------------------------------------------------
# The functions and constants an expression may use
# ---------------------------------------------------------------------------


def js_round(x: float) -> float:
    """JavaScript's ``Math.round``: halves go **up**, toward +infinity.

    Public because the renderer needs it too. Python's ``round`` and numpy's
    ``rint`` both round halves to even, so a value landing exactly on .5 —
    which integer-sized shapes on a pixel grid do constantly — would be placed
    one pixel from where the studio places it.
    """
    return math.floor(x + 0.5)


def _js_sign(x: float) -> float:
    return math.copysign(1.0, x) if x else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _ease_in_out(t: float) -> float:
    x = _clamp(t, 0.0, 1.0)
    return 2 * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 2 / 2


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - _clamp(t, 0.0, 1.0)) ** 3


_HEX_COLOR = re.compile(r"^#([0-9a-fA-F]{6})$")


def _with_alpha(color: Any, alpha: Any) -> str:
    """``"#rrggbb"`` -> ``"rgba(r, g, b, a)"``; anything else passes through.

    The one string-valued function in the language. It exists so a fill can
    be ``{"expr": "withAlpha(params.ink, 0.12)"}`` — a translucent colour
    derived from a colour parameter — without the scene format having to grow
    string operations. ``parse_color`` already documents accepting the rgba
    form this produces.
    """
    text = str(color)
    match = _HEX_COLOR.match(text)
    if not match:
        return text
    packed = int(match.group(1), 16)
    red, green, blue = (packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF
    return f"rgba({red}, {green}, {blue}, {_js_string(alpha)})"


def _to_int32(value: float) -> int:
    """JavaScript's ToInt32, which its bitwise operators apply to operands."""
    return ((int(value) & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sqrt": math.sqrt,
    "abs": abs,
    "exp": math.exp,
    "log": math.log,
    "log2": math.log2,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": js_round,
    "sign": _js_sign,
    "min": min,
    "max": max,
    "pow": lambda a, b: a**b,
    "clamp": _clamp,
    "lerp": lambda a, b, t: a + (b - a) * t,
    "step": lambda edge, x: 0.0 if x < edge else 1.0,
    "smoothstep": _smoothstep,
    # The language's own modulo, always non-negative — unlike the % operator.
    "mod": lambda a, b: ((a % b) + b) % b if b else float("nan"),
    "easeInOut": _ease_in_out,
    "easeOutCubic": _ease_out_cubic,
    "hypot": math.hypot,
    # 32-bit integer operations, for hash-style per-cell variation. Unsigned
    # result for xor, matching the TypeScript's `>>> 0`.
    "xor": lambda a, b: float((_to_int32(a) ^ _to_int32(b)) & 0xFFFFFFFF),
    "band": lambda a, b: float(_to_int32(a) & _to_int32(b)),
    "rng": rng,
    "rngN": rng_n,
    "fixed": lambda value, digits: f"{float(value):.{int(digits)}f}",
    "withAlpha": _with_alpha,
}

CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    # The language has no booleans of its own; these are the numbers
    # comparisons produce.
    "true": 1.0,
    "false": 0.0,
}

# Identifiers the host supplies. Anything else is an error at parse time,
# which is what keeps a typo from silently evaluating to nothing.
HOST_IDENTIFIERS = frozenset({"time", "dt", "width", "height", "dpr", "params"})

# In the studio, `ref(id)` reads another layer's resolved fields. alhazen does
# not implement it: cross-layer reads make evaluation ORDER load-bearing, and
# a renderer whose output depends on which layer it happened to evaluate first
# is not one an experiment can rely on. It is refused at load, by name.
REF_IDENTIFIER = "ref"

# Binding power, lowest first. `**` is right-associative, handled below.
PRECEDENCE: dict[str, int] = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "!=": 3,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
    "**": 7,
}

_TWO_CHAR_OPS = {"**", "==", "!=", "<=", ">=", "&&", "||"}
_ONE_CHAR_OPS = set("+-*/%<>!?:(),.")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    kind: str  # "num" | "str" | "id" | "op"
    value: Any


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if char.isdigit() or (
            char == "." and index + 1 < len(source) and source[index + 1].isdigit()
        ):
            start = index
            while index < len(source) and (source[index].isdigit() or source[index] == "."):
                index += 1
            # Exponent notation, e.g. 1e-3: the 'e' is part of the number
            # only when a digit or sign follows it.
            if index < len(source) and source[index] in "eE":
                lookahead = index + 1
                if lookahead < len(source) and (
                    source[lookahead].isdigit() or source[lookahead] in "+-"
                ):
                    index = lookahead + 1
                    while index < len(source) and source[index].isdigit():
                        index += 1
            tokens.append(Token("num", float(source[start:index])))
            continue
        if char in "'\"":
            closing = source.find(char, index + 1)
            if closing < 0:
                raise ConfigError(f"unterminated string in expression: {source!r}")
            tokens.append(Token("str", source[index + 1 : closing]))
            index = closing + 1
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < len(source) and (source[index].isalnum() or source[index] == "_"):
                index += 1
            tokens.append(Token("id", source[start:index]))
            continue
        two = source[index : index + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(Token("op", two))
            index += 2
            continue
        if char in _ONE_CHAR_OPS:
            tokens.append(Token("op", char))
            index += 1
            continue
        raise ConfigError(
            f"unexpected character {char!r} at position {index} in expression: {source!r}"
        )
    return tokens


# ---------------------------------------------------------------------------
# The AST, and the parser that builds it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    kind: str  # "num" | "str" | "id" | "unary" | "bin" | "tern" | "member" | "call"
    value: Any = None
    args: tuple[Any, ...] = ()


class Parser:
    """Precedence-climbing, which is the same shape as the TypeScript's."""

    def __init__(self, tokens: list[Token], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.position = 0

    def peek(self) -> Token | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def consume(self) -> Token:
        token = self.peek()
        if token is None:
            raise ConfigError(f"expression ends unexpectedly: {self.source!r}")
        self.position += 1
        return token

    def expect_op(self, value: str) -> None:
        token = self.peek()
        if token is None or token.kind != "op" or token.value != value:
            raise ConfigError(
                f"expected {value!r} in expression {self.source!r}, got "
                f"{token.value if token else 'end of expression'!r}"
            )
        self.position += 1

    def parse(self) -> Node:
        node = self.parse_ternary()
        if self.position != len(self.tokens):
            leftover = self.tokens[self.position]
            raise ConfigError(
                f"unexpected {leftover.value!r} after the end of expression {self.source!r}"
            )
        return node

    def parse_ternary(self) -> Node:
        condition = self.parse_binary(0)
        token = self.peek()
        if token is not None and token.kind == "op" and token.value == "?":
            self.consume()
            if_true = self.parse_ternary()
            self.expect_op(":")
            if_false = self.parse_ternary()
            return Node("tern", args=(condition, if_true, if_false))
        return condition

    def parse_binary(self, min_precedence: int) -> Node:
        left = self.parse_unary()
        while True:
            token = self.peek()
            if token is None or token.kind != "op":
                break
            precedence = PRECEDENCE.get(token.value)
            if precedence is None or precedence < min_precedence:
                break
            self.consume()
            # `**` is right-associative: parsing its right side at the SAME
            # precedence lets it re-enter, so 2**3**2 is 2**(3**2).
            next_min = precedence if token.value == "**" else precedence + 1
            right = self.parse_binary(next_min)
            left = Node("bin", value=token.value, args=(left, right))
        return left

    def parse_unary(self) -> Node:
        token = self.peek()
        if token is not None and token.kind == "op" and token.value in ("-", "+", "!"):
            self.consume()
            # Unary binds tighter than `**` here, so -2**2 is (-2)**2 = 4.
            # That is the studio's parser, not JavaScript's (which rejects
            # the expression outright) — and scenes are written against it.
            return Node("unary", value=token.value, args=(self.parse_unary(),))
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            token = self.peek()
            if token is None or token.kind != "op":
                break
            if token.value == ".":
                self.consume()
                name = self.consume()
                if name.kind != "id":
                    raise ConfigError(f"expected a property name after '.' in {self.source!r}")
                node = Node("member", value=name.value, args=(node,))
            elif token.value == "(":
                self.consume()
                arguments: list[Node] = []
                if not self._at_op(")"):
                    arguments.append(self.parse_ternary())
                    while self._at_op(","):
                        self.consume()
                        arguments.append(self.parse_ternary())
                self.expect_op(")")
                node = Node("call", args=(node, tuple(arguments)))
            else:
                break
        return node

    def parse_primary(self) -> Node:
        token = self.consume()
        if token.kind == "num":
            return Node("num", value=token.value)
        if token.kind == "str":
            return Node("str", value=token.value)
        if token.kind == "id":
            return Node("id", value=token.value)
        if token.kind == "op" and token.value == "(":
            node = self.parse_ternary()
            self.expect_op(")")
            return node
        raise ConfigError(f"unexpected {token.value!r} in expression {self.source!r}")

    def _at_op(self, value: str) -> bool:
        token = self.peek()
        return token is not None and token.kind == "op" and token.value == value


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class EvalContext:
    """What an expression may see. Nothing else is reachable from one."""

    time: float = 0.0  # seconds since the scene started
    dt: float = 0.0  # MILLISECONDS since the previous frame (the format's unit)
    width: float = 0.0  # logical canvas size
    height: float = 0.0
    dpr: float = 1.0
    params: dict[str, Any] | None = None
    # Extra numeric names the host injects (a repeat's index variable).
    vars: dict[str, float] | None = None


def _truthy(value: Any) -> bool:
    """JavaScript truthiness, for the operands of && || ! and the ternary."""
    if isinstance(value, str):
        return value != ""
    return bool(value)


def _evaluate(node: Node, context: EvalContext) -> Any:
    kind = node.kind
    if kind == "num" or kind == "str":
        return node.value
    if kind == "id":
        name = node.value
        if context.vars and name in context.vars:
            return context.vars[name]
        if name in CONSTANTS:
            return CONSTANTS[name]
        if name == "params":
            return context.params or {}
        if name in HOST_IDENTIFIERS:
            return getattr(context, name)
        if name in FUNCTIONS:
            # A bare function name is only meaningful when called; returning
            # it lets the call node find it, and nothing else can use it.
            return FUNCTIONS[name]
        raise ConfigError(f"unknown identifier {name!r} in a scene expression")
    if kind == "member":
        target = _evaluate(node.args[0], context)
        prop = node.value
        if prop.startswith("__"):
            # Nothing in a scene has any business reaching a dunder; this is
            # the one place a hostile scene could otherwise try to.
            raise ConfigError(f"property {prop!r} is not readable from a scene expression")
        if isinstance(target, dict):
            if prop not in target:
                raise ConfigError(
                    f"expression reads params.{prop}, which this scene's params do not "
                    f"define (they have {sorted(target)})"
                )
            return target[prop]
        raise ConfigError(f"cannot read {prop!r}: that value is not an object")
    if kind == "call":
        function = _evaluate(node.args[0], context)
        if not callable(function):
            raise ConfigError("attempted to call something that is not a function")
        arguments = [_evaluate(argument, context) for argument in node.args[1]]
        try:
            return function(*arguments)
        except ConfigError:
            raise
        except Exception as error:
            raise ConfigError(f"error evaluating a scene expression: {error}") from error
    if kind == "unary":
        value = _evaluate(node.args[0], context)
        if node.value == "-":
            return -value
        if node.value == "+":
            return +value
        return 0.0 if _truthy(value) else 1.0
    if kind == "tern":
        condition = _evaluate(node.args[0], context)
        return _evaluate(node.args[1] if _truthy(condition) else node.args[2], context)

    # Binary.
    left = _evaluate(node.args[0], context)
    operator = node.value
    # && and || short-circuit and return an OPERAND, not a boolean — the
    # TypeScript's semantics, which scenes rely on for `a || fallback`.
    if operator == "&&":
        return _evaluate(node.args[1], context) if _truthy(left) else left
    if operator == "||":
        return left if _truthy(left) else _evaluate(node.args[1], context)
    right = _evaluate(node.args[1], context)
    if operator == "+":
        if isinstance(left, str) or isinstance(right, str):
            return _js_string(left) + _js_string(right)
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if operator == "/":
        # JavaScript divides by zero to infinity rather than raising; a scene
        # that does it gets an infinite coordinate, which the renderer then
        # clips, instead of a crash mid-frame.
        if right == 0:
            return math.inf if left > 0 else (-math.inf if left < 0 else math.nan)
        return left / right
    if operator == "%":
        # JavaScript's remainder keeps the sign of the DIVIDEND; Python's
        # keeps the sign of the divisor. math.fmod is the JavaScript one.
        return math.fmod(left, right) if right else math.nan
    if operator == "**":
        return left**right
    if operator == "==":
        return 1.0 if left == right else 0.0
    if operator == "!=":
        return 1.0 if left != right else 0.0
    if operator == "<":
        return 1.0 if left < right else 0.0
    if operator == "<=":
        return 1.0 if left <= right else 0.0
    if operator == ">":
        return 1.0 if left > right else 0.0
    if operator == ">=":
        return 1.0 if left >= right else 0.0
    raise ConfigError(f"unknown operator {operator!r} in a scene expression")


def _js_string(value: Any) -> str:
    """How JavaScript renders a value inside a string concatenation.

    Only the integer case differs from Python's ``str``, and it differs
    visibly: 3.0 there prints as "3".
    """
    if isinstance(value, float) and value.is_integer() and math.isfinite(value):
        return str(int(value))
    return str(value)


def identifiers(source: str) -> set[str]:
    """Every bare identifier an expression reads, including function names.

    Used at scene load to resolve names against the function library, the
    builtin variables and the scene's own declared params — so a typo in a
    ternary branch that this session happens never to take is still an error
    when the file is opened, which is what the spec means by "parse time".
    """
    node = compile_node(source)
    found: set[str] = set()
    _collect_identifiers(node, found)
    return found


def _collect_identifiers(node: Any, found: set[str]) -> None:
    # A call node nests its arguments in a tuple rather than spreading them,
    # so the walk handles both shapes.
    if isinstance(node, tuple):
        for child in node:
            _collect_identifiers(child, found)
        return
    if not isinstance(node, Node):
        return
    if node.kind == "id":
        found.add(str(node.value))
    # A member access reads its target, not the property name: `params.foo`
    # contributes `params`, and whether `foo` exists is a question about the
    # params, answered where they are known.
    for child in node.args:
        _collect_identifiers(child, found)


def param_references(source: str) -> set[str]:
    """Every ``params.<name>`` this expression reads.

    What a scene needs handed to it is a question an experiment has to answer
    before it can run one, and the honest place to answer it is the scene
    itself rather than a comment in the task file.
    """
    found: set[str] = set()
    _collect_params(compile_node(source), found)
    return found


def _collect_params(node: Any, found: set[str]) -> None:
    if isinstance(node, tuple):
        for child in node:
            _collect_params(child, found)
        return
    if not isinstance(node, Node):
        return
    if node.kind == "member":
        target = node.args[0]
        if isinstance(target, Node) and target.kind == "id" and target.value == "params":
            found.add(str(node.value))
    for child in node.args:
        _collect_params(child, found)


_CACHE: dict[str, Node] = {}


def compile_node(source: str) -> Node:
    """Parse once, cached by source text — the same cache the studio keeps,
    for the same reason: a scene's fields are re-parsed every frame."""
    node = _CACHE.get(source)
    if node is None:
        node = Parser(tokenize(source), source).parse()
        _CACHE[source] = node
    return node


def compile_expr(source: str) -> Callable[[EvalContext], Any]:
    """Parse once, evaluate many times."""
    node = compile_node(source)
    return lambda context: _evaluate(node, context)


def evaluate_expr(source: str, context: EvalContext) -> Any:
    return compile_expr(source)(context)


def evaluate_number(source: str, context: EvalContext) -> float:
    """Evaluate, insisting on a number — which every geometric field is."""
    value = evaluate_expr(source, context)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise ConfigError(f"expression {source!r} produced {value!r}, but a number was needed here")
