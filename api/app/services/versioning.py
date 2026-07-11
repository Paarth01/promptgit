"""Phase 1: prompt registry — versioning, rollback, diff."""

import difflib
import re
from uuid import UUID

from app.db_models import Prompt, PromptAuditLog, PromptVersion
from sqlalchemy import func
from sqlalchemy.orm import Session


class PromptNotFound(Exception):
    pass


class VersionNotFound(Exception):
    pass


class SchemaDriftError(Exception):
    """Raised when prompt_text's {vars} don't match declared template_variables."""

    pass


class MissingVariablesError(Exception):
    pass


def get_prompt_by_slug(db: Session, slug: str) -> Prompt:
    prompt = db.query(Prompt).filter(Prompt.slug == slug).first()
    if not prompt:
        raise PromptNotFound(f"no prompt with slug '{slug}'")
    return prompt


def create_prompt(db: Session, slug: str, description: str | None) -> Prompt:
    prompt = Prompt(slug=slug, description=description)
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    return prompt


def create_version(
    db: Session,
    slug: str,
    prompt_text: str,
    few_shot_examples: list[dict],
    params: dict,
    template_variables: list[str],
    commit_message: str,
    created_by: str,
    activate: bool = False,
) -> PromptVersion:
    """Insert a new immutable version. Validates {var} usage matches declared
    template_variables at commit time (not just at serve time) to catch
    schema drift as early as possible."""
    prompt = get_prompt_by_slug(db, slug)

    used_vars = set(re.findall(r"\{(\w+)\}", prompt_text))
    declared_vars = set(template_variables)
    if used_vars != declared_vars:
        raise SchemaDriftError(
            f"prompt_text references {sorted(used_vars - declared_vars) or '{}'} "
            f"not in template_variables, and/or declares "
            f"{sorted(declared_vars - used_vars) or '{}'} that aren't used in the text."
        )

    # Next version number, scoped to this prompt, race-safe via row lock.
    db.query(Prompt).filter(Prompt.id == prompt.id).with_for_update().one()
    next_version = (
        db.query(func.coalesce(func.max(PromptVersion.version_number), 0))
        .filter(PromptVersion.prompt_id == prompt.id)
        .scalar()
    ) + 1

    version = PromptVersion(
        prompt_id=prompt.id,
        version_number=next_version,
        prompt_text=prompt_text,
        few_shot_examples=few_shot_examples,
        params=params,
        template_variables=template_variables,
        commit_message=commit_message,
        created_by=created_by,
    )
    db.add(version)
    db.flush()  # get version.id without committing yet

    db.add(
        PromptAuditLog(
            prompt_id=prompt.id,
            action="create_version",
            from_version_id=prompt.active_version_id,
            to_version_id=version.id,
            actor=created_by,
            reason=commit_message,
        )
    )

    if activate or prompt.active_version_id is None:
        prompt.active_version_id = version.id
        if not (activate and prompt.active_version_id is None):
            # log activation explicitly if it wasn't just "first version auto-activates"
            pass

    db.commit()
    db.refresh(version)
    return version


def activate_version(db: Session, slug: str, version_id: UUID, actor: str, reason: str) -> Prompt:
    """Rollback / promote: an O(1) pointer flip, fully audit-logged.
    No redeploy — the serving layer resolves active_version_id on every call
    (behind a short-TTL cache)."""
    prompt = get_prompt_by_slug(db, slug)
    version = (
        db.query(PromptVersion)
        .filter(PromptVersion.id == version_id, PromptVersion.prompt_id == prompt.id)
        .first()
    )
    if not version:
        raise VersionNotFound(f"version {version_id} does not belong to prompt '{slug}'")

    previous = prompt.active_version_id
    is_rollback = (
        previous is not None
        and db.query(PromptVersion).filter(PromptVersion.id == previous).first()
        and version.version_number
        < db.query(PromptVersion.version_number).filter(PromptVersion.id == previous).scalar()
    )

    prompt.active_version_id = version.id
    db.add(
        PromptAuditLog(
            prompt_id=prompt.id,
            action="rollback" if is_rollback else "activate",
            from_version_id=previous,
            to_version_id=version.id,
            actor=actor,
            reason=reason,
        )
    )
    db.commit()
    db.refresh(prompt)
    return prompt


def get_version_by_number(db: Session, slug: str, version_number: int) -> PromptVersion:
    prompt = get_prompt_by_slug(db, slug)
    version = (
        db.query(PromptVersion)
        .filter(PromptVersion.prompt_id == prompt.id, PromptVersion.version_number == version_number)
        .first()
    )
    if not version:
        raise VersionNotFound(f"prompt '{slug}' has no version {version_number}")
    return version


def diff_versions(db: Session, slug: str, from_num: int, to_num: int) -> dict:
    v_from = get_version_by_number(db, slug, from_num)
    v_to = get_version_by_number(db, slug, to_num)

    text_diff = list(
        difflib.unified_diff(
            v_from.prompt_text.splitlines(keepends=True),
            v_to.prompt_text.splitlines(keepends=True),
            fromfile=f"v{v_from.version_number}",
            tofile=f"v{v_to.version_number}",
        )
    )

    all_param_keys = set(v_from.params or {}) | set((v_to.params or {}))
    params_diff = {
        k: {"from": (v_from.params or {}).get(k), "to": (v_to.params or {}).get(k)}
        for k in all_param_keys
        if (v_from.params or {}).get(k) != (v_to.params or {}).get(k)
    }

    from_vars, to_vars = set(v_from.template_variables), set(v_to.template_variables)

    return {
        "from_version": from_num,
        "to_version": to_num,
        "prompt_text_diff": "".join(text_diff),
        "few_shot_examples_changed": v_from.few_shot_examples != v_to.few_shot_examples,
        "params_diff": params_diff,
        "template_variables_diff": {
            "added": sorted(to_vars - from_vars),
            "removed": sorted(from_vars - to_vars),
        },
    }


def render(version: PromptVersion, context: dict) -> str:
    """Serve-time validation: catches schema drift and missing caller-supplied
    variables before the prompt ever reaches the LLM."""
    used_vars = set(re.findall(r"\{(\w+)\}", version.prompt_text))
    declared_vars = set(version.template_variables)
    if used_vars != declared_vars:
        raise SchemaDriftError(
            f"version {version.version_number}: prompt_text vars {sorted(used_vars)} "
            f"don't match declared template_variables {sorted(declared_vars)}"
        )
    missing = declared_vars - set(context.keys())
    if missing:
        raise MissingVariablesError(f"missing required variables: {sorted(missing)}")
    return version.prompt_text.format(**context)
