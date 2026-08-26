"""Pydantic v2 models -- the contract every agent codes against.

Mirrors db/schema.sql. Phase 1 only populates Posting (and only the fields
it's responsible for -- match_category, applied_at, and application_status
are left at their defaults for Phase 2 to fill in later).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

DescriptionSource = Literal["adzuna_snippet", "redirect_url", "company_site"]
MatchCategory = Literal["strong", "mixed", "weak"]
ApplicationStatus = Literal[
    "not_applied", "applied", "interviewing", "rejected", "offer", "withdrawn"
]
EventType = Literal[
    "interview_scheduled", "rejected", "offer", "followup", "applied_confirmation"
]
AgentRunStatus = Literal["success", "partial", "failed"]


class AdzunaResult(BaseModel):
    """One raw result from the Adzuna /search endpoint.

    Adzuna nests company/location as {"display_name": ...} objects and
    returns salary_is_predicted as the string "1"/"0" rather than a bool --
    both flattened/coerced here so the rest of the app works with plain
    values. See explore_adzuna.py for the real observed response shape.
    """

    external_id: str = Field(alias="id")
    title: str
    company: str
    location: Optional[str] = None
    redirect_url: str
    description: Optional[str] = None
    salary_min: Optional[Decimal] = None
    salary_max: Optional[Decimal] = None
    salary_is_predicted: Optional[bool] = None
    created: Optional[datetime] = None

    model_config = {"populate_by_name": True}

    @field_validator("company", mode="before")
    @classmethod
    def _flatten_company(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return value.get("display_name")
        return value

    @field_validator("location", mode="before")
    @classmethod
    def _flatten_location(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return value.get("display_name")
        return value

    @field_validator("salary_is_predicted", mode="before")
    @classmethod
    def _coerce_salary_is_predicted(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value == "1"
        return value


class Posting(BaseModel):
    id: Optional[UUID] = None
    source: str = "adzuna"
    external_id: str
    title: str
    company: str
    location: Optional[str] = None
    url: str
    description: Optional[str] = None
    description_source: DescriptionSource = "adzuna_snippet"
    salary_min: Optional[Decimal] = None
    salary_max: Optional[Decimal] = None
    salary_is_predicted: Optional[bool] = None
    match_category: Optional[MatchCategory] = None
    match_notes: Optional[str] = None
    discovered_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    application_status: ApplicationStatus = "not_applied"


class CompanyResearch(BaseModel):
    company: str
    culture_summary: Optional[str] = None
    products_summary: Optional[str] = None
    sources: list[str] = Field(default_factory=list)
    updated_at: Optional[datetime] = None


class AgentRun(BaseModel):
    id: Optional[int] = None
    agent_name: str
    started_at: datetime
    finished_at: datetime
    status: AgentRunStatus
    items_processed: int = 0
    items_new: int = 0
    error_message: Optional[str] = None


class ApplicationEvent(BaseModel):
    id: Optional[int] = None
    posting_id: UUID
    event_type: EventType
    detected_at: Optional[datetime] = None
    source_email_id: Optional[str] = None
    notes: Optional[str] = None
