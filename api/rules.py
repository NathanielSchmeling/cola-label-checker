"""
Compliance matching rules.

Gemini extracts raw values from the label image. This module decides
whether each extracted value satisfies the application requirement.

Design principle: Python enforces the rules, the model supplies the
extracted values and any observational notes. No judgment calls here —
if the logic is ambiguous (e.g. formatting variants of ABV) we ask
Gemini to normalise the value before it arrives here.
"""

import re
from typing import Optional

from api.models import FieldResult, Status, WarningResult

# ---------------------------------------------------------------------------
# Canonical government warning text (27 CFR 16.21 / 16.22)
# ---------------------------------------------------------------------------

WARNING_HEADER = "GOVERNMENT WARNING:"
WARNING_BODY = (
    "(1) According to the Surgeon General, women should not drink alcoholic "
    "beverages during pregnancy because of the risk of birth defects. "
    "(2) Consumption of alcoholic beverages impairs your ability to drive a "
    "car or operate machinery, and may cause health problems."
)
GOVERNMENT_WARNING = f"{WARNING_HEADER} {WARNING_BODY}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Collapse whitespace/newlines — labels wrap text freely."""
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------

def check_text_field(
    name: str,
    expected: str,
    found: Optional[str],
    note: Optional[str],
) -> FieldResult:
    """Case-insensitive match for brand name and class/type."""
    if found is None:
        return FieldResult(field=name, expected=expected, found=None,
                           status=Status.NOT_FOUND, note="Not found on the label.")

    status = (
        Status.MATCH
        if _norm(expected).lower() == _norm(found).lower()
        else Status.MISMATCH
    )
    return FieldResult(field=name, expected=expected, found=found,
                       status=status, note=note)


def check_numeric_field(
    name: str,
    expected: str,
    expected_normalised: Optional[str],
    found_normalised: Optional[str],
    found_raw: Optional[str],
    note: Optional[str],
) -> FieldResult:
    """
    Numeric match for ABV and net contents.

    Gemini normalises both the label value and the application value into
    a common form (e.g. "45%" or "750 mL") so formatting variants like
    "45% Alc./Vol. (90 Proof)" vs "45% ALC/VOL" never cause false fails.
    Python just compares the two normalised strings.
    """
    if found_normalised is None or found_raw is None:
        return FieldResult(field=name, expected=expected, found=None,
                           status=Status.NOT_FOUND, note="Not found on the label.")

    # Fall back to raw application value if Gemini didn't normalise it.
    compare_expected = _norm(expected_normalised).lower() if expected_normalised else _norm(expected).lower()
    compare_found = _norm(found_normalised).lower()

    status = Status.MATCH if compare_expected == compare_found else Status.MISMATCH
    return FieldResult(field=name, expected=expected, found=found_raw,
                       status=status, note=note)


# ---------------------------------------------------------------------------
# Government warning rule
# ---------------------------------------------------------------------------

def check_government_warning(
    extracted: Optional[str],
    header_bold: Optional[bool],
) -> WarningResult:
    """
    Two independent checks per 27 CFR 16.21/16.22:
      1. Body wording — must match verbatim (whitespace-normalised).
      2. Header — must be present and read "GOVERNMENT WARNING:" in all caps.
    Bold is best-effort: if the model can't tell, flag for visual review.
    """
    issues: list[str] = []

    if not extracted or not extracted.strip():
        return WarningResult(
            status=Status.NOT_FOUND,
            present=False,
            wording_exact=False,
            header_all_caps=False,
            header_bold=None,
            extracted_text=None,
            issues=["No government warning statement found on the label."],
        )

    found = _norm(extracted)
    canonical_body = _norm(WARNING_BODY)

    # Strip header (any casing) before comparing body wording.
    found_body = re.sub(r"^government warning:\s*", "", found, flags=re.IGNORECASE)

    # --- body check ---
    body_ok = found_body == canonical_body
    body_wrong_case = (not body_ok) and found_body.lower() == canonical_body.lower()

    # --- header check ---
    has_header = found.lower().startswith("government warning:")
    header_all_caps = found.startswith("GOVERNMENT WARNING:")

    if body_ok and header_all_caps:
        status = Status.MATCH
    elif body_ok and not has_header:
        status = Status.NEEDS_REVIEW
        issues.append(
            'Warning body text is correct but the "GOVERNMENT WARNING:" header '
            "was not detected — verify it appears on the label in capital letters "
            "and bold type."
        )
    elif body_ok and has_header and not header_all_caps:
        status = Status.MISMATCH
        issues.append(
            f'"GOVERNMENT WARNING:" must be in all capital letters; '
            f'the label shows "{extracted[:25].strip()}…".'
        )
    elif body_wrong_case:
        status = Status.MISMATCH
        issues.append("Warning body wording is correct but capitalization differs from the required text.")
        if has_header and not header_all_caps:
            issues.append('"GOVERNMENT WARNING:" must be in all capital letters.')
    else:
        status = Status.MISMATCH
        issues.append("Warning body text does not match the required wording verbatim.")
        if has_header and not header_all_caps:
            issues.append('"GOVERNMENT WARNING:" must be in all capital letters.')

    if header_bold is False:
        issues.append('The "GOVERNMENT WARNING:" header does not appear to be bold.')
        if status == Status.MATCH:
            status = Status.NEEDS_REVIEW
    elif header_bold is None and status == Status.MATCH:
        issues.append("Could not confirm the header is bold — verify visually.")

    return WarningResult(
        status=status,
        present=True,
        wording_exact=body_ok,
        header_all_caps=header_all_caps,
        header_bold=header_bold,
        extracted_text=extracted,
        issues=issues,
    )