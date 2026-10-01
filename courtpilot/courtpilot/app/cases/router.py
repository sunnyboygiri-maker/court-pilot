from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.cases import service
from app.cases.schemas import (
    AdvocateSearchResult,
    SearchAdvocateIn,
    SnapshotOut,
    TrackCaseIn,
    TrackedCaseDetailOut,
    TrackedCaseOut,
    UpdateTrackedCaseIn,
)
from models.database import CaseSnapshot, CaseStatus, CourtCase, TrackedCase, User
from models.session import get_db
from scraper.ecourts import ECourtsScraper

router = APIRouter(prefix="/cases", tags=["cases"])


def _http_error(e: service.CaseServiceError) -> HTTPException:
    return HTTPException(e.status_code, str(e))


@router.post("/track", response_model=TrackedCaseOut, status_code=status.HTTP_201_CREATED)
async def track_case(
    body: TrackCaseIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    scraper: ECourtsScraper = Depends(service.get_scraper),
):
    try:
        tracked = await service.track_case(
            db,
            user,
            body.cnr_number,
            scraper,
            label=body.label,
            client_name=body.client_name,
            notes=body.notes,
            priority=body.priority,
            is_petitioner_side=body.is_petitioner_side,
        )
    except service.CaseServiceError as e:
        raise _http_error(e)
    return service.tracked_payload(tracked)


@router.get("/", response_model=list[TrackedCaseOut])
async def list_cases(
    status_filter: Optional[CaseStatus] = Query(default=None, alias="status"),
    hearing_from: Optional[date] = None,
    hearing_to: Optional[date] = None,
    q: Optional[str] = Query(default=None, max_length=100, description="Search CNR, parties, label, client"),
    priority: Optional[int] = Query(default=None, ge=0, le=2),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await service.list_tracked(
        db,
        user.id,
        status=status_filter,
        hearing_from=hearing_from,
        hearing_to=hearing_to,
        search=q,
        priority=priority,
        limit=limit,
        offset=offset,
    )
    return [service.tracked_payload(t) for t in rows]


@router.get("/upcoming", response_model=list[TrackedCaseOut])
async def upcoming_hearings(
    days: int = Query(default=7, ge=1, le=60),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await service.upcoming(db, user.id, days=days)
    return [service.tracked_payload(t) for t in rows]


@router.post("/search-advocate", response_model=list[AdvocateSearchResult])
async def search_advocate(
    body: SearchAdvocateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    scraper: ECourtsScraper = Depends(service.get_scraper),
):
    if bool(body.advocate_name) == bool(body.bar_code):
        raise HTTPException(422, "Provide exactly one of advocate_name or bar_code")
    try:
        results = await scraper.fetch_cases_by_advocate(
            body.advocate_name, body.state, bar_code=body.bar_code
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    tracked_cnrs = set(
        (
            await db.scalars(
                select(CourtCase.cnr_number).join(TrackedCase).where(TrackedCase.user_id == user.id)
            )
        ).all()
    )
    return [
        AdvocateSearchResult(
            cnr_number=r["cnr_number"],
            case_type=r.get("case_type"),
            case_number=r.get("case_number"),
            petitioner=r.get("petitioner"),
            respondent=r.get("respondent"),
            status=r.get("status"),
            next_hearing_date=r.get("next_hearing_date"),
            court_name=r.get("court_name"),
            already_tracked=r["cnr_number"] in tracked_cnrs,
        )
        for r in results
        if r.get("cnr_number")
    ]


@router.get("/{case_id}", response_model=TrackedCaseDetailOut)
async def get_case(
    case_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        tracked = await service.get_tracked(db, user.id, case_id)
    except service.CaseServiceError as e:
        raise _http_error(e)
    snapshots = (
        await db.scalars(
            select(CaseSnapshot)
            .where(CaseSnapshot.case_id == case_id)
            .order_by(CaseSnapshot.captured_at.desc(), CaseSnapshot.id.desc())
            .limit(20)
        )
    ).all()
    payload = service.tracked_payload(tracked, detail=True)
    payload["snapshots"] = [SnapshotOut.model_validate(s) for s in snapshots]
    return payload


@router.put("/{case_id}", response_model=TrackedCaseOut)
async def update_case(
    case_id: int,
    body: UpdateTrackedCaseIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        tracked = await service.get_tracked(db, user.id, case_id)
    except service.CaseServiceError as e:
        raise _http_error(e)
    updates = body.model_dump(exclude_unset=True)
    if updates.get("notify_whatsapp") and not service.has_whatsapp(user):
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "WhatsApp notifications need the WhatsApp add-on")
    for field, value in updates.items():
        setattr(tracked, field, value)
    await db.commit()
    return service.tracked_payload(await service.get_tracked(db, user.id, case_id))


@router.delete("/{case_id}/untrack", status_code=status.HTTP_204_NO_CONTENT)
async def untrack_case(
    case_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await service.untrack_case(db, user.id, case_id)
    except service.CaseServiceError as e:
        raise _http_error(e)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
