import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.models.user import User
from app.models.interview import Interview
from app.models.score import InterviewScore, InterviewReport
from app.routers.auth import get_current_user
from app.schemas.interview import InterviewSetupRequest, InterviewSetupResponse
from app.services.question_planner import generate_interview_plan
from app.services.evaluator import evaluate_interview
from app.voice.daily_helpers import create_daily_room, get_daily_token
from app.config import settings

router = APIRouter(prefix="/api/interview", tags=["interview"])


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
        status="setup",
    )
    db.add(interview)
    await db.commit()
    await db.refresh(interview)

    return InterviewSetupResponse(interview_id=interview.id, status=interview.status)


@router.post("/{interview_id}/plan")
async def create_interview_plan(
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

    if not interview.interview_plan:
        raise HTTPException(status_code=400, detail="Interview plan not generated yet")

    # Reset status so we can restart
    if interview.status == "in_progress":
        # Stop old bot if running
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(f"{settings.voice_agent_url}/stop-bot/{interview_id}")
            except Exception:
                pass
        interview.status = "planned"
        await db.commit()

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
    interview.status = "in_progress"
    await db.commit()

    # Call voice agent server to start the bot
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{settings.voice_agent_url}/start-bot",
                json={
                    "room_url": room["url"],
                    "bot_token": bot_token,
                    "interview_id": interview_id,
                    "interview_plan": interview.interview_plan,
                    "resume_text": interview.resume_text,
                    "job_description": interview.job_description,
                    "company_info": interview.company_info,
                    "role_type": interview.role_type,
                    "level": interview.level,
                    "years_of_experience": interview.years_of_experience,
                    "employment_type": interview.employment_type,
                },
            )
            if not res.is_success:
                raise ValueError(f"Voice agent returned {res.status_code}: {res.text}")
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
    # Stop the bot via voice agent server
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(f"{settings.voice_agent_url}/stop-bot/{interview_id}")
        except Exception:
            pass

    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    interview.status = "completed"
    await db.commit()

    return {"interview_id": interview.id, "status": "completed"}


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


async def _run_evaluation(interview_id: str):
    """Run evaluation on a completed interview and save results to DB."""
    async with async_session() as session:
        result = await session.execute(
            select(Interview).where(Interview.id == interview_id)
        )
        iv = result.scalar_one_or_none()
        if not iv or not iv.transcript:
            logger.warning(f"Cannot evaluate interview {interview_id}: no transcript")
            return

        # Check if report already exists (avoid duplicate)
        existing_report = await session.execute(
            select(InterviewReport).where(InterviewReport.interview_id == interview_id)
        )
        if existing_report.scalar_one_or_none():
            logger.info(f"Report already exists for interview {interview_id}, skipping")
            return

        evaluation = await evaluate_interview(
            transcript=iv.transcript,
            interview_plan=iv.interview_plan or {},
            resume_text=iv.resume_text,
        )

        # Save category scores
        for cat_score in evaluation.get("category_scores", []):
            score = InterviewScore(
                interview_id=interview_id,
                category=cat_score["category"],
                score=cat_score["score"],
                feedback=cat_score.get("feedback"),
            )
            session.add(score)

        # Save report
        report = InterviewReport(
            interview_id=interview_id,
            overall_score=evaluation["overall_score"],
            strengths=evaluation.get("strengths", []),
            weaknesses=evaluation.get("weaknesses", []),
            detailed_feedback=evaluation.get("detailed_feedback", ""),
            recommendations=evaluation.get("recommendations", []),
            question_answers=evaluation.get("question_answers", []),
            confidence_analysis=evaluation.get("confidence_analysis"),
            skill_assessment=evaluation.get("skill_assessment"),
        )
        session.add(report)

        iv.status = "evaluated"
        await session.commit()
        logger.info(f"Evaluation saved for interview {interview_id}, score: {evaluation['overall_score']}")


async def _run_evaluation_safely(interview_id: str):
    try:
        await _run_evaluation(interview_id)
    except Exception as eval_err:
        logger.exception(f"Auto-evaluation failed for {interview_id}: {type(eval_err).__name__}: {eval_err}")


@router.post("/{interview_id}/bot-callback")
async def bot_callback(
    interview_id: str,
    request: Request,
):
    """Callback from the voice agent when the interview ends.

    The voice agent sends the transcript here after the interview is complete.
    Authenticated via a shared secret (Daily API key) to prevent unauthorized calls.
    """
    # Verify the request is from our voice agent
    bot_secret = request.headers.get("X-Bot-Secret", "")
    if bot_secret != settings.daily_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    transcript = body.get("transcript", [])

    async with async_session() as session:
        result = await session.execute(
            select(Interview).where(Interview.id == interview_id)
        )
        iv = result.scalar_one_or_none()
        if not iv:
            raise HTTPException(status_code=404, detail="Interview not found")

        iv.transcript = transcript
        iv.status = "completed"
        await session.commit()
        logger.info(f"Bot callback: interview {interview_id} completed with {len(transcript)} entries")

    # Run evaluation after responding so the voice agent callback does not time out.
    asyncio.create_task(_run_evaluation_safely(interview_id))

    return {"status": "ok"}


@router.post("/{interview_id}/evaluate")
async def trigger_evaluation(
    interview_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger evaluation for a completed interview."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if not interview.transcript:
        raise HTTPException(status_code=400, detail="No transcript available yet")

    # Check if report already exists
    existing = await db.execute(
        select(InterviewReport).where(InterviewReport.interview_id == interview_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Report already exists")

    try:
        await _run_evaluation(interview_id)
    except Exception as e:
        logger.error(f"Evaluation failed for {interview_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {str(e)}")

    return {"interview_id": interview_id, "status": "evaluated"}


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
        return {"status": "pending", "message": "Report is being generated..."}

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
        },
    }
