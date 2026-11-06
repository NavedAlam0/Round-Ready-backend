import asyncio
import hmac
import json
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.models.user import User
from app.models.interview import Interview
from app.models.score import InterviewScore, InterviewReport
from app.routers.auth import get_current_user
from app.schemas.interview import InterviewSetupRequest, InterviewSetupResponse, CodeSyncRequest, BotCallbackRequest
from app.services.question_planner import generate_interview_plan
from app.services.coding_planner import generate_coding_problem
from app.services.report_completion import (
    DomainFinalSubmissionConflict, complete_from_callback, end_completed_interview,
    finalize_saved_code, request_report, save_code_submission,
)
from app.services.report_jobs import InterviewNotFoundError, ReportJobNotReadyError
from app.services.report_progress import get_report_progress
from app.services.daily_helpers import create_daily_room, get_daily_token
from app.config import settings

router = APIRouter(prefix="/api/interview", tags=["interview"])


def _voice_agent_payload(interview: Interview, bot_token: str, room_url: str) -> dict:
    payload = {
        "room_url": room_url,
        "bot_token": bot_token,
        "interview_id": interview.id,
        "interview_plan": interview.interview_plan,
        "interview_mode": interview.interview_mode,
        "resume_text": interview.resume_text,
        "job_description": interview.job_description,
        "company_info": interview.company_info,
        "role_type": interview.role_type,
        "level": interview.level,
        "years_of_experience": interview.years_of_experience,
        "employment_type": interview.employment_type,
    }
    if interview.interview_mode == "coding":
        payload["coding_problem"] = interview.coding_problem
        payload["coding_language"] = interview.coding_language
    return payload


async def _start_voice_agent(interview: Interview, bot_token: str, room_url: str) -> str | None:
    payload = _voice_agent_payload(interview, bot_token, room_url)

    if settings.voice_agent_provider == "pipecat_cloud":
        if not settings.pipecat_api_key:
            raise ValueError("PIPECAT_API_KEY is not configured")

        url = (
            f"{settings.pipecat_api_base_url.rstrip('/')}/v1/public/"
            f"{settings.pipecat_agent_name}/start"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.pipecat_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "createDailyRoom": False,
                    "transport": "daily",
                    "body": payload,
                },
            )
            if not res.is_success:
                raise ValueError(f"Pipecat Cloud returned {res.status_code}: {res.text}")

        response = res.json()
        session_id = response.get("sessionId")
        logger.info(
            f"Pipecat Cloud agent started for interview {interview.id}; "
            f"session_id={session_id}"
        )
        return session_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(f"{settings.voice_agent_url}/start-bot", json=payload)
        if not res.is_success:
            raise ValueError(f"Voice agent returned {res.status_code}: {res.text}")
    return None


