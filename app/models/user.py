import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, SmallInteger, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Firebase UID
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    auth_provider: Mapped[str] = mapped_column(String, nullable=False, default="email")  # "google.com" or "password"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Saved preferences (persist across interview sessions)
    resume_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    role_type: Mapped[str | None] = mapped_column(String, nullable=True)
    level: Mapped[str | None] = mapped_column(String, nullable=True)
    years_of_experience: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)

    # Plan & credits
    plan: Mapped[str] = mapped_column(String, nullable=False, default="free")  # free, starter, pro
    credits_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=2)  # free users get 2
    is_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    interviews: Mapped[list["Interview"]] = relationship(back_populates="user")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user")
