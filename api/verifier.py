"""
Label verification core.

Gemini's job: read the label image and extract raw values.
Python's job: apply the compliance rules (see rules.py).

Gemini extracts:
  - Raw text for brand name and class/type (exactly as printed)
  - Normalised number for ABV and net contents (so formatting
    variants like "45% Alc./Vol." vs "45% ALC/VOL" never cause
    false failures — the model resolves the visual noise)
  - Verbatim transcription of the government warning
  - An optional note on anything it observes about each field

Python (rules.py) then applies:
  - Case-insensitive string match for brand name and class/type
  - Numeric equality for ABV and net contents
  - Verbatim body + all-caps header check for the warning
"""

import os
import time
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from api.models import ApplicationData, Status, VerificationResult
from api.rules import (
    check_government_warning,
    check_numeric_field,
    check_text_field,
)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Extraction schema — what we ask Gemini to fill in
# ---------------------------------------------------------------------------

class ExtractedTextField(BaseModel):
    """A text field transcribed verbatim from the label."""
    value_on_label: Optional[str] = Field(
        None,
        description="The value exactly as printed on the label, or null if absent.",
    )
    note: Optional[str] = Field(
        None,
        description=(
            "One short sentence noting anything unusual or different compared "
            "to the application value. Null if nothing notable."
        ),
    )


class ExtractedNumericField(BaseModel):
    """A numeric field where the model normalises away formatting."""
    raw_value: Optional[str] = Field(
        None,
        description="The value exactly as printed on the label, or null if absent.",
    )
    normalised_value: Optional[str] = Field(
        None,
        description=(
            "The value with formatting stripped to its essential parts "
            "for comparison. For ABV: just the percentage number and unit, "
            "e.g. '45%'. For net contents: number and unit, e.g. '750 mL'. "
            "Null if absent."
        ),
    )
    normalised_expected: Optional[str] = Field(
        None,
        description=(
            "The application value normalised the same way as normalised_value, "
            "so both sides can be compared as strings. "
            "For ABV: e.g. '45%'. For net contents: e.g. '750 mL'."
        ),
    )
    note: Optional[str] = Field(
        None,
        description=(
            "One short sentence noting anything unusual or different compared "
            "to the application value. Null if nothing notable."
        ),
    )


class Extraction(BaseModel):
    image_is_readable_label: bool = Field(
        description="False if the image is not a readable alcohol beverage label.",
    )
    image_issues: list[str] = Field(
        default_factory=list,
        description="Image quality problems: glare, blur, angle, cropping, low light.",
    )
    brand_name: ExtractedTextField
    class_type: ExtractedTextField
    alcohol_content: ExtractedNumericField
    net_contents: ExtractedNumericField
    government_warning_text: Optional[str] = Field(
        None,
        description=(
            "VERBATIM transcription of the government warning statement, "
            "preserving the original capitalization exactly, character for "
            "character, including the 'GOVERNMENT WARNING:' header if present. "
            "Null if no warning appears on the label."
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
# Gemini call
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are assisting a TTB compliance agent reviewing an alcohol beverage label.
The application data the agent submitted is below. Your job is to extract
values from the label image — not to judge whether they match.

Application data (for reference only):
  brand_name:       {brand_name}
  class_type:       {class_type}
  alcohol_content:  {alcohol_content}
  net_contents:     {net_contents}

Extraction rules:
- brand_name and class_type: transcribe exactly as printed, including case.
- alcohol_content: raw_value is exactly as printed; normalised_value is the
  percentage only e.g. "45%" (strip "Alc./Vol.", proof statements, etc.);
  normalised_expected is the application value normalised the same way.
- net_contents: raw_value is exactly as printed; normalised_value is number
  and unit only e.g. "750 mL" (strip any extra text);
  normalised_expected is the application value normalised the same way.
- government_warning_text: verbatim, character-for-character transcription
  including the "GOVERNMENT WARNING:" header if present. Do not correct or
  paraphrase — transcribe exactly what is printed.
- For any note field: one short sentence about anything notable or different
  from the application value. Omit if nothing to say.
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
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            temperature=0,
        ),
    )
    return response.parsed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def verify_label(
    image_bytes: bytes, mime_type: str, app: ApplicationData
) -> VerificationResult:
    start = time.perf_counter()
    extraction = _extract(image_bytes, mime_type, app)

    if not extraction.image_is_readable_label:
        elapsed = time.perf_counter() - start
        return VerificationResult(
            overall=Status.NEEDS_REVIEW,
            fields=[],
            government_warning=check_government_warning(None, None),
            image_readable=False,
            image_issues=extraction.image_issues or [
                "Image could not be read as an alcohol beverage label. "
                "Request a clearer image."
            ],
            processing_seconds=round(elapsed, 2),
            model=GEMINI_MODEL,
        )

    fields = [
        check_text_field(
            "Brand name", app.brand_name,
            extraction.brand_name.value_on_label,
            extraction.brand_name.note,
        ),
        check_text_field(
            "Class / type", app.class_type,
            extraction.class_type.value_on_label,
            extraction.class_type.note,
        ),
        check_numeric_field(
            "Alcohol content", app.alcohol_content,
            extraction.alcohol_content.normalised_expected,
            extraction.alcohol_content.normalised_value,
            extraction.alcohol_content.raw_value,
            extraction.alcohol_content.note,
        ),
        check_numeric_field(
            "Net contents", app.net_contents,
            extraction.net_contents.normalised_expected,
            extraction.net_contents.normalised_value,
            extraction.net_contents.raw_value,
            extraction.net_contents.note,
        ),
    ]
    warning = check_government_warning(
        extraction.government_warning_text,
        extraction.government_warning_header_bold,
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