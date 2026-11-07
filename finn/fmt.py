"""German number formatting."""
from decimal import ROUND_HALF_UP, Decimal


def de(x: Decimal | float | int, decimals: int = 0) -> str:
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    s = f"{d:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def teur(x: Decimal) -> str:
    """T€ amount, shown in T€ with thousands separator."""
    return f"{de(x, 0)} T€"


def ratio(value: Decimal, unit: str, decimals: int) -> str:
    if unit == "%":
        return f"{de(value, decimals)} %"
    return f"{de(value, decimals)}{unit}"


def threshold(raw: str, unit: str) -> str:
    """Threshold exactly as precise as written in the contract (27.5 -> '27,5 %')."""
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    return ratio(Decimal(raw), unit, decimals)


def headroom(value: Decimal, unit: str, decimals: int) -> str:
    if unit == "%":
        return f"{de(value, decimals)} Pp."
    return de(value, decimals)


def op(operator: str) -> str:
    return {">=": "≥", "<=": "≤"}[operator]
