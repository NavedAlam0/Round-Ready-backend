from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.interview import Interview
from app.models.score import InterviewScore
from app.models.user import User
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

STREAK_STATUSES = {"completed", "evaluated"}
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SKILL_FOCUS_STATUSES = {"completed", "evaluated"}


def _timezone_from_name(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _longest_streak_days(practice_dates: set) -> int:
    if not practice_dates:
        return 0

    longest = 0
    for day in practice_dates:
        if day - timedelta(days=1) in practice_dates:
            continue

        current = day
        count = 1
        while current + timedelta(days=1) in practice_dates:
            current += timedelta(days=1)
            count += 1

        longest = max(longest, count)

    return longest


def _current_streak_days(practice_dates: set, today) -> int:
    if not practice_dates:
        return 0

    cursor = today
    if cursor not in practice_dates:
        cursor = today - timedelta(days=1)
        if cursor not in practice_dates:
            return 0

    count = 0
    while cursor in practice_dates:
        count += 1
        cursor -= timedelta(days=1)

    return count


@router.get("/streak")
async def get_practice_streak(
    timezone_name: str = Query("UTC", alias="timezone"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return practice streak data for the authenticated user."""
    user_tz = _timezone_from_name(timezone_name)
    now = datetime.now(user_tz)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())

    result = await db.execute(
        select(Interview.created_at)
        .where(
            Interview.user_id == current_user.id,
            Interview.status.in_(STREAK_STATUSES),
        )
    )

    practice_dates = {
        _as_timezone_aware(created_at).astimezone(user_tz).date()
        for created_at in result.scalars().all()
    }

    active_weekdays = [
        WEEKDAY_KEYS[index]
        for index in range(7)
        if week_start + timedelta(days=index) in practice_dates
    ]

    last_practice_date = max(practice_dates).isoformat() if practice_dates else None

    return {
        "current_days": _current_streak_days(practice_dates, today),
        "longest_days": _longest_streak_days(practice_dates),
        "active_weekdays": active_weekdays,
        "last_practice_date": last_practice_date,
        "timezone": user_tz.key,
    }


@router.get("/skill-focus")
async def get_skill_focus(
    limit: int = Query(20, ge=1, le=20),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the user's lowest-scoring interview categories across evaluated sessions."""
    result = await db.execute(
        select(
            InterviewScore.category,
            func.avg(InterviewScore.score).label("average_score"),
            func.count(InterviewScore.id).label("sample_count"),
        )
        .join(Interview, Interview.id == InterviewScore.interview_id)
        .where(
            Interview.user_id == current_user.id,
            Interview.status.in_(SKILL_FOCUS_STATUSES),
        )
        .group_by(InterviewScore.category)
        .order_by(func.avg(InterviewScore.score).asc(), InterviewScore.category.asc())
        .limit(limit)
    )

    skills = [
        {
            "category": category,
            "average_score": round(float(average_score), 1),
            "sample_count": int(sample_count),
        }
        for category, average_score, sample_count in result.all()
    ]

    return {"skills": skills}
