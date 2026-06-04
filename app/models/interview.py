import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, ForeignKey, JSON, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    resume_text: Mapped[str] = mapped_column(Text, nullable=False)
    job_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    role_type: Mapped[str | None] = mapped_column(String, nullable=True)
    level: Mapped[str | None] = mapped_column(String, nullable=True)  # "entry" or "experienced"
    years_of_experience: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)  # "internship" or "full_time"
    interview_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    transcript: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="setup")  # setup, planned, in_progress, completed
    daily_room_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="interviews")
    questions: Mapped[list["InterviewQuestion"]] = relationship(back_populates="interview")
    scores: Mapped[list["InterviewScore"]] = relationship(back_populates="interview")
    report: Mapped["InterviewReport | None"] = relationship(back_populates="interview", uselist=False)


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interview_id: Mapped[str] = mapped_column(String, ForeignKey("interviews.id"), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    interview: Mapped["Interview"] = relationship(back_populates="questions")
