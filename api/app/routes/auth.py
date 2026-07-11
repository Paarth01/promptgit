from uuid import UUID

from app import schemas
from app.auth import generate_key, hash_key, require_admin, require_viewer
from app.database import get_db
from app.db_models import ApiKey
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/keys", response_model=schemas.ApiKeyCreated, status_code=201)
def create_key(
    body: schemas.ApiKeyCreate,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_admin),
):
    raw_key = generate_key()
    key_row = ApiKey(
        name=body.name,
        key_hash=hash_key(raw_key),
        role=body.role,
        created_by=caller.name if hasattr(caller, "name") else "admin",
    )
    db.add(key_row)
    db.commit()
    db.refresh(key_row)
    return schemas.ApiKeyCreated(id=key_row.id, name=key_row.name, role=key_row.role, api_key=raw_key)


@router.get("/keys", response_model=list[schemas.ApiKeyOut])
def list_keys(db: Session = Depends(get_db), caller: ApiKey = Depends(require_admin)):
    return db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: UUID, db: Session = Depends(get_db), caller: ApiKey = Depends(require_admin)):
    from datetime import datetime, timezone

    key_row = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key_row:
        raise HTTPException(404, "key not found")
    key_row.revoked_at = datetime.now(timezone.utc)
    db.commit()


@router.get("/whoami", response_model=schemas.WhoAmI)
def whoami(caller: ApiKey = Depends(require_viewer)):
    return schemas.WhoAmI(
        name=getattr(caller, "name", "dev-mode"),
        role=caller.role,
    )
