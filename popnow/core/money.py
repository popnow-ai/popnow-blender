"""US dollar amounts from the PopNow API (standard library only, no bpy).

The API bills in exact USD from the API balance behind the key. Amounts arrive as decimal strings with up to nine decimal
places ("0.0405"); they are read into decimal.Decimal and never into a float, so nothing is rounded on the way.
The integer credit fields (1 credit = $0.001) are deprecated display projections: they are read only when a response
carries no USD field, which is how older servers and older task history look.
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal
from typing import Any

# One deprecated credit in USD
CREDIT_USD = Decimal("0.001")
# The smallest amount the API bills
NANO_USD = Decimal("0.000000001")

_USD = re.compile(r"^-?\d+(\.\d{1,9})?$")


def parse_usd(value: Any) -> Decimal | None:
    """An exact USD decimal string ("0.0405", up to nine decimals, no exponent) as a Decimal; None for anything else"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _USD.match(text):
        return None
    return Decimal(text)


def credits_to_usd(value: Any) -> Decimal | None:
    """A deprecated whole-credit amount in USD (1 credit = $0.001)"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return Decimal(value) * CREDIT_USD


def first_usd(*values: Any) -> Decimal | None:
    """The first readable USD string: the exact field first, then its legacy aliases"""
    for v in values:
        d = parse_usd(v)
        if d is not None:
            return d
    return None


def first_usd_text(*values: Any) -> str:
    """Same as first_usd, but returns the server's own string unchanged ("" when none is readable)"""
    for v in values:
        if parse_usd(v) is not None:
            return v.strip()
    return ""


def usd_text(amount: Decimal | None) -> str:
    """Plain decimal text for storage: no exponent, no trailing zeros ("0.0405", "5"); "" for None"""
    if amount is None:
        return ""
    q = amount.quantize(NANO_USD, rounding=ROUND_CEILING)
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_usd(amount: Decimal | None) -> str:
    """For people: "$0.0405", "$5.00", "$1,250.50", "-$1.50". Every significant digit, at least two decimals"""
    if amount is None:
        return "?"
    text = usd_text(amount)
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    return f"{sign}${int(whole):,}.{frac.ljust(2, '0')}"


def usd_to_credits(amount: Decimal) -> int:
    """Whole deprecated credits for a USD amount, rounded up (the old display unit)"""
    return int((amount / CREDIT_USD).to_integral_value(rounding=ROUND_CEILING))
