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
from app.schemas.interview import InterviewSetupRequest, InterviewSetupResponse, CodeSyncRequest
from app.services.question_planner import generate_interview_plan
from app.services.coding_planner import generate_coding_problem
from app.services.evaluator import evaluate_interview
from app.services.coding_evaluator import evaluate_coding_interview
from app.voice.daily_helpers import create_daily_room, get_daily_token
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
        "context": {
            "role_type": interview.role_type,
            "level": interview.level,
            "years_of_experience": interview.years_of_experience,
            "employment_type": interview.employment_type,
            "coding_language": interview.coding_language,
            "company_name": interview.company_info.get("name") if interview.company_info else None,
        },
    }


@router.post("/{interview_id}/code-sync")
async def sync_code(
    interview_id: str,
    data: CodeSyncRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the user's current code snapshot (periodic backup + final save)."""
    result = await db.execute(
        select(Interview).where(
            Interview.id == interview_id,
            Interview.user_id == current_user.id,
        )
    )
    interview = result.scalar_one_or_none()
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    interview.final_code = data.code
    if data.language:
        interview.coding_language = data.language
    await db.commit()

    return {"status": "ok"}


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

    if interview.status == "completed":
        raise HTTPException(status_code=400, detail="Interview already completed")

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

    # Reset status so we can restart
    if interview.status == "in_progress":
        # Stop old bot if running
        try:
            await _stop_voice_agent(interview)
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
    # Stop the bot via voice agent server
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
        await _stop_voice_agent(interview)
    except Exception:
        pass

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


def _check_coding_submission(final_code: str | None, coding_problem: dict | None, coding_language: str | None) -> dict:
    """Check if the user wrote enough code beyond the starter template.

    Returns {"sufficient": bool, "reason": str}.
    """
    if not final_code or not final_code.strip():
        return {"sufficient": False, "reason": "No code was submitted."}

    # Get the starter code for the user's language
    starter_code = ""
    if coding_problem and coding_language:
        starter_code = (coding_problem.get("starter_code") or {}).get(coding_language, "")

    # If final code is identical to starter code (ignoring whitespace)
    if starter_code and final_code.strip() == starter_code.strip():
        return {"sufficient": False, "reason": "Only the starter code was submitted — no changes were made."}

    # Count new lines the user added (non-blank lines not in the starter)
    starter_lines = set(line.strip() for line in starter_code.splitlines() if line.strip())
    final_lines = [line for line in final_code.splitlines() if line.strip()]
    new_lines = [line for line in final_lines if line.strip() not in starter_lines]

    if len(new_lines) < 3:
        return {
            "sufficient": False,
            "reason": f"Only {len(new_lines)} new line(s) of code written. Write at least 3 lines beyond the starter code.",
        }

    return {"sufficient": True, "reason": ""}


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

        # --- Coding mode: check code submission instead of spoken answers ---
        if iv.interview_mode == "coding":
            code_check = _check_coding_submission(iv.final_code, iv.coding_problem, iv.coding_language)
            if not code_check["sufficient"]:
                logger.warning(f"Skipping evaluation for coding interview {interview_id}: {code_check['reason']}")
                iv.status = "incomplete"
                iv.incomplete_reason = code_check["reason"]
                await session.commit()
                return
        else:
            # Voice mode: need at least 3 substantive user responses to evaluate
            user_entries = [
                e for e in iv.transcript
                if e.get("role") == "user"
                and len(e.get("text", "").strip().split()) >= 5
            ]
            if len(user_entries) < 3:
                logger.warning(f"Skipping evaluation for {interview_id}: insufficient user responses ({len(user_entries)})")
                iv.status = "incomplete"
                await session.commit()
                return

        # Check if report already exists (avoid duplicate)
        existing_report = await session.execute(
            select(InterviewReport).where(InterviewReport.interview_id == interview_id)
        )
        if existing_report.scalar_one_or_none():
            logger.info(f"Report already exists for interview {interview_id}, skipping")
            return

        if iv.interview_mode == "coding":
            evaluation = await evaluate_coding_interview(
                transcript=iv.transcript,
                coding_problem=iv.coding_problem or {},
                final_code=iv.final_code,
                coding_language=iv.coding_language,
            )
        else:
            evaluation = await evaluate_interview(
                transcript=iv.transcript,
                interview_plan=iv.interview_plan or {},
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
            code_review=evaluation.get("code_review"),
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
    callback_status = body.get("status")
    error = body.get("error")

    async with async_session() as session:
        result = await session.execute(
            select(Interview).where(Interview.id == interview_id)
        )
        iv = result.scalar_one_or_none()
        if not iv:
            raise HTTPException(status_code=404, detail="Interview not found")

        iv.transcript = transcript
        iv.status = "failed" if callback_status == "failed" and not transcript else "completed"
        await session.commit()
        logger.info(f"Bot callback: interview {interview_id} completed with {len(transcript)} entries")

    # Run evaluation after a short delay so the frontend's final code-sync
    # can complete before we read final_code from the database.
    if transcript:
        async def _delayed_evaluation(iid: str):
            await asyncio.sleep(5)  # wait for frontend final code-sync to commit
            await _run_evaluation_safely(iid)
        asyncio.create_task(_delayed_evaluation(interview_id))
    elif error:
        logger.warning(f"Bot callback for {interview_id} had no transcript: {error}")

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

    # Check if interview was too short to evaluate
    if interview.status == "incomplete":
        if interview.interview_mode == "coding":
            reason = interview.incomplete_reason or "Not enough code was written."
            message = f"Unable to generate a report. {reason} Please write at least 3 lines of code beyond the starter template to receive feedback."
        else:
            message = "Interview was too short to generate a report. You need to answer at least a few questions for the AI to evaluate your performance."
        return {
            "status": "incomplete",
            "message": message,
        }

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
