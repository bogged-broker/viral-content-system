"""
/utils/math.py

Safe Math (No Floats Unless Proven)

This file is not "some helper arithmetic."
This is where you prevent silent numeric corruption from ever entering your system.

Research-grade. Deterministic. Zero floating-point drift.

WHAT THIS FILE EXISTS FOR (NON-NEGOTIABLE):
math.py is the single authority for numeric operations allowed inside the system.

It exists to eliminate:
- Silent float rounding errors
- Implicit float promotion
- Division producing floats unexpectedly
- Overflow drift assumptions
- Mixed-type arithmetic
- Non-deterministic numeric behavior across runtimes
- Hidden precision loss

It answers:
> "Is this arithmetic operation safe, deterministic, and audit-stable?"

If math lies:
- Aggregation is wrong
- Rates drift
- Replay diverges
- Audit cannot reconcile values

Numeric integrity is systemic integrity.

CORE LAW:
All arithmetic must be explicit, type-safe, and precision-declared.

No implicit float usage. No silent integer → float promotion. No tolerance-based comparisons. No epsilon games.

Either the system can prove the precision is safe — or it rejects.

WHAT THIS FILE IS NOT:
- Not NumPy
- Not scientific computing
- Not probability helpers
- Not statistics library
- Not vectorized math
- Not ML utilities

This is correctness-oriented infrastructure math only.

NUMERIC POLICY:
Type        Allowed                    Notes
int         Yes                        Canonical numeric primitive
Decimal     Yes (explicit only)        Must set context
float       Forbidden by default       Only via explicit safe wrapper
Fraction    Optional                   Deterministic but heavier

All computation layers must import math utilities from here.
Direct + - * / use is allowed only on validated safe types.

DESIGN PRINCIPLES:
1. Integers by default
2. Division must declare precision
3. Float must never appear silently
4. Ratios must return structured type
5. Rounding must be explicit
6. Overflow expectations documented
7. No implicit casting
8. Deterministic across Python versions
9. Deterministic across CPU architectures
10. No reliance on IEEE float quirks

FORBIDDEN OPERATIONS:
Inside system core:
- float division
- a / b unless integer exact
- math.sqrt
- math.log
- implicit int → float conversion
- round() on float
- tolerance-based comparisons
- numpy floats
- automatic Decimal precision drift

If floats appear in core aggregation — reject at validation layer.

FLOAT HANDLING POLICY (STRICT):
If floats must exist at outer boundary:
- Convert explicitly to Decimal at ingestion
- Reject NaN
- Reject Inf
- Reject negative zero
- Reject subnormals
- No float propagation beyond ingestion boundary

Float must die at system edge.

DECIMAL CONTEXT POLICY:
Context must define:
- precision
- rounding mode
- traps (enabled for InvalidOperation, DivisionByZero)

No reliance on global context.
Each operation sets context locally.

DETERMINISM GUARANTEES:
1. Same inputs → same result
2. Same division precision → same decimal result
3. Same rounding mode → same output across machines
4. No architecture-based drift
5. No IEEE edge-case reliance

OVERFLOW POLICY:
Python ints are arbitrary precision.
If business logic requires bounded ints:
- Enforce via require_range upstream
- Math layer does not silently wrap

No int overflow wraparound ever.
"""

from decimal import Decimal, Context, ROUND_HALF_EVEN, InvalidOperation, DivisionByZero, localcontext
from typing import Sequence

from utils.errors import MathError


__all__ = [
    'add_int',
    'sub_int',
    'mul_int',
    'div_int_exact',
    'div_ratio',
    'safe_decimal',
    'percent',
    'clamp_int',
    'mean_int_exact',
    'compare_int',
    'reject_float_contamination',
]


# ============================================================================
# DECIMAL CONTEXT MANAGEMENT
# ============================================================================

def _get_decimal_context(precision: int) -> Context:
    """
    Create deterministic Decimal context.
    
    Guarantees:
        - Explicit precision
        - Deterministic rounding (ROUND_HALF_EVEN)
        - Traps enabled for errors
        - No global context pollution
    
    Args:
        precision: Number of significant digits
        
    Returns:
        Configured Context instance
        
    Raises:
        MathError: If precision < 1 or not int
    """
    if type(precision) is not int:
        raise MathError(
            f"Precision must be exactly int, got {type(precision).__name__}: {precision}",
            code="MATH_INVALID_PRECISION_TYPE"
        )
    
    if precision < 1:
        raise MathError(
            f"Precision must be >= 1, got: {precision}",
            code="MATH_INVALID_PRECISION_RANGE"
        )
    
    ctx = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        traps=[InvalidOperation, DivisionByZero],
    )
    return ctx


