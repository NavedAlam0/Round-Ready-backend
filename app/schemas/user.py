from typing import Optional
from pydantic import BaseModel


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    auth_provider: str

    model_config = {"from_attributes": True}


class UserPreferencesResponse(BaseModel):
    resume_text: Optional[str] = None
    resume_file_name: Optional[str] = None
    role_type: Optional[str] = None
    level: Optional[str] = None
    years_of_experience: Optional[int] = None
    employment_type: Optional[str] = None


class UserPreferencesUpdate(BaseModel):
    role_type: Optional[str] = None
    level: Optional[str] = None
    years_of_experience: Optional[int] = None
    employment_type: Optional[str] = None
