import hashlib
from decimal import Decimal, InvalidOperation


def normalize_value(value) -> str:
    if value is None:
        return ""

    # Normalize strings
    if isinstance(value, str):
        value = value.strip().lower()

        value = (
            value
            .replace("\r\n", " ")
            .replace("\n", " ")
            .replace("\r", " ")
            .strip()
            .lower()
        )

        # Try numeric normalization for string numbers
        try:
            num = Decimal(value)
            return normalize_number(num)
        except InvalidOperation:
            return value

    # Normalize numbers
    if isinstance(value, (int, float, Decimal)):
        return normalize_number(Decimal(str(value)))

    # Normalize booleans
    if isinstance(value, bool):
        return "true" if value else "false"

    # Fallback
    return str(value).strip().lower()


def normalize_number(num: Decimal) -> str:
    """
    Converts 1, 1.0, 1.00 → '1'
    Converts 1.2300 → '1.23'
    """
    num = num.normalize()
    # Remove exponent notation
    return format(num, 'f')


def create_hash_for_row(row: dict) -> str:
    payload = "|".join(
        f"{k.lower()}={normalize_value(row[k])}"
        for k in sorted(row)
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()