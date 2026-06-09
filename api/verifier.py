"""
Label verification core.

Design decision: we split the work between the LLM and plain Python.

  - Gemini (vision) EXTRACTS text from the label image and makes fuzzy
    equivalence judgments where human-style judgment is appropriate
    (e.g. "STONE'S THROW" on the label vs "Stone's Throw" in the
    application is the same brand).

  - Python performs the GOVERNMENT WARNING check deterministically.
    27 CFR Part 16 requires the warning verbatim, with "GOVERNMENT
    WARNING:" in capital letters. A legally exact requirement should
    not depend on a model's judgment, so we transcribe the warning with
    Gemini and compare it to the canonical text in code.
"""

import os
import re
import time
from enum import Enum
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Canonical government warning text (27 CFR 16.21)
# ---------------------------------------------------------------------------

# Per 27 CFR 16.22, only "GOVERNMENT WARNING" must be in caps and bold.
# Split into header + body so we can check them independently.
WARNING_HEADER = "GOVERNMENT WARNING:"
WARNING_BODY = (
    "(1) According to the Surgeon General, women should not drink alcoholic "
    "beverages during pregnancy because of the risk of birth defects. "
    "(2) Consumption of alcoholic beverages impairs your ability to drive a "
    "car or operate machinery, and may cause health problems."
)
GOVERNMENT_WARNING = f"{WARNING_HEADER} {WARNING_BODY}"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# ---------------------------------------------------------------------------
# Schema Gemini fills in (structured output)
# ---------------------------------------------------------------------------


class ExtractedField(BaseModel):
    """One label field as read by the model, with a fuzzy comparison."""

    value_on_label: Optional[str] = Field(
        None, description="The value exactly as printed on the label, or null if absent."
    )
    matches_application: Optional[bool] = Field(
        None,
        description=(
            "Whether the label value and the application value refer to the "
            "same thing, ignoring case, punctuation and formatting "
            "differences. Null if the field is absent from the label."
        ),
    )
    note: Optional[str] = Field(
        None, description="One short sentence explaining any difference. Null if none."
    )


class Extraction(BaseModel):
    image_is_readable_label: bool = Field(
        description="False if the image is not a readable alcohol beverage label."
    )
    image_issues: list[str] = Field(
        default_factory=list,
        description="Quality problems: glare, blur, angle, cropping, low light.",
    )
    brand_name: ExtractedField
    class_type: ExtractedField
    alcohol_content: ExtractedField
    net_contents: ExtractedField
    government_warning_text: Optional[str] = Field(
        None,
        description=(
            "VERBATIM transcription of the government warning statement, "
            "preserving the original capitalization exactly, character for "
            "character. Null if no warning appears."
        ),
    )
    government_warning_header_bold: Optional[bool] = Field(
        None,
        description=(
            "Whether the 'GOVERNMENT WARNING:' header appears in bold type. "
            "Null if you cannot tell."
        ),
    )


# ---------------------------------------------------------------------------
# Result models returned to the frontend
# ---------------------------------------------------------------------------


class Status(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_FOUND = "not_found"
    NEEDS_REVIEW = "needs_review"


class FieldResult(BaseModel):
    field: str
    expected: str
    found: Optional[str]
    status: Status
    note: Optional[str] = None


class WarningResult(BaseModel):
    status: Status
    present: bool
    wording_exact: bool
    header_all_caps: bool
    header_bold: Optional[bool]
    extracted_text: Optional[str]
    issues: list[str]


class VerificationResult(BaseModel):
    overall: Status
    fields: list[FieldResult]
    government_warning: WarningResult
    image_readable: bool
    image_issues: list[str]
    processing_seconds: float
    model: str


class ApplicationData(BaseModel):
    brand_name: str
    class_type: str
    alcohol_content: str
    net_contents: str


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are assisting a TTB compliance agent. The image is the artwork for an
alcohol beverage label. The agent's application data is below. Read the label
carefully and fill in the response schema.

Application data:
  brand_name: {brand_name}
  class_type: {class_type}
  alcohol_content: {alcohol_content}
  net_contents: {net_contents}

Rules:
- value_on_label must be transcribed exactly as printed, including case.
- matches_application is a judgment call: treat values as matching when they
  clearly refer to the same thing despite case, punctuation, or formatting
  differences (e.g. "45% Alc./Vol." vs "45% ALC/VOL", "STONE'S THROW" vs
  "Stone's Throw"). Treat them as NOT matching when the substance differs
  (different number, different name, different size).
- government_warning_text must be a character-exact transcription of the
  ENTIRE warning statement including the "GOVERNMENT WARNING:" header if
  present. Do not correct it to the official wording; transcribe what is
  actually printed, header and all. If the header is absent from the label,
  transcribe only the body text that is present.
"""


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


def _extract(image_bytes: bytes, mime_type: str, app: ApplicationData) -> Extraction:
    prompt = _PROMPT_TEMPLATE.format(**app.model_dump())
    response = _client().models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Extraction,
            # Disable "thinking" — extraction doesn't need it and the
            # latency budget is ~5 seconds end to end.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            temperature=0,
        ),
    )
    return response.parsed


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace/newlines; labels wrap the warning freely."""
    return re.sub(r"\s+", " ", text).strip()


def check_government_warning(
    extracted: Optional[str], header_bold: Optional[bool]
) -> WarningResult:
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

    found = _normalize_whitespace(extracted)
    canonical_body = _normalize_whitespace(WARNING_BODY)

    # Strip the header (any casing) before checking body wording.
    found_body = re.sub(r"^government warning:\s*", "", found, flags=re.IGNORECASE)

    # --- body check ---
    body_ok = found_body == canonical_body
    body_wrong_case = (not body_ok) and found_body.lower() == canonical_body.lower()

    # --- header check ---
    has_header = found.lower().startswith("government warning:")
    header_all_caps = found.startswith("GOVERNMENT WARNING:")

    # Overall status: both must pass for MATCH.
    if body_ok and header_all_caps:
        status = Status.MATCH
    elif body_ok and not has_header:
        # Body is correct but header absent from transcription — ambiguous,
        # could be a Gemini omission or genuinely missing from the label.
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


def _field_result(name: str, expected: str, ext: ExtractedField) -> FieldResult:
    if ext.value_on_label is None:
        return FieldResult(
            field=name,
            expected=expected,
            found=None,
            status=Status.NOT_FOUND,
            note="Not found on the label.",
        )

    # Cheap deterministic pass first: exact or case/whitespace-insensitive.
    a = _normalize_whitespace(expected).lower()
    b = _normalize_whitespace(ext.value_on_label).lower()
    if a == b:
        status = Status.MATCH
    elif ext.matches_application:
        # Model judged them equivalent despite formatting differences —
        # surface as a match but keep the note so the agent sees why.
        status = Status.MATCH
    elif ext.matches_application is False:
        status = Status.MISMATCH
    else:
        status = Status.NEEDS_REVIEW

    return FieldResult(
        field=name,
        expected=expected,
        found=ext.value_on_label,
        status=status,
        note=ext.note,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_label(image_bytes: bytes, mime_type: str, app: ApplicationData) -> VerificationResult:
    start = time.perf_counter()
    extraction = _extract(image_bytes, mime_type, app)

    if not extraction.image_is_readable_label:
        elapsed = time.perf_counter() - start
        return VerificationResult(
            overall=Status.NEEDS_REVIEW,
            fields=[],
            government_warning=check_government_warning(None, None),
            image_readable=False,
            image_issues=extraction.image_issues
            or ["Image could not be read as an alcohol beverage label. Request a clearer image."],
            processing_seconds=round(elapsed, 2),
            model=GEMINI_MODEL,
        )

    fields = [
        _field_result("Brand name", app.brand_name, extraction.brand_name),
        _field_result("Class / type", app.class_type, extraction.class_type),
        _field_result("Alcohol content", app.alcohol_content, extraction.alcohol_content),
        _field_result("Net contents", app.net_contents, extraction.net_contents),
    ]
    warning = check_government_warning(
        extraction.government_warning_text, extraction.government_warning_header_bold
    )

    statuses = [f.status for f in fields] + [warning.status]
    if any(s in (Status.MISMATCH, Status.NOT_FOUND) for s in statuses):
        overall = Status.MISMATCH
    elif any(s == Status.NEEDS_REVIEW for s in statuses):
        overall = Status.NEEDS_REVIEW
    else:
        overall = Status.MATCH

    elapsed = time.perf_counter() - start
    return VerificationResult(
        overall=overall,
        fields=fields,
        government_warning=warning,
        image_readable=True,
        image_issues=extraction.image_issues,
        processing_seconds=round(elapsed, 2),
        model=GEMINI_MODEL,
    )
