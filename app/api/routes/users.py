from datetime import UTC, date, datetime, time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    get_company_id,
    get_current_user,
    get_db,
    require_admin,
    require_admin_manager,
)
from app.core.security import get_password_hash
from app.models.branch import Branch
from app.models.location_ping import LocationPing
from app.models.user import User, UserRole
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services.plan_limits import enforce_can_create
from app.services.route_service import haversine_km

router = APIRouter()

# Only archive a new breadcrumb once the collector has moved this far (km),
# so standing still at a house doesn't fill the history with duplicate points.
MIN_MOVE_KM = 0.025  # 25 meters


class LocationInput(BaseModel):
    latitude: Decimal = Field(ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal = Field(ge=-180, le=180, max_digits=9, decimal_places=6)


class CollectorLocation(BaseModel):
    id: int
    full_name: str
    last_lat: Decimal | None
    last_lng: Decimal | None
    last_location_at: datetime | None


class TrackPoint(BaseModel):
    latitude: Decimal
    longitude: Decimal
    recorded_at: datetime


class TrackResponse(BaseModel):
    user: UserRead
    date: date
    points: list[TrackPoint]
    distance_km: float
    started_at: datetime | None
    ended_at: datetime | None


@router.post("/me/location", status_code=status.HTTP_204_NO_CONTENT)
def update_my_location(
    payload: LocationInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Opt-in live tracking: the mobile app posts the user's current position.

    Updates the user's last position (for the live map) and appends a breadcrumb
    to the location history when the user has moved far enough from the last one.
    """
    now = datetime.now(UTC)
    current_user.last_lat = payload.latitude
    current_user.last_lng = payload.longitude
    current_user.last_location_at = now

    last_ping = db.scalar(
        select(LocationPing)
        .where(LocationPing.user_id == current_user.id)
        .order_by(LocationPing.recorded_at.desc())
    )
    moved_enough = last_ping is None or haversine_km(
        float(last_ping.latitude), float(last_ping.longitude),
        float(payload.latitude), float(payload.longitude),
    ) >= MIN_MOVE_KM
    if moved_enough:
        db.add(LocationPing(
            user_id=current_user.id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            recorded_at=now,
        ))

    db.commit()


@router.get("/{user_id}/track", response_model=TrackResponse)
def collector_track(
    user_id: int,
    track_date: date = Query(alias="date"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> dict:
    """The route a collector actually traveled on a given day (breadcrumb trail)."""
    collector = db.scalar(select(User).where(User.id == user_id, User.company_id == company_id))
    if collector is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    day_start = datetime.combine(track_date, time.min, tzinfo=UTC)
    day_end = datetime.combine(track_date, time.max, tzinfo=UTC)
    pings = db.scalars(
        select(LocationPing)
        .where(
            LocationPing.user_id == user_id,
            LocationPing.recorded_at >= day_start,
            LocationPing.recorded_at <= day_end,
        )
        .order_by(LocationPing.recorded_at)
    ).all()

    distance = 0.0
    for a, b in zip(pings, pings[1:]):
        distance += haversine_km(float(a.latitude), float(a.longitude), float(b.latitude), float(b.longitude))

    return {
        "user": collector,
        "date": track_date,
        "points": [
            {"latitude": p.latitude, "longitude": p.longitude, "recorded_at": p.recorded_at}
            for p in pings
        ],
        "distance_km": round(distance, 2),
        "started_at": pings[0].recorded_at if pings else None,
        "ended_at": pings[-1].recorded_at if pings else None,
    }


@router.get("/locations", response_model=list[CollectorLocation])
def list_collector_locations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> list[User]:
    """Last known position of the company's collectors (for the live map)."""
    statement = (
        select(User)
        .where(
            User.company_id == company_id,
            User.role == UserRole.collector,
            User.is_active.is_(True),
        )
        .order_by(User.full_name)
    )
    return list(db.scalars(statement).all())


@router.get("/collectors", response_model=list[UserRead])
def list_collectors(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> list[User]:
    """Active collectors of the company, for portfolio assignment.

    Available to admins and managers (managers assign collectors when
    creating/editing customers but cannot access the full user list).
    """
    statement = (
        select(User)
        .where(
            User.company_id == company_id,
            User.role == UserRole.collector,
            User.is_active.is_(True),
        )
        .order_by(User.full_name)
    )
    return list(db.scalars(statement).all())


@router.get("", response_model=list[UserRead])
def list_users(
    q: str | None = Query(default=None),
    role: UserRole | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    company_id: int = Depends(get_company_id),
) -> list[User]:
    statement = select(User).where(User.company_id == company_id).order_by(User.full_name)
    users = list(db.scalars(statement).all())

    if role is not None:
        users = [user for user in users if user.role == role]
    if q:
        term = q.lower().strip()
        users = [
            user for user in users
            if term in user.full_name.lower() or term in user.email.lower()
        ]
    return users


def _validate_branch(db: Session, branch_id: int | None, company_id: int) -> int | None:
    if branch_id is None:
        return None
    branch = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id))
    if branch is None:
        raise HTTPException(status_code=404, detail="La sucursal no existe.")
    return branch.id


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    company_id: int = Depends(get_company_id),
) -> User:
    existing = db.scalar(select(User).where(User.email == payload.email.lower()))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    enforce_can_create(db, company_id, "user")
    user = User(
        full_name=payload.full_name.strip(),
        email=payload.email.lower(),
        password_hash=get_password_hash(payload.password),
        role=payload.role,
        company_id=company_id,
        branch_id=_validate_branch(db, payload.branch_id, company_id),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.put("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    company_id: int = Depends(get_company_id),
) -> User:
    user = db.scalar(select(User).where(User.id == user_id, User.company_id == company_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    existing = db.scalar(select(User).where(User.email == payload.email.lower(), User.id != user_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    user.full_name = payload.full_name.strip()
    user.email = payload.email.lower()
    user.role = payload.role
    user.is_active = payload.is_active
    user.branch_id = _validate_branch(db, payload.branch_id, company_id)
    if payload.password:
        user.password_hash = get_password_hash(payload.password)

    db.commit()
    db.refresh(user)
    return user
