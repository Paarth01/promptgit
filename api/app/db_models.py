"""SQLAlchemy ORM models — mirrors migrations/001_init.sql exactly."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Prompt(Base):
    __tablename__ = "prompts"

    id = uuid_pk()
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(Text)
    active_version_id = Column(UUID(as_uuid=True), ForeignKey("prompt_versions.id"))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    versions = relationship(
        "PromptVersion",
        back_populates="prompt",
        foreign_keys="PromptVersion.prompt_id",
    )
    active_version = relationship("PromptVersion", foreign_keys=[active_version_id], post_update=True)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("prompt_id", "version_number"),)

    id = uuid_pk()
    prompt_id = Column(UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False)
    version_number = Column(Integer, nullable=False)
    prompt_text = Column(Text, nullable=False)
    few_shot_examples = Column(JSONB, nullable=False, default=list)
    params = Column(JSONB, nullable=False, default=dict)
    template_variables = Column(ARRAY(String), nullable=False, default=list)
    commit_message = Column(Text, nullable=False)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    prompt = relationship("Prompt", back_populates="versions", foreign_keys=[prompt_id])


class PromptAuditLog(Base):
    __tablename__ = "prompt_audit_log"
    __table_args__ = (CheckConstraint("action IN ('create_version','activate','rollback')"),)

    id = uuid_pk()
    prompt_id = Column(UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False)
    action = Column(String, nullable=False)
    from_version_id = Column(UUID(as_uuid=True), ForeignKey("prompt_versions.id"))
    to_version_id = Column(UUID(as_uuid=True), ForeignKey("prompt_versions.id"))
    actor = Column(String, nullable=False)
    reason = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint("metric_type IN ('binary','continuous')"),
        CheckConstraint("status IN ('draft','running','paused','stopped_guardrail','completed')"),
    )

    id = uuid_pk()
    prompt_id = Column(UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    primary_metric = Column(String, nullable=False)
    metric_type = Column(String, nullable=False)
    target_sample_size = Column(Integer, nullable=False)
    min_detectable_effect = Column(Numeric)
    status = Column(String, nullable=False, default="draft")
    winner_variant_id = Column(UUID(as_uuid=True), ForeignKey("experiment_variants.id"))
    hold_until = Column(DateTime(timezone=True))
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    started_at = Column(DateTime(timezone=True))
    stopped_at = Column(DateTime(timezone=True))

    variants = relationship(
        "ExperimentVariant",
        back_populates="experiment",
        foreign_keys="ExperimentVariant.experiment_id",
    )


class ExperimentVariant(Base):
    __tablename__ = "experiment_variants"
    __table_args__ = (
        UniqueConstraint("experiment_id", "label"),
        CheckConstraint("traffic_weight >= 0 AND traffic_weight <= 1"),
    )

    id = uuid_pk()
    experiment_id = Column(
        UUID(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    prompt_version_id = Column(UUID(as_uuid=True), ForeignKey("prompt_versions.id"), nullable=False)
    label = Column(String, nullable=False)
    traffic_weight = Column(Numeric, nullable=False)
    is_baseline = Column(Boolean, nullable=False, default=False)

    experiment = relationship("Experiment", back_populates="variants", foreign_keys=[experiment_id])


class ExperimentAssignment(Base):
    __tablename__ = "experiment_assignments"
    __table_args__ = (UniqueConstraint("experiment_id", "unit_id"),)

    id = uuid_pk()
    experiment_id = Column(
        UUID(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    unit_id = Column(String, nullable=False)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("experiment_variants.id"), nullable=False)
    assigned_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class ExperimentEvent(Base):
    __tablename__ = "experiment_events"

    id = uuid_pk()
    experiment_id = Column(
        UUID(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    variant_id = Column(UUID(as_uuid=True), ForeignKey("experiment_variants.id"), nullable=False)
    unit_id = Column(String, nullable=False)
    latency_ms = Column(Numeric)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost_usd = Column(Numeric)
    is_error = Column(Boolean, nullable=False, default=False)
    primary_metric_value = Column(Numeric)
    custom_metrics = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class ExperimentAnalysisSnapshot(Base):
    __tablename__ = "experiment_analysis_snapshots"

    id = uuid_pk()
    experiment_id = Column(
        UUID(as_uuid=True), ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    variant_id = Column(UUID(as_uuid=True), ForeignKey("experiment_variants.id"), nullable=False)
    sample_size = Column(Integer, nullable=False)
    mean_value = Column(Numeric)
    std_dev = Column(Numeric)
    p_value = Column(Numeric)
    is_significant = Column(Boolean)
    test_used = Column(String)
    min_detectable_effect = Column(Numeric)
    computed_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (CheckConstraint("role IN ('viewer','editor','admin')"),)

    id = uuid_pk()
    name = Column(String, nullable=False)
    key_hash = Column(String, unique=True, nullable=False, index=True)
    role = Column(String, nullable=False)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
