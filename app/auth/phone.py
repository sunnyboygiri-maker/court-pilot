import re
from typing import Optional


def normalize_phone(value: str) -> Optional[str]:
    """
    Normalize an Indian mobile number to +91XXXXXXXXXX.
    Accepts "98xxxxxxxx", "098xxxxxxxx", "91 98xxx xxxxx", "+91-98xxxxxxxx".
    Returns None if it isn't a valid Indian mobile number.
    """
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None