async def _stop_voice_agent(interview: Interview) -> None:
    if settings.voice_agent_provider == "pipecat_cloud":
        if not interview.pipecat_session_id:
            return
        if not settings.pipecat_private_api_key:
            logger.warning("PIPECAT_PRIVATE_API_KEY is not configured; cannot stop Pipecat session")
            return
        url = (
            f"{settings.pipecat_api_base_url.rstrip('/')}/v1/agents/"
            f"{settings.pipecat_agent_name}/sessions/{interview.pipecat_session_id}"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.delete(
                url,
                headers={"Authorization": f"Bearer {settings.pipecat_private_api_key}"},
            )
            if res.status_code not in (200, 404):
                raise ValueError(f"Pipecat Cloud stop returned {res.status_code}: {res.text}")
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{settings.voice_agent_url}/stop-bot/{interview.id}")


@router.get("s")
async def list_interviews(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all interviews for the current user, newest first."""
    result = await db.execute(
        select(Interview)
        .where(Interview.user_id == current_user.id)
        .order_by(Interview.created_at.desc())
    )
    interviews = result.scalars().all()

    items = []
    for iv in interviews:
        # Check if report exists
        report_result = await db.execute(
            select(InterviewReport).where(InterviewReport.interview_id == iv.id)
        )
        report = report_result.scalar_one_or_none()

        items.append({
            "id": iv.id,
            "status": iv.status,
            "role_type": iv.role_type,
            "level": iv.level,
            "employment_type": iv.employment_type,
            "interview_mode": iv.interview_mode,
            "coding_language": iv.coding_language,
            "coding_problem_title": iv.coding_problem.get("title") if iv.coding_problem else None,
            "company_name": iv.company_info.get("name") if iv.company_info else None,
            "overall_score": report.overall_score if report else None,
            "created_at": iv.created_at.isoformat(),
        })

    return {"interviews": items}


@router.delete("/{interview_id}")
async def delete_interview(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an interview and its associated scores and report."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    # Delete associated scores and report first
    await db.execute(
        select(InterviewScore).where(InterviewScore.interview_id == interview_id)
    )
    scores = (await db.execute(
        select(InterviewScore).where(InterviewScore.interview_id == interview_id)
    )).scalars().all()
    for s in scores:
        await db.delete(s)

    report = (await db.execute(
        select(InterviewReport).where(InterviewReport.interview_id == interview_id)
    )).scalar_one_or_none()
    if report:
        await db.delete(report)

    await db.delete(interview)
    await db.commit()

    return {"detail": "Interview deleted"}


@router.post("/setup", response_model=InterviewSetupResponse)
async def setup_interview(
    data: InterviewSetupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    interview = Interview(
        user_id=current_user.id,
        resume_text=data.resume_text,
        job_description=data.job_description,
        company_info=data.company_info,
        role_type=data.role_type,
        level=data.level,
        years_of_experience=data.years_of_experience,
        employment_type=data.employment_type,
        interview_mode=data.interview_mode,
        coding_language=data.coding_language,
        status="setup",
    )
    db.add(interview)
    await db.commit()
    await db.refresh(interview)

    return InterviewSetupResponse(interview_id=interview.id, status=interview.status)


@router.post("/{interview_id}/plan")
async def create_interview_plan(
    interview_id: str,
    regenerate: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    # If plan already exists and not regenerating, return existing
    if interview.interview_plan and not regenerate:
        return {
            "interview_id": interview.id,
            "status": interview.status,
            "plan": interview.interview_plan,
            "context": {
                "role_type": interview.role_type,
                "level": interview.level,
                "years_of_experience": interview.years_of_experience,
                "employment_type": interview.employment_type,
                "company_name": interview.company_info.get("name") if interview.company_info else None,
            },
        }

    try:
        plan = await generate_interview_plan(
            resume_text=interview.resume_text,
            job_description=interview.job_description,
            company_info=interview.company_info,
            role_type=interview.role_type,
            level=interview.level,
            years_of_experience=interview.years_of_experience,
            employment_type=interview.employment_type,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate plan: {str(e)}")

    interview.interview_plan = plan
    interview.status = "planned"
    await db.commit()

    return {
        "interview_id": interview.id,
        "status": "planned",
        "plan": plan,
        "context": {
            "role_type": interview.role_type,
            "level": interview.level,
            "years_of_experience": interview.years_of_experience,
            "employment_type": interview.employment_type,
            "company_name": interview.company_info.get("name") if interview.company_info else None,
        },
    }


@router.get("/{interview_id}/plan")
async def get_interview_plan(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if not interview.interview_plan:
        raise HTTPException(status_code=404, detail="Plan not generated yet")

    return {
        "interview_id": interview.id,
        "status": interview.status,
        "plan": interview.interview_plan,
        "context": {
            "role_type": interview.role_type,
            "level": interview.level,
            "years_of_experience": interview.years_of_experience,
            "employment_type": interview.employment_type,
            "company_name": interview.company_info.get("name") if interview.company_info else None,
        },
    }


@router.post("/{interview_id}/coding-plan")
async def create_coding_plan(
    interview_id: str,
    regenerate: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a coding problem for a coding round interview."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if interview.interview_mode != "coding":
        raise HTTPException(status_code=400, detail="This interview is not a coding round")

    # Return existing if not regenerating
    if interview.coding_problem and not regenerate:
        return {
            "interview_id": interview.id,
            "status": interview.status,
            "problem": interview.coding_problem,
            "context": {
                "role_type": interview.role_type,
                "level": interview.level,
                "years_of_experience": interview.years_of_experience,
                "employment_type": interview.employment_type,
                "coding_language": interview.coding_language,
                "company_name": interview.company_info.get("name") if interview.company_info else None,
            },
        }

    # Fetch previous coding problem titles for this user to avoid repeats
    prev_result = await db.execute(
        select(Interview.coding_problem).where(
            Interview.user_id == current_user.id,
            Interview.interview_mode == "coding",
            Interview.coding_problem.isnot(None),
            Interview.id != interview_id,
        )
    )
    previous_problems = []
    for (prob,) in prev_result.all():
        if isinstance(prob, dict) and prob.get("title"):
            previous_problems.append(prob["title"])

    try:
        problem = await generate_coding_problem(
            role_type=interview.role_type,
            level=interview.level,
            years_of_experience=interview.years_of_experience,
            employment_type=interview.employment_type,
            coding_language=interview.coding_language,
            previous_problems=previous_problems if previous_problems else None,
            resume_text=interview.resume_text,
            company_info=interview.company_info,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate coding problem: {str(e)}")

    interview.coding_problem = problem
    interview.status = "planned"
    await db.commit()

    return {
        "interview_id": interview.id,
        "status": "planned",
        "problem": problem,
        "context": {
            "role_type": interview.role_type,
            "level": interview.level,
            "years_of_experience": interview.years_of_experience,
            "employment_type": interview.employment_type,
            "coding_language": interview.coding_language,
            "company_name": interview.company_info.get("name") if interview.company_info else None,
        },
    }


@router.get("/{interview_id}/coding-plan")
async def get_coding_plan(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the coding problem for a coding round interview."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if not interview.coding_problem:
        raise HTTPException(status_code=404, detail="Coding problem not generated yet")

    return {
        "interview_id": interview.id,
        "status": interview.status,
        "problem": interview.coding_problem,
        "saved_code": interview.final_code,
        "context": {
            "role_type": interview.role_type,
            "level": interview.level,
            "years_of_experience": interview.years_of_experience,
            "employment_type": interview.employment_type,
            "coding_language": interview.coding_language,
            "company_name": interview.company_info.get("name") if interview.company_info else None,
        },
    }


def _completion_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InterviewNotFoundError):
        return HTTPException(status_code=404, detail="Interview not found")
    return HTTPException(status_code=409, detail=str(exc))


@router.post("/{interview_id}/code-sync")
async def sync_code(
    interview_id: str,
    data: CodeSyncRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a backup or atomically accept the final coding submission."""
    try:
        job = await save_code_submission(
            db, interview_id, user_id=current_user.id, code=data.code,
            language=data.language, is_final=data.is_final,
        )
        await db.commit()
    except (InterviewNotFoundError, ReportJobNotReadyError, DomainFinalSubmissionConflict) as exc:
        raise _completion_error(exc) from exc
    return {"status": "ok", "report_status": job.status if job else None}


@router.post("/{interview_id}/code-finalize")
async def finalize_code_backup(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Explicit user action to submit the last server backup after a lost draft."""
    user_id = current_user.id
    try:
        await finalize_saved_code(db, interview_id, user_id=user_id)
        await db.commit()
        return await get_report_progress(db, interview_id, user_id=user_id)
    except (InterviewNotFoundError, ReportJobNotReadyError, DomainFinalSubmissionConflict) as exc:
        raise _completion_error(exc) from exc


@router.get("/{interview_id}/status")
async def get_interview_status(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current status of an interview."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    return {"status": interview.status, "interview_mode": interview.interview_mode}


@router.post("/{interview_id}/start")
async def start_interview(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if interview.status in ("completed", "evaluated", "incomplete", "failed"):
        raise HTTPException(status_code=400, detail="Interview already completed")

    if interview.status == "in_progress":
        # Rejoining must not stop the current bot and start another one on the
        # same interview ID: its old callback could freeze the wrong transcript.
        if not interview.daily_room_url:
            raise HTTPException(status_code=409, detail="Interview connection is still being prepared")
        room_url = interview.daily_room_url
        await db.commit()
        try:
            token = await get_daily_token(room_url.rstrip("/").rsplit("/", 1)[-1], is_owner=False)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Could not reconnect to the active interview") from exc
        return {"room_url": room_url, "token": token, "status": "in_progress"}

    if interview.interview_mode == "coding":
        if not interview.coding_problem:
            raise HTTPException(status_code=400, detail="Coding problem not generated yet")
    else:
        if not interview.interview_plan:
            raise HTTPException(status_code=400, detail="Interview plan not generated yet")

    # Credit check — super admins bypass
    if not current_user.is_super_admin:
        if current_user.credits_remaining <= 0:
            raise HTTPException(
                status_code=403,
                detail="No credits remaining. Please upgrade your plan or buy a top-up.",
            )
        # Deduct 1 credit
        current_user.credits_remaining -= 1
        db.add(current_user)
        await db.commit()
        await db.refresh(current_user)
        logger.info(f"Deducted 1 credit for user {current_user.id}, remaining: {current_user.credits_remaining}")

    # Create Daily room and get tokens
    try:
        room = await create_daily_room()
        bot_token = await get_daily_token(room["name"], is_owner=True)
        user_token = await get_daily_token(room["name"], is_owner=False)
    except Exception as e:
        logger.exception(f"Daily.co API error for interview {interview_id}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create room: {type(e).__name__}: {e}")

    # Update interview
    interview.daily_room_url = room["url"]
    interview.pipecat_session_id = None
    interview.status = "in_progress"
    await db.commit()

    # Call voice agent server to start the bot
    try:
        session_id = await _start_voice_agent(interview, bot_token, room["url"])
        if session_id:
            interview.pipecat_session_id = session_id
            await db.commit()
        logger.info(f"Voice agent started for interview {interview_id}")
    except Exception as e:
        logger.error(f"Failed to start voice agent: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start voice agent: {e}")

    return {
        "room_url": room["url"],
        "token": user_token,
        "status": "in_progress",
    }


@router.post("/{interview_id}/end")
async def end_interview(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        interview = await end_completed_interview(db, interview_id, user_id=current_user.id)
        await db.commit()
    except (InterviewNotFoundError, ReportJobNotReadyError) as exc:
        raise _completion_error(exc) from exc

    # Release the transaction before stopping the bot: its callback may need the
    # same interview row while this network request is still in progress.
    status = interview.status
    if status == "completed":
        try:
            await _stop_voice_agent(interview)
        except Exception as exc:
            logger.warning("Could not stop interview {} bot ({})", interview_id, type(exc).__name__)
    return {"interview_id": interview_id, "status": status}


@router.get("/{interview_id}/transcript")
async def get_transcript(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    return {"transcript": interview.transcript or []}


@router.post("/{interview_id}/bot-callback")
async def bot_callback(
    interview_id: str,
    data: BotCallbackRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Save the voice transcript and its report job in one transaction."""
    secret = request.headers.get("X-Bot-Secret", "")
    if not settings.daily_api_key or not hmac.compare_digest(secret, settings.daily_api_key):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        job = await complete_from_callback(
            db, interview_id,
            transcript=[entry.model_dump() for entry in data.transcript], status=data.status,
        )
        await db.commit()
    except (InterviewNotFoundError, ReportJobNotReadyError) as exc:
        raise _completion_error(exc) from exc
    return {"status": "ok", "report_status": job.status if job else None}


@router.post("/{interview_id}/evaluate", status_code=202)
async def trigger_evaluation(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Idempotently enqueue evaluation; the worker performs the AI request."""
    user_id = current_user.id
    try:
        await request_report(db, interview_id, user_id=user_id)
        await db.commit()
        progress = await get_report_progress(db, interview_id, user_id=user_id)
    except (InterviewNotFoundError, ReportJobNotReadyError) as exc:
        raise _completion_error(exc) from exc
    return {"interview_id": interview_id, **progress}


async def _report_event_stream(request, hub, interview_id: str, user_id: str):
    deadline = time.monotonic() + 300
    previous = None
    # Subscribe BEFORE reading the snapshot, so a commit between subscribing and
    # reading cannot be missed. Reconnects always re-read durable current state.
    with hub.watch(interview_id) as changed:
        while time.monotonic() < deadline:
            changed.clear()
            try:
                async with async_session() as session:
                    progress = await get_report_progress(session, interview_id, user_id=user_id)
            except InterviewNotFoundError:
                yield 'event: report_status\ndata: {"status":"failed","message":"Interview is no longer available."}\n\n'
                return
            except Exception as exc:
                logger.warning("Report stream interrupted for {} ({})", interview_id, type(exc).__name__)
                return  # Client reconnects and re-reads persisted progress.
            payload = json.dumps(progress, separators=(",", ":"))
            if payload != previous:
                yield f"event: report_status\ndata: {payload}\n\n"
                previous = payload
            if progress["status"] in ("ready", "failed", "incomplete"):
                return
            while not changed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(changed.wait(), timeout=min(15, remaining))
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"


@router.get("/{interview_id}/report/events")
async def report_events(
    interview_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated report updates; report contents are fetched when ready."""
    user_id = current_user.id
    try:
        await get_report_progress(db, interview_id, user_id=user_id)
    except InterviewNotFoundError as exc:
        raise _completion_error(exc) from exc
    # Auth and ownership checks share the dependency session. Return its pooled
    # connection before opening a stream that can remain connected for minutes.
    await db.close()
    hub = getattr(request.app.state, "report_notifications", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="Report notifications are temporarily unavailable")
    return StreamingResponse(
        _report_event_stream(request, hub, interview_id, user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{interview_id}/report")
async def get_report(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the evaluation report for an interview."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    # Get report
    report_result = await db.execute(
        select(InterviewReport).where(InterviewReport.interview_id == interview_id)
    )
    report = report_result.scalar_one_or_none()
    if not report:
        progress = await get_report_progress(db, interview_id, user_id=current_user.id)
        if progress["status"] != "ready":
            return progress
        # The worker may have committed between the first report lookup and the
        # progress lookup. A ready HTTP response must always include the report.
        report = await db.scalar(select(InterviewReport).where(
            InterviewReport.interview_id == interview_id,
        ))
        if report is None:
            return {"status": "generating", "message": "The report is being prepared for download."}

    # Get scores
    scores_result = await db.execute(
        select(InterviewScore).where(InterviewScore.interview_id == interview_id)
    )
    scores = scores_result.scalars().all()

    return {
        "status": "ready",
        "report": {
            "overall_score": report.overall_score,
            "strengths": report.strengths,
            "weaknesses": report.weaknesses,
            "detailed_feedback": report.detailed_feedback,
            "recommendations": report.recommendations,
            "question_answers": report.question_answers or [],
            "confidence_analysis": report.confidence_analysis,
            "skill_assessment": report.skill_assessment or [],
            "code_review": report.code_review,
            "category_scores": [
                {"category": s.category, "score": s.score, "feedback": s.feedback}
                for s in scores
            ],
            "created_at": report.created_at.isoformat(),
        },
        "context": {
            "role_type": interview.role_type,
            "level": interview.level,
            "years_of_experience": interview.years_of_experience,
            "employment_type": interview.employment_type,
            "company_name": interview.company_info.get("name") if interview.company_info else None,
            "interview_id": interview.id,
            "interview_mode": interview.interview_mode,
            "coding_language": interview.coding_language,
            "coding_problem": interview.coding_problem,
            "final_code": interview.final_code,
        },
    }
