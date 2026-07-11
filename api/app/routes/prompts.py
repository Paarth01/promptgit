from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.database import get_db
from app.db_models import Prompt, PromptAuditLog, PromptVersion
from app.services import versioning
from app.services.serving import invalidate_cache

router = APIRouter(prefix="/prompts", tags=["registry"])


@router.post("", response_model=schemas.PromptOut, status_code=201)
def create_prompt(body: schemas.PromptCreate, db: Session = Depends(get_db)):
    existing = db.query(Prompt).filter(Prompt.slug == body.slug).first()
    if existing:
        raise HTTPException(409, f"prompt '{body.slug}' already exists")
    return versioning.create_prompt(db, body.slug, body.description)


@router.get("", response_model=list[schemas.PromptOut])
def list_prompts(db: Session = Depends(get_db)):
    return db.query(Prompt).order_by(Prompt.created_at.desc()).all()


@router.get("/{slug}", response_model=schemas.PromptOut)
def get_prompt(slug: str, db: Session = Depends(get_db)):
    try:
        return versioning.get_prompt_by_slug(db, slug)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))


@router.post("/{slug}/versions", response_model=schemas.PromptVersionOut, status_code=201)
def create_version(slug: str, body: schemas.PromptVersionCreate, db: Session = Depends(get_db)):
    try:
        version = versioning.create_version(
            db, slug,
            prompt_text=body.prompt_text,
            few_shot_examples=body.few_shot_examples,
            params=body.params,
            template_variables=body.template_variables,
            commit_message=body.commit_message,
            created_by=body.created_by,
            activate=body.activate,
        )
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))
    except versioning.SchemaDriftError as e:
        raise HTTPException(422, str(e))
    invalidate_cache(slug)
    return version


@router.get("/{slug}/versions", response_model=list[schemas.PromptVersionOut])
def list_versions(slug: str, db: Session = Depends(get_db)):
    try:
        prompt = versioning.get_prompt_by_slug(db, slug)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))
    return (
        db.query(PromptVersion)
        .filter(PromptVersion.prompt_id == prompt.id)
        .order_by(PromptVersion.version_number.desc())
        .all()
    )


@router.get("/{slug}/versions/{version_number}", response_model=schemas.PromptVersionOut)
def get_version(slug: str, version_number: int, db: Session = Depends(get_db)):
    try:
        return versioning.get_version_by_number(db, slug, version_number)
    except (versioning.PromptNotFound, versioning.VersionNotFound) as e:
        raise HTTPException(404, str(e))


@router.get("/{slug}/active", response_model=schemas.PromptVersionOut)
def get_active_version(slug: str, db: Session = Depends(get_db)):
    try:
        prompt = versioning.get_prompt_by_slug(db, slug)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))
    if not prompt.active_version_id:
        raise HTTPException(404, f"prompt '{slug}' has no active version")
    return db.query(PromptVersion).filter(PromptVersion.id == prompt.active_version_id).one()


@router.get("/{slug}/diff", response_model=schemas.DiffResponse)
def diff(slug: str, from_: int, to: int, db: Session = Depends(get_db)):
    try:
        return versioning.diff_versions(db, slug, from_, to)
    except (versioning.PromptNotFound, versioning.VersionNotFound) as e:
        raise HTTPException(404, str(e))


@router.post("/{slug}/activate", response_model=schemas.PromptOut)
def activate(slug: str, body: schemas.ActivateRequest, db: Session = Depends(get_db)):
    try:
        prompt = versioning.activate_version(db, slug, body.version_id, body.actor, body.reason)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))
    except versioning.VersionNotFound as e:
        raise HTTPException(400, str(e))
    invalidate_cache(slug)
    return prompt


@router.get("/{slug}/audit-log", response_model=list[schemas.AuditLogEntry])
def audit_log(slug: str, db: Session = Depends(get_db)):
    try:
        prompt = versioning.get_prompt_by_slug(db, slug)
    except versioning.PromptNotFound as e:
        raise HTTPException(404, str(e))
    return (
        db.query(PromptAuditLog)
        .filter(PromptAuditLog.prompt_id == prompt.id)
        .order_by(PromptAuditLog.created_at.desc())
        .all()
    )


@router.post("/{slug}/render", response_model=dict)
def render_version(
    slug: str, version_number: int, body: schemas.RenderRequest, db: Session = Depends(get_db)
):
    """Sanity-check endpoint: render a specific version against sample context
    without going through experiment routing. Useful for the side-by-side
    comparison tool in Phase 4."""
    try:
        version = versioning.get_version_by_number(db, slug, version_number)
        rendered = versioning.render(version, body.context)
    except (versioning.PromptNotFound, versioning.VersionNotFound) as e:
        raise HTTPException(404, str(e))
    except (versioning.SchemaDriftError, versioning.MissingVariablesError) as e:
        raise HTTPException(422, str(e))
    return {"rendered": rendered}
