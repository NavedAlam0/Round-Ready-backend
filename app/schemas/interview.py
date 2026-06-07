from pydantic import BaseModel
from typing import Optional


class ResumeUploadResponse(BaseModel):
    resume_text: str
    page_count: int


class JobScrapeRequest(BaseModel):
    url: str


class JobScrapeResponse(BaseModel):
    title: str
    company: str
    description: str


class CompanyResearchRequest(BaseModel):
    url: str


class CompanyResearchResponse(BaseModel):
    name: str
    description: str
    industry: str
    tech_stack: list[str]
    products: list[str]
    culture_notes: str


class InterviewSetupRequest(BaseModel):
    resume_text: str
    job_description: Optional[str] = None
    company_info: Optional[dict] = None
    role_type: Optional[str] = None
    level: Optional[str] = None  # "entry" or "experienced"
    years_of_experience: Optional[int] = None
    employment_type: Optional[str] = None  # "internship" or "full_time"
    interview_mode: str = "voice"  # "voice" or "coding"
    coding_language: Optional[str] = None  # "python", "javascript", "java", "cpp", "go"
    coding_difficulty: Optional[str] = None  # "easy", "medium", "hard"
    coding_topic: Optional[str] = None  # e.g. "arrays", "trees", "dynamic-programming"


class CodeSyncRequest(BaseModel):
    code: str
    language: Optional[str] = None


class InterviewSetupResponse(BaseModel):
    interview_id: str
    status: str

    model_config = {"from_attributes": True}