# ============================================================================
# TYPE VALIDATION
# ============================================================================

def _validate_int_only(value: int, param_name: str) -> None:
    """
    Enforce strict int type - reject bool, float, Decimal.
    
    Args:
        value: Value to validate
        param_name: Parameter name for error messages
        
    Raises:
        MathError: If value is not exactly int (rejects bool subclass)
    """
    if type(value) is not int:
        raise MathError(
            f"{param_name} must be exactly int, got {type(value).__name__}: {value}",
            code="MATH_TYPE_ERROR",
            details={"param": param_name, "type": type(value).__name__}
        )


def _validate_all_ints(values: Sequence[int], context: str) -> None:
    """
    Validate that all values in sequence are strict ints.
    
    Args:
        values: Sequence to validate
        context: Context string for error messages
        
    Raises:
        MathError: If any value is not exactly int
    """
    for i, value in enumerate(values):
        if type(value) is not int:
            raise MathError(
                f"{context}[{i}] must be exactly int, got {type(value).__name__}: {value}",
                code="MATH_TYPE_ERROR",
                details={"context": context, "index": str(i), "type": type(value).__name__}
            )


# ============================================================================
# INTEGER ARITHMETIC
# ============================================================================

def add_int(a: int, b: int) -> int:
    """
    Type-safe integer addition.
    
    Guarantees:
        - Both operands must be exactly int (not bool)
        - No float promotion
        - Deterministic result
        - No overflow (Python arbitrary precision)
    
    Args:
        a: First operand
        b: Second operand
        
    Returns:
        Sum as int
        
    Raises:
        MathError: If either operand is not exactly int
        
    Example:
        >>> add_int(100, 50)
        150
        >>> add_int(True, 1)  # Raises MathError
    """
    _validate_int_only(a, 'a')
    _validate_int_only(b, 'b')
    return a + b


def sub_int(a: int, b: int) -> int:
    """
    Type-safe integer subtraction.
    
    Guarantees:
        - Both operands must be exactly int
        - No float promotion
        - Deterministic result
    
    Args:
        a: Minuend
        b: Subtrahend
        
    Returns:
        Difference as int
        
    Raises:
        MathError: If either operand is not exactly int
        
    Example:
        >>> sub_int(100, 30)
        70
    """
    _validate_int_only(a, 'a')
    _validate_int_only(b, 'b')
    return a - b


def mul_int(a: int, b: int) -> int:
    """
    Type-safe integer multiplication.
    
    Used in: window sizing, rate scaling, duration multipliers
    
    Guarantees:
        - Both operands must be exactly int
        - No float promotion
        - No overflow wraparound (arbitrary precision)
        - Deterministic result
    
    Args:
        a: First factor
        b: Second factor
        
    Returns:
        Product as int
        
    Raises:
        MathError: If either operand is not exactly int
        
    Example:
        >>> mul_int(1000, 60)
        60000
    """
    _validate_int_only(a, 'a')
    _validate_int_only(b, 'b')
    return a * b


def div_int_exact(a: int, b: int) -> int:
    """
    Exact integer division - rejects non-exact results.
    
    Used in: window alignment, deterministic chunk sizing
    
    Guarantees:
        - Both operands must be exactly int
        - b != 0
        - a % b == 0 (exact division)
        - Returns int, never float
    
    Args:
        a: Dividend
        b: Divisor
        
    Returns:
        Quotient as int
        
    Raises:
        MathError: If either operand is not exactly int
        ZeroDivisionError: If b == 0
        MathError: If division is not exact (remainder exists)
        
    Example:
        >>> div_int_exact(100, 10)
        10
        >>> div_int_exact(100, 3)  # Raises MathError
    """
    _validate_int_only(a, 'a')
    _validate_int_only(b, 'b')
    
    if b == 0:
        raise ZeroDivisionError("Cannot divide by zero")
    
    quotient, remainder = divmod(a, b)
    
    if remainder != 0:
        raise MathError(
            f"Division not exact: {a} / {b} has remainder {remainder}",
            code="MATH_NON_EXACT_DIVISION",
            details={"numerator": str(a), "denominator": str(b), "remainder": str(remainder)}
        )
    
    return quotient


# ============================================================================
# DECIMAL ARITHMETIC
# ============================================================================

