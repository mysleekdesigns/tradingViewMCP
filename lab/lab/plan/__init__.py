"""Declarative strategy plans: JSON schema + validator, safe rule expressions, compiler.

Results are historical and not forward-looking.
"""

from lab.plan.compile import compile_plan, compute_features
from lab.plan.expr import (
    CheckedExpr,
    ExprScope,
    check_expression,
    crosses_above,
    crosses_below,
    evaluate_expression,
    prev,
)
from lab.plan.schema import plan_json_schema
from lab.plan.validator import (
    PlanValidationError,
    checked_expressions,
    format_errors,
    parse_plan,
    plan_hash,
    plan_to_dict,
    tunable_params,
    validate_plan,
    with_params,
)

__all__ = [
    "CheckedExpr",
    "ExprScope",
    "PlanValidationError",
    "check_expression",
    "checked_expressions",
    "compile_plan",
    "compute_features",
    "crosses_above",
    "crosses_below",
    "evaluate_expression",
    "format_errors",
    "parse_plan",
    "plan_hash",
    "plan_json_schema",
    "plan_to_dict",
    "prev",
    "tunable_params",
    "validate_plan",
    "with_params",
]
