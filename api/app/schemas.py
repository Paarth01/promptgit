from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ── Prompt Registry ──────────────────────────────────────────────────────

class PromptCreate(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$")
    description: Optional[str] = None


class PromptVersionCreate(BaseModel):
    prompt_text: str
    few_shot_examples: list[dict[str, Any]] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    template_variables: list[str] = Field(default_factory=list)
    commit_message: str
    created_by: str
    activate: bool = False  # if true, immediately becomes the active version


class PromptVersionOut(BaseModel):
    id: UUID
    prompt_id: UUID
    version_number: int
    prompt_text: str
    few_shot_examples: list[dict[str, Any]]
    params: dict[str, Any]
    template_variables: list[str]
    commit_message: str
    created_by: str
    created_at: datetime

    class Config:
        from_attributes = True


class PromptOut(BaseModel):
    id: UUID
    slug: str
    description: Optional[str]
    active_version_id: Optional[UUID]
    created_at: datetime

    class Config:
        from_attributes = True


class ActivateRequest(BaseModel):
    version_id: UUID
    actor: str
    reason: str


class DiffResponse(BaseModel):
    from_version: int
    to_version: int
    prompt_text_diff: str
    few_shot_examples_changed: bool
    params_diff: dict[str, Any]
    template_variables_diff: dict[str, list[str]]


class RenderRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)


class AuditLogEntry(BaseModel):
    id: UUID
    action: str
    from_version_id: Optional[UUID]
    to_version_id: Optional[UUID]
    actor: str
    reason: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ── Experiment Engine ────────────────────────────────────────────────────

class VariantSpec(BaseModel):
    label: str
    prompt_version_id: UUID
    traffic_weight: float = Field(..., ge=0, le=1)
    is_baseline: bool = False


class ExperimentCreate(BaseModel):
    prompt_slug: str
    name: str
    primary_metric: str
    metric_type: str = Field(..., pattern="^(binary|continuous)$")
    target_sample_size: int = Field(..., gt=0)
    min_detectable_effect: Optional[float] = None
    variants: list[VariantSpec]
    created_by: str

    @field_validator("variants")
    @classmethod
    def validate_variants(cls, v: list[VariantSpec]) -> list[VariantSpec]:
        if len(v) < 2:
            raise ValueError("an experiment needs at least 2 variants")
        total = sum(x.traffic_weight for x in v)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"traffic_weight must sum to 1.0, got {total}")
        if sum(1 for x in v if x.is_baseline) != 1:
            raise ValueError("exactly one variant must be marked is_baseline=true")
        labels = [x.label for x in v]
        if len(labels) != len(set(labels)):
            raise ValueError("variant labels must be unique")
        return v


class ExperimentVariantOut(BaseModel):
    id: UUID
    label: str
    prompt_version_id: UUID
    traffic_weight: float
    is_baseline: bool

    class Config:
        from_attributes = True


class ExperimentOut(BaseModel):
    id: UUID
    prompt_id: UUID
    name: str
    primary_metric: str
    metric_type: str
    target_sample_size: int
    min_detectable_effect: Optional[float]
    status: str
    winner_variant_id: Optional[UUID]
    hold_until: Optional[datetime]
    created_by: str
    created_at: datetime
    started_at: Optional[datetime]
    stopped_at: Optional[datetime]
    variants: list[ExperimentVariantOut]

    class Config:
        from_attributes = True


class ServeRequest(BaseModel):
    unit_id: str
    context: dict[str, Any] = Field(default_factory=dict)


class ServeResponse(BaseModel):
    resolved_prompt_text: str
    prompt_version_id: UUID
    version_number: int
    params: dict[str, Any]
    experiment_id: Optional[UUID] = None
    variant_id: Optional[UUID] = None
    variant_label: Optional[str] = None


# ── Metrics ───────────────────────────────────────────────────────────────

class EventCreate(BaseModel):
    unit_id: str
    variant_id: UUID
    latency_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    is_error: bool = False
    primary_metric_value: Optional[float] = None
    custom_metrics: dict[str, Any] = Field(default_factory=dict)


class VariantStats(BaseModel):
    variant_id: UUID
    label: str
    is_baseline: bool
    sample_size: int
    mean_value: Optional[float]
    std_dev: Optional[float]
    error_rate: float
    p_value_vs_baseline: Optional[float]
    is_significant: Optional[bool]
    test_used: Optional[str]
    relative_lift_vs_baseline: Optional[float]


class ExperimentResults(BaseModel):
    experiment_id: UUID
    status: str
    target_sample_size: int
    total_samples: int
    progress_pct: float
    variants: list[VariantStats]
    winner_variant_id: Optional[UUID]
    winner_ready: bool
    winner_reason: Optional[str]