def safe_decimal(value: int | str) -> Decimal:
    """
    Safe Decimal instantiation - rejects float input.
    
    All Decimal creation must pass through this function to prevent
    float contamination.
    
    Guarantees:
        - No float input accepted
        - Deterministic conversion
        - Context-independent instantiation
    
    Args:
        value: Integer or string representation
        
    Returns:
        Decimal instance
        
    Raises:
        MathError: If value is float or bool
        InvalidOperation: If string format invalid
        
    Example:
        >>> safe_decimal(42)
        Decimal('42')
        >>> safe_decimal('123.45')
        Decimal('123.45')
        >>> safe_decimal(3.14)  # Raises MathError
    """
    if isinstance(value, float):
        raise MathError(
            f"Float input forbidden for Decimal: {value}. "
            "Use int or str for deterministic precision.",
            code="MATH_FLOAT_FORBIDDEN",
            details={"value": str(value)}
        )
    
    if isinstance(value, bool):
        raise MathError(
            "Bool input forbidden for Decimal",
            code="MATH_BOOL_FORBIDDEN"
        )
    
    if not isinstance(value, (int, str)):
        raise MathError(
            f"Decimal input must be int or str, got {type(value).__name__}",
            code="MATH_INVALID_DECIMAL_TYPE",
            details={"type": type(value).__name__}
        )
    
    return Decimal(value)


def div_ratio(
    numerator: int,
    denominator: int,
    *,
    precision: int,
) -> Decimal:
    """
    Division with explicit precision control using Decimal.
    
    Guarantees:
        - No float intermediates
        - Explicit precision declaration
        - Deterministic rounding (ROUND_HALF_EVEN)
        - Context-local computation
        - Reproducible across platforms
    
    Args:
        numerator: Dividend (must be int)
        denominator: Divisor (must be int, non-zero)
        precision: Decimal precision (significant digits)
        
    Returns:
        Decimal result with specified precision
        
    Raises:
        MathError: If operands not exactly int or precision invalid
        DivisionByZero: If denominator is 0
        
    Example:
        >>> div_ratio(1, 3, precision=10)
        Decimal('0.3333333333')
        >>> div_ratio(22, 7, precision=5)
        Decimal('3.1429')
    """
    _validate_int_only(numerator, 'numerator')
    _validate_int_only(denominator, 'denominator')
    
    if denominator == 0:
        raise DivisionByZero("Cannot divide by zero")
    
    ctx = _get_decimal_context(precision)
    
    with localcontext(ctx):
        num_dec = safe_decimal(numerator)
        den_dec = safe_decimal(denominator)
        result = num_dec / den_dec
    
    return result


def percent(
    numerator: int,
    denominator: int,
    *,
    precision: int,
) -> Decimal:
    """
    Calculate percentage with strict precision control.
    
    Semantics: (numerator / denominator) * 100
    
    Used by: analytics rates, completion metrics
    
    Guarantees:
        - No float arithmetic
        - Explicit precision
        - Deterministic rounding
        - Reproducible results
    
    Args:
        numerator: Numerator value
        denominator: Denominator value (non-zero)
        precision: Decimal precision for result
        
    Returns:
        Percentage as Decimal
        
    Raises:
        MathError: If operands not exactly int
        DivisionByZero: If denominator is 0
        
    Example:
        >>> percent(3, 4, precision=4)
        Decimal('75.00')
        >>> percent(1, 3, precision=6)
        Decimal('33.3333')
    """
    _validate_int_only(numerator, 'numerator')
    _validate_int_only(denominator, 'denominator')
    
    if denominator == 0:
        raise DivisionByZero("Cannot calculate percentage with zero denominator")
    
    ctx = _get_decimal_context(precision)
    
    with localcontext(ctx):
        num_dec = safe_decimal(numerator)
        den_dec = safe_decimal(denominator)
        hundred = safe_decimal(100)
        result = (num_dec / den_dec) * hundred
    
    return result


# ============================================================================
# INTEGER UTILITIES
# ============================================================================

def clamp_int(
    value: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """
    Clamp integer to bounds with explicit enforcement.
    
    Guarantees:
        - Type-safe clamping
        - Bounds validation
        - Deterministic result
    
    Args:
        value: Value to clamp
        min_value: Minimum bound (inclusive), None for no minimum
        max_value: Maximum bound (inclusive), None for no maximum
        
    Returns:
        Clamped value
        
    Raises:
        MathError: If value not exactly int or min_value > max_value
        
    Example:
        >>> clamp_int(150, min_value=0, max_value=100)
        100
        >>> clamp_int(-10, min_value=0)
        0
        >>> clamp_int(50, min_value=0, max_value=100)
        50
    """
    _validate_int_only(value, 'value')
    
    if min_value is not None:
        _validate_int_only(min_value, 'min_value')
    
    if max_value is not None:
        _validate_int_only(max_value, 'max_value')
    
    if min_value is not None and max_value is not None:
        if min_value > max_value:
            raise MathError(
                f"min_value ({min_value}) must be <= max_value ({max_value})",
                code="MATH_INVALID_BOUNDS",
                details={"min_value": str(min_value), "max_value": str(max_value)}
            )
    
    result = value
    
    if min_value is not None and result < min_value:
        result = min_value
    
    if max_value is not None and result > max_value:
        result = max_value
    
    return result


def mean_int_exact(values: Sequence[int]) -> int:
    """
    Calculate exact integer mean - rejects fractional results.
    
    Used in: strict invariant contexts where fractional mean is invalid
    
    Guarantees:
        - All values must be exactly int
        - Sum must divide count exactly
        - No rounding or truncation
        - Deterministic result
    
    Args:
        values: Non-empty sequence of integers
        
    Returns:
        Exact mean as int
        
    Raises:
        MathError: If any value not exactly int or sequence empty or mean not exact
        
    Example:
        >>> mean_int_exact([10, 20, 30])
        20
        >>> mean_int_exact([10, 20, 25])  # Raises ValueError (sum=55, count=3)
    """
    if not values:
        raise MathError(
            "Cannot compute mean of empty sequence",
            code="MATH_EMPTY_SEQUENCE"
        )
    
    _validate_all_ints(values, 'values')
    
    total = sum(values)
    count = len(values)
    
    quotient, remainder = divmod(total, count)
    
    if remainder != 0:
        raise MathError(
            f"Mean not exact: sum {total} / count {count} has remainder {remainder}",
            code="MATH_NON_EXACT_MEAN",
            details={"sum": str(total), "count": str(count), "remainder": str(remainder)}
        )
    
    return quotient


def compare_int(a: int, b: int) -> int:
    """
    Total ordering comparison for integers.
    
    Used in: ordering utilities, sorting keys
    
    Guarantees:
        - Type-safe comparison
        - Total ordering
        - Deterministic result
    
    Args:
        a: First value
        b: Second value
        
    Returns:
        -1 if a < b
         0 if a == b
         1 if a > b
        
    Raises:
        MathError: If either value not exactly int
        
    Example:
        >>> compare_int(10, 20)
        -1
        >>> compare_int(20, 10)
        1
        >>> compare_int(15, 15)
        0
    """
    _validate_int_only(a, 'a')
    _validate_int_only(b, 'b')
    
    if a < b:
        return -1
    elif a > b:
        return 1
    else:
        return 0


# ============================================================================
# FLOAT BOUNDARY HANDLING (INGESTION ONLY)
# ============================================================================

def reject_float_contamination(value: float) -> None:
    """
    Validate float at system boundary before conversion.
    
    Called at ingestion to ensure float safety before Decimal conversion.
    
    Rejects:
        - NaN
        - Infinity
        - Negative zero
        - Subnormal numbers
    
    Args:
        value: Float value to validate
        
    Raises:
        MathError: If value is unsafe (NaN, Inf, negative zero, subnormal)
        
    Note:
        After validation, convert to Decimal via str: safe_decimal(str(value))
        
    This function is called at ingestion boundary to ensure float safety
    before Decimal conversion. Float must die at system edge.
    """
    import math
    
    if math.isnan(value):
        raise MathError(
            "NaN not allowed in system",
            code="MATH_NAN_FORBIDDEN"
        )
    
    if math.isinf(value):
        raise MathError(
            "Infinity not allowed in system",
            code="MATH_INF_FORBIDDEN"
        )
    
    # Reject negative zero
    if value == 0.0 and math.copysign(1.0, value) < 0:
        raise MathError(
            "Negative zero not allowed in system",
            code="MATH_NEGATIVE_ZERO_FORBIDDEN"
        )
    
    # Reject subnormal (denormal) numbers
    if value != 0.0 and abs(value) < 2.2250738585072014e-308:
        raise MathError(
            "Subnormal float not allowed in system",
            code="MATH_SUBNORMAL_FORBIDDEN"
        )


# ============================================================================
# DETERMINISM VERIFICATION
# ============================================================================

def verify_deterministic_division(
    numerator: int,
    denominator: int,
    precision: int,
    expected: str,
) -> bool:
    """
    Verify division produces expected deterministic result.
    
    Used in: testing, replay verification
    
    Args:
        numerator: Dividend
        denominator: Divisor
        precision: Decimal precision
        expected: Expected result as string
        
    Returns:
        True if result matches expected exactly
        
    Example:
        >>> verify_deterministic_division(1, 3, 10, '0.3333333333')
        True
    """
    result = div_ratio(numerator, denominator, precision=precision)
    expected_dec = safe_decimal(expected)
    return result == expected_dec