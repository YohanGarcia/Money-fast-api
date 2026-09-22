from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.tour_progress import TourProgress, TourStatus
from app.models.user import User
from app.schemas.tour_progress import TourProgressRead, TourProgressUpsert

router = APIRouter()


@router.get("", response_model=list[TourProgressRead])
def list_tour_progress(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TourProgress]:
    stmt = select(TourProgress).where(TourProgress.user_id == current_user.id)
    return db.scalars(stmt).all()


@router.put("/{tour_id}", response_model=TourProgressRead)
def upsert_tour_progress(
    tour_id: str,
    payload: TourProgressUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TourProgress:
    progress = db.scalar(
        select(TourProgress).where(
            TourProgress.user_id == current_user.id,
            TourProgress.tour_id == tour_id,
        )
    )
    if progress is None:
        progress = TourProgress(user_id=current_user.id, tour_id=tour_id)
        db.add(progress)

    progress.status = TourStatus(payload.status)
    progress.current_step = payload.current_step
    progress.tour_version = payload.tour_version
    if progress.status == TourStatus.completed and progress.completed_at is None:
        progress.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(progress)
    return progress


@router.delete("/{tour_id}", status_code=status.HTTP_204_NO_CONTENT)
def reset_tour_progress(
    tour_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    progress = db.scalar(
        select(TourProgress).where(
            TourProgress.user_id == current_user.id,
            TourProgress.tour_id == tour_id,
        )
    )
    if progress is not None:
        db.delete(progress)
        db.commit()
