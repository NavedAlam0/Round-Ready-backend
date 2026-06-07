from datetime import datetime

from sqlalchemy import String, DateTime, Integer, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    razorpay_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    razorpay_signature: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # amount in paise/cents
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    payment_type: Mapped[str] = mapped_column(String, nullable=False)  # subscription, topup
    credits_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")  # pending, success, failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="payments")
