from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.routers.auth import get_current_user
from app.schemas.interview import (
    ResumeUploadResponse,
    JobScrapeRequest,
    JobScrapeResponse,
    CompanyResearchRequest,
    CompanyResearchResponse,
)
from app.services.resume_parser import save_upload, parse_resume
from app.services.resume_validator import validate_resume_text
from app.services.job_scraper import scrape_job_posting
from app.services.company_researcher import research_company

router = APIRouter(prefix="/api", tags=["resume"])


@router.post("/resume/upload", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    file_path = save_upload(settings.upload_dir, current_user.id, file_bytes, file.filename)

    try:
        result = parse_resume(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse PDF: {str(e)}")

    # Strip null bytes — some PDFs contain \x00 which PostgreSQL can't store
    result["raw_text"] = result["raw_text"].replace("\x00", "")

    if not result["raw_text"].strip():
        raise HTTPException(status_code=400, detail="Could not extract text from PDF. Make sure your resume is not a scanned image.")

    # Validate that the PDF is actually a resume
    validation = await validate_resume_text(result["raw_text"])
    if not validation["is_resume"]:
        reason = validation.get("reason", "")
        detail = "This doesn't look like a resume. Please upload a document with your work experience, skills, and education."
        if reason:
            detail = f"{detail} ({reason})"
        raise HTTPException(status_code=400, detail=detail)

    # Save resume to user profile so it persists across sessions
    current_user.resume_text = result["raw_text"]
    current_user.resume_file_name = file.filename
    db.add(current_user)
    await db.commit()

    return ResumeUploadResponse(resume_text=result["raw_text"], page_count=result["pages"])


@router.delete("/resume")
async def delete_resume(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current_user.resume_text = None
    current_user.resume_file_name = None
    db.add(current_user)
    await db.commit()
    return {"ok": True}


@router.post("/job/scrape", response_model=JobScrapeResponse)
async def scrape_job(
    data: JobScrapeRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        result = await scrape_job_posting(data.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to scrape job posting: {str(e)}")

    return JobScrapeResponse(
        title=result["title"],
        company=result["company"],
        description=result["description"],
    )


@router.post("/company/research", response_model=CompanyResearchResponse)
async def research_company_endpoint(
    data: CompanyResearchRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        result = await research_company(data.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Company research failed: {str(e)}")

    return CompanyResearchResponse(**result)
