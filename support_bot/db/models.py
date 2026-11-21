from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    gender: Mapped[str] = mapped_column(String(1))
    nickname: Mapped[str] = mapped_column(String(32), unique=True)
    greeting: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    shifts: Mapped[list[Shift]] = relationship(back_populates="employee", cascade="all, delete-orphan")

    __table_args__ = (CheckConstraint("gender IN ('m', 'f')"),)


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    start_minutes: Mapped[int] = mapped_column(Integer)
    end_minutes: Mapped[int] = mapped_column(Integer)
    employee: Mapped[Employee] = relationship(back_populates="shifts")

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6"),
        CheckConstraint("start_minutes BETWEEN 0 AND 1439"),
        CheckConstraint("end_minutes BETWEEN 0 AND 1439"),
        UniqueConstraint("employee_id", "weekday", "start_minutes", "end_minutes"),
    )


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    user_display_name: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(String(300))
    media_type: Mapped[str | None] = mapped_column(String(30))
    thread_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    request_message_id: Mapped[int | None] = mapped_column(BigInteger)
    request_media_message_id: Mapped[int | None] = mapped_column(BigInteger)
    initial_message_ids: Mapped[str] = mapped_column(Text, default="")
    topic_media_message_id: Mapped[int | None] = mapped_column(BigInteger)
    pinned_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(50), default="new", index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), index=True)
    previous_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    note: Mapped[str] = mapped_column(Text, default="")
    custom_title: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_user_message_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_support_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    employee: Mapped[Employee | None] = relationship(foreign_keys=[employee_id])
    previous_employee: Mapped[Employee | None] = relationship(foreign_keys=[previous_employee_id])
    reminder: Mapped[Reminder | None] = relationship(back_populates="ticket", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_tickets_active_user", "user_id", "status", "deleted_at"),)


class SystemTopic(Base):
    __tablename__ = "system_topics"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    thread_id: Mapped[int] = mapped_column(BigInteger)


class MessageLink(Base):
    __tablename__ = "message_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    target_chat_id: Mapped[int] = mapped_column(BigInteger)
    target_message_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(30), default="text")
    media_file_id: Mapped[str | None] = mapped_column(String(300))
    thumbnail_file_id: Mapped[str | None] = mapped_column(String(300))
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    from_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_chat_id", "source_message_id"),
        UniqueConstraint("target_chat_id", "target_message_id"),
    )


class TicketReadState(Base):
    __tablename__ = "ticket_read_states"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True)
    last_read_message_id: Mapped[int] = mapped_column(Integer, default=0)


class QuickReply(Base):
    __tablename__ = "quick_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list[QuickReplyItem]] = relationship(
        back_populates="quick_reply",
        cascade="all, delete-orphan",
        order_by="QuickReplyItem.position, QuickReplyItem.id",
    )

    __table_args__ = (Index("ix_quick_replies_position", "position", "id"),)


class QuickReplyItem(Base):
    __tablename__ = "quick_reply_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    quick_reply_id: Mapped[int] = mapped_column(ForeignKey("quick_replies.id", ondelete="CASCADE"), index=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(30), default="text")
    media_file_id: Mapped[str | None] = mapped_column(String(300))
    thumbnail_file_id: Mapped[str | None] = mapped_column(String(300))
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer, default=0)
    quick_reply: Mapped[QuickReply] = relationship(back_populates="items")
    copies: Mapped[list[QuickReplyCopy]] = relationship(back_populates="item", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source_chat_id", "source_message_id"),
        Index("ix_quick_reply_items_order", "quick_reply_id", "position", "id"),
    )


class QuickReplyCopy(Base):
    __tablename__ = "quick_reply_copies"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("quick_reply_items.id", ondelete="CASCADE"), index=True)
    message_link_id: Mapped[int] = mapped_column(
        ForeignKey("message_links.id", ondelete="CASCADE"), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    item: Mapped[QuickReplyItem] = relationship(back_populates="copies")
    message_link: Mapped[MessageLink] = relationship()

    __table_args__ = (UniqueConstraint("item_id", "message_link_id"),)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    ticket_number: Mapped[int] = mapped_column(Integer, index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    employee: Mapped[Employee | None] = relationship()

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5"),
        UniqueConstraint("ticket_id"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Counter(Base):
    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Reminder(Base):
    __tablename__ = "reminders"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    next_run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ticket: Mapped[Ticket] = relationship(back_populates="reminder")


class FsmRecord(Base):
    __tablename__ = "fsm_records"

    key: Mapped[str] = mapped_column(String(400), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
