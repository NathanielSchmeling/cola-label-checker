"""
Shared Pydantic models used across the API.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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