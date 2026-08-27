"""The expression language, pinned to illusion-studio's own TypeScript.

Every expected value in ``fixtures_expr_parity.json`` was produced by running
the studio's ``src/expr.ts`` and ``src/rng.ts`` under node. A value that
agrees with a Python reimplementation of what the TypeScript *appears* to do
would prove nothing; these agree with what it actually does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alhazen.errors import ConfigError
from alhazen.scenes.expr import EvalContext, evaluate_expr, evaluate_number
from alhazen.scenes.rng import mulberry32, rng, rng_n

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures_expr_parity.json").read_text())


@pytest.fixture(scope="module")
def context() -> EvalContext:
    data = FIXTURE["context"]
    return EvalContext(
        time=data["time"],
        dt=data["dt"],
        width=data["width"],
        height=data["height"],
        dpr=data["dpr"],
        params=data["params"],
    )


class TestRngParity:
    @pytest.mark.parametrize("case", FIXTURE["rng"], ids=lambda case: f"seed{case['seed']}")
    def test_the_sequence_is_bit_exact(self, case):
        # Not approximately: a stored scene's seeded noise must render the
        # same forever, and "close" would mean a different picture.
        generator = mulberry32(case["seed"])
        assert [generator() for _ in case["values"]] == case["values"]

    def test_the_language_functions_agree_with_the_generator(self):
        assert rng(1234) == mulberry32(1234)()
        generator = mulberry32(1234)
        for _ in range(5):
            generator()
        assert rng_n(1234, 5) == generator()

    def test_rngn_is_clamped_rather_than_left_to_run(self):
        # An expression asking for the billionth sample would otherwise hang
        # a render, which is worse than drawing the clamped value.
        assert 0.0 <= rng_n(1, 10**9) < 1.0


class TestExpressionParity:
    @pytest.mark.parametrize("case", FIXTURE["expressions"], ids=lambda case: case["src"])
    def test_matches_the_typescript(self, case, context):
        result = evaluate_expr(case["src"], context)
        expected = case["value"]
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            assert float(result) == pytest.approx(float(expected), rel=1e-12, abs=1e-12)
        else:
            assert result == expected


class TestJavaScriptSemantics:
    """The places where the language is deliberately not Python."""

    def test_round_takes_halves_upward(self, context):
        # Python rounds halves to even, so its round(2.5) is 2.
        assert evaluate_number("round(2.5)", context) == 3
        assert evaluate_number("round(-2.5)", context) == -2

    def test_remainder_keeps_the_dividends_sign(self, context):
        # Python's % keeps the divisor's sign, giving 2 here.
        assert evaluate_number("-1 % 3", context) == -1
        # ...and the language's own mod() is the non-negative one.
        assert evaluate_number("mod(-1, 3)", context) == 2

    def test_unary_minus_binds_tighter_than_power(self, context):
        assert evaluate_number("-2 ** 2", context) == 4

    def test_power_is_right_associative(self, context):
        assert evaluate_number("2 ** 3 ** 2", context) == 512

    def test_comparisons_are_numbers(self, context):
        assert evaluate_expr("1 < 2", context) == 1.0
        assert evaluate_expr("1 > 2", context) == 0.0

    def test_or_returns_an_operand_not_a_boolean(self, context):
        # Scenes use `a || fallback`, which needs the value, not True.
        assert evaluate_expr("0 || 7", context) == 7.0
        assert evaluate_expr("5 || 7", context) == 5.0


class TestStrictness:
    def test_an_unknown_identifier_fails(self, context):
        with pytest.raises(ConfigError, match="unknown identifier"):
            evaluate_expr("speed * 2", context)

    def test_an_unknown_function_fails(self, context):
        with pytest.raises(ConfigError, match="unknown identifier"):
            evaluate_expr("wobble(1)", context)

    def test_a_missing_param_names_what_is_defined(self, context):
        with pytest.raises(ConfigError, match="params do not define"):
            evaluate_expr("params.nonesuch", context)

    def test_dunder_access_is_refused(self, context):
        # The one place a hostile scene could try to reach the interpreter.
        with pytest.raises(ConfigError, match="not readable"):
            evaluate_expr("params.__class__", context)

    def test_a_syntax_error_names_the_expression(self, context):
        with pytest.raises(ConfigError):
            evaluate_expr("1 +", context)
        with pytest.raises(ConfigError):
            evaluate_expr("1 ) 2", context)

    def test_a_stray_character_is_refused(self, context):
        with pytest.raises(ConfigError, match="unexpected character"):
            evaluate_expr("1 @ 2", context)

    def test_a_non_numeric_result_where_a_number_is_needed(self, context):
        with pytest.raises(ConfigError, match="a number was needed"):
            evaluate_number("fixed(1.5, 2)", context)


class TestHostValues:
    def test_the_clock_and_canvas_are_readable(self, context):
        assert evaluate_number("time", context) == FIXTURE["context"]["time"]
        assert evaluate_number("width", context) == FIXTURE["context"]["width"]
        # dt is MILLISECONDS in this language — the format's own quirk, kept
        # so an expression written in the studio means the same here.
        assert evaluate_number("dt", context) == pytest.approx(16.6666, abs=1e-3)

    def test_host_variables_shadow_nothing_they_should_not(self):
        # A repeat's index variable is visible; the rest of the language is
        # unchanged around it.
        context = EvalContext(params={}, vars={"i": 3.0})
        assert evaluate_number("i * 2", context) == 6.0
        assert evaluate_number("pi > 3", context) == 1.0


class TestWithAlpha:
    """The one string-valued function: a fill can be
    ``{"expr": "withAlpha(params.ink, 0.12)"}`` — a translucent colour derived
    from a colour parameter — without the scene format growing string
    operations. The values below are what illusion-studio's own `evalExpr`
    returns for the same sources.
    """

    def test_a_hex_colour_becomes_rgba(self, context):
        assert evaluate_expr("withAlpha('#f59e0b', 0.12)", context) == "rgba(245, 158, 11, 0.12)"

    def test_an_integral_alpha_prints_without_a_decimal_point(self):
        # JavaScript renders 1.0 as "1"; Python's str would give "1.0".
        assert evaluate_expr("withAlpha('#f59e0b', 1)", EvalContext()) == "rgba(245, 158, 11, 1)"

    def test_anything_that_is_not_a_six_digit_hex_passes_through(self):
        assert evaluate_expr("withAlpha('tomato', 0.5)", EvalContext()) == "tomato"
        assert evaluate_expr("withAlpha('#abc', 0.5)", EvalContext()) == "#abc"

    def test_the_result_is_a_colour_the_renderer_accepts(self):
        from alhazen.scenes.render import parse_color

        text = evaluate_expr("withAlpha('#204080', 0.25)", EvalContext())
        rgb, alpha = parse_color(str(text))

        assert rgb.tolist() == [32, 64, 128]
        assert alpha == pytest.approx(0.25)

    def test_it_reads_a_colour_out_of_the_params(self):
        context = EvalContext(params={"ink": "#112233"})
        assert evaluate_expr("withAlpha(params.ink, 0.5)", context) == "rgba(17, 34, 51, 0.5)"


class TestParamReferences:
    def test_it_finds_what_a_scene_asks_for(self):
        from alhazen.scenes.expr import param_references

        assert param_references("params.a + sin(params.b * time)") == {"a", "b"}

    def test_a_reference_in_an_untaken_branch_still_counts(self):
        from alhazen.scenes.expr import param_references

        assert param_references("time > 1e9 ? params.never : params.always") == {
            "never",
            "always",
        }

    def test_an_expression_reading_no_params_returns_nothing(self):
        from alhazen.scenes.expr import param_references

        assert param_references("sin(time) * width") == set()
