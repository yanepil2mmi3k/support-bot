from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from support_bot.db.models import (
    Counter,
    Employee,
    MessageLink,
    Reminder,
    Review,
    Ticket,
    utcnow,
)


MEDIA_ATTRIBUTES = ("photo", "video", "document", "animation", "audio", "voice", "video_note", "sticker")


def extract_message_media(message) -> dict[str, object | None]:
    for attribute in MEDIA_ATTRIBUTES:
        value = getattr(message, attribute, None)
        if not value:
            continue
        media = value[-1] if attribute == "photo" else value
        thumbnail = getattr(media, "thumbnail", None)
        return {
            "media_file_id": getattr(media, "file_id", None),
            "thumbnail_file_id": getattr(thumbnail, "file_id", None),
            "file_name": getattr(media, "file_name", None),
            "mime_type": getattr(media, "mime_type", None),
            "file_size": getattr(media, "file_size", None),
        }
    return {}


def message_body(message, media: dict[str, object | None]) -> str | None:
    if message.text is not None:
        return message.text
    if message.caption is not None:
        return message.caption
    return "" if media else None


class TicketError(ValueError):
    pass


class ActiveTicketExists(TicketError):
    pass


class PermissionDenied(TicketError):
    pass


class TicketService:
    def __init__(self, session: AsyncSession, admin_id: int) -> None:
        self.session = session
        self.admin_id = admin_id

    async def _begin_write(self) -> None:
        await self.session.commit()
        await self.session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _query():
        return select(Ticket).options(
            selectinload(Ticket.employee),
            selectinload(Ticket.previous_employee),
            selectinload(Ticket.reminder),
        )

    async def next_number(self) -> int:
        number = await self.session.scalar(
            update(Counter).where(Counter.name == "ticket").values(value=Counter.value + 1).returning(Counter.value)
        )
        if number is None:
            self.session.add(Counter(name="ticket", value=1))
            await self.session.flush()
            return 1
        return int(number)

    async def create(
        self,
        *,
        user_id: int,
        username: str | None,
        display_name: str,
        category: str,
        description: str,
        media_file_id: str | None = None,
        media_type: str | None = None,
        closed_statuses: set[str] | None = None,
    ) -> Ticket:
        await self._begin_write()
        active = await self.active_for_user(user_id, closed_statuses)
        if active:
            raise ActiveTicketExists(f"У вас уже есть открытое обращение №{active.number}")
        number = await self.next_number()
        ticket = Ticket(
            number=number,
            user_id=user_id,
            username=username,
            user_display_name=display_name,
            category=category,
            description=description,
            media_file_id=media_file_id,
            media_type=media_type,
            status="new",
        )
        self.session.add(ticket)
        await self.session.commit()
        return await self.get_by_id(ticket.id)

    async def get_by_id(self, ticket_id: int) -> Ticket | None:
        return await self.session.scalar(self._query().where(Ticket.id == ticket_id))

    async def get(self, number: int) -> Ticket | None:
        return await self.session.scalar(self._query().where(Ticket.number == number))

    async def by_thread(self, thread_id: int) -> Ticket | None:
        return await self.session.scalar(self._query().where(Ticket.thread_id == thread_id))

    async def active_for_user(
        self, user_id: int, closed_statuses: set[str] | None = None
    ) -> Ticket | None:
        closed = closed_statuses if closed_statuses is not None else {"archive", "resolved"}
        return await self.session.scalar(
            self._query()
            .where(
                Ticket.user_id == user_id,
                Ticket.deleted_at.is_(None),
                Ticket.status.not_in(closed),
            )
            .order_by(Ticket.number.desc())
        )

    async def latest_for_user(self, user_id: int) -> Ticket | None:
        return await self.session.scalar(
            self._query()
            .where(Ticket.user_id == user_id, Ticket.deleted_at.is_(None))
            .order_by(Ticket.number.desc())
        )

    async def list(self, statuses: list[str] | None = None, include_deleted: bool = False) -> list[Ticket]:
        query = self._query().order_by(Ticket.number)
        if statuses is not None:
            query = query.where(Ticket.status.in_(statuses))
        if not include_deleted:
            query = query.where(Ticket.deleted_at.is_(None))
        return list((await self.session.execute(query)).scalars())

    def check_access(self, ticket: Ticket, employee: Employee | None) -> None:
        if employee is None or not employee.active:
            raise PermissionDenied("Активный профиль сотрудника не найден")
        if employee.telegram_id != self.admin_id and ticket.employee_id != employee.id:
            raise PermissionDenied("Можно управлять только своими тикетами")

    async def assign(self, ticket: Ticket, employee: Employee, *, transfer: bool = False) -> None:
        if employee.id is not None:
            employee = await self.session.get(Employee, employee.id)
        if transfer and ticket.employee_id and ticket.employee_id != employee.id:
            ticket.previous_employee_id = ticket.employee_id
        ticket.employee_id = employee.id
        ticket.employee = employee
        if not transfer:
            ticket.status = "work"
        ticket.updated_at = utcnow()
        await self.session.commit()

    async def claim(self, ticket: Ticket, employee: Employee) -> bool:
        claimed_id = await self.session.scalar(
            update(Ticket)
            .where(Ticket.id == ticket.id, Ticket.employee_id.is_(None), Ticket.status == "new")
            .values(employee_id=employee.id, status="work", updated_at=utcnow())
            .returning(Ticket.id)
        )
        if claimed_id is None:
            await self.session.rollback()
            return False
        await self.session.commit()
        ticket.employee_id = employee.id
        ticket.employee = employee
        ticket.status = "work"
        return True

    async def release_uncreated(self, ticket: Ticket) -> None:
        if ticket.thread_id is None:
            ticket.employee_id = None
            ticket.employee = None
            ticket.status = "new"
            await self.session.commit()

    async def cancel_creation(self, ticket: Ticket) -> bool:
        if (
            ticket.request_message_id is not None
            or ticket.request_media_message_id is not None
            or ticket.thread_id is not None
        ):
            return False
        now = utcnow()
        ticket.status = "archive"
        ticket.deleted_at = now
        ticket.archived_at = now
        ticket.employee_id = None
        ticket.employee = None
        ticket.previous_employee_id = None
        ticket.previous_employee = None
        reminder = await self.session.get(Reminder, ticket.id)
        if reminder:
            reminder.active = False
        await self.session.commit()
        return True

    async def set_status(self, ticket: Ticket, status: str, status_data: dict) -> bool:
        closed = bool(status_data.get("closed") or status_data.get("archive"))
        if ticket.status == status:
            if closed:
                reminder = await self.session.get(Reminder, ticket.id)
                if reminder:
                    reminder.active = False
                    await self.session.commit()
            return False
        now = utcnow()
        ticket.status = status
        ticket.updated_at = now
        if closed:
            ticket.resolved_at = ticket.resolved_at or now
            ticket.archived_at = now if status_data.get("archive") else None
            reminder = await self.session.get(Reminder, ticket.id)
            if reminder:
                reminder.active = False
        else:
            ticket.resolved_at = None
            ticket.archived_at = None
        await self.session.commit()
        return True

    async def close(self, ticket: Ticket, status_data: dict | None = None) -> bool:
        data = status_data or {"key": "archive", "closed": True, "archive": True}
        return await self.set_status(ticket, str(data["key"]), data)

    async def reopen(self, ticket: Ticket, closed_statuses: set[str] | None = None) -> bool:
        closed = closed_statuses if closed_statuses is not None else {"archive", "resolved"}
        if ticket.status not in closed:
            return False
        await self._begin_write()
        if ticket.deleted_at:
            raise TicketError("Архивный топик был удалён, создайте новое обращение")
        if ticket.user_id:
            active = await self.active_for_user(ticket.user_id, closed)
            if active and active.id != ticket.id:
                raise TicketError(f"Сначала закройте активное обращение №{active.number}")
        ticket.status = "work"
        ticket.resolved_at = None
        ticket.archived_at = None
        ticket.last_user_message_at = utcnow()
        ticket.updated_at = utcnow()
        await self.session.commit()
        return True

    async def reset_counter(self, target: int) -> None:
        if target < 0:
            raise TicketError("Счётчик не может быть отрицательным")
        await self._begin_write()
        maximum = await self.session.scalar(select(func.max(Ticket.number)))
        maximum = int(maximum or 0)
        if target < maximum:
            raise TicketError(f"Нельзя установить {target}: номера до {maximum} уже использовались")
        counter = await self.session.get(Counter, "ticket")
        if counter is None:
            counter = Counter(name="ticket", value=target)
            self.session.add(counter)
        else:
            counter.value = target
        await self.session.commit()

    async def add_review(self, ticket: Ticket, rating: int, comment: str | None = None) -> Review:
        if rating not in range(1, 6):
            raise TicketError("Оценка должна быть от 1 до 5")
        review = await self.session.scalar(select(Review).where(Review.ticket_id == ticket.id))
        if review is None:
            review = Review(
                ticket_id=ticket.id,
                ticket_number=ticket.number,
                employee_id=ticket.employee_id,
                user_id=ticket.user_id,
                rating=rating,
                comment=comment,
            )
            self.session.add(review)
        else:
            review.rating = rating
            review.comment = comment
            review.employee_id = ticket.employee_id
        await self.session.commit()
        return review

    async def set_reminder(self, ticket: Ticket, employee: Employee, after_minutes: int) -> Reminder:
        reminder = await self.session.get(Reminder, ticket.id)
        if reminder is None:
            reminder = Reminder(ticket_id=ticket.id)
            self.session.add(reminder)
        ticket.reminder = reminder
        reminder.employee_id = employee.id
        reminder.next_run_at = utcnow() + timedelta(minutes=after_minutes)
        reminder.active = True
        await self.session.commit()
        return reminder

    async def disable_reminder(self, ticket: Ticket) -> None:
        reminder = await self.session.get(Reminder, ticket.id)
        if reminder:
            reminder.active = False
            await self.session.commit()

    async def save_message_link(
        self,
        ticket: Ticket,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: int,
        target_message_id: int,
        *,
        body: str | None = None,
        content_type: str = "text",
        from_staff: bool = False,
        media_file_id: str | None = None,
        thumbnail_file_id: str | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        file_size: int | None = None,
    ) -> MessageLink:
        link = MessageLink(
            ticket_id=ticket.id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
            body=body,
            content_type=content_type,
            from_staff=from_staff,
            media_file_id=media_file_id,
            thumbnail_file_id=thumbnail_file_id,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
        )
        self.session.add(link)
        await self.session.commit()
        return link

    @staticmethod
    def update_message_metadata(link: MessageLink, message) -> None:
        media = extract_message_media(message)
        link.body = message_body(message, media)
        link.content_type = str(message.content_type).split(".")[-1].lower()
        for name, value in media.items():
            setattr(link, name, value)

    async def linked_message(self, ticket: Ticket, chat_id: int, message_id: int) -> MessageLink | None:
        return await self.session.scalar(
            select(MessageLink).where(
                MessageLink.ticket_id == ticket.id,
                or_(
                    (MessageLink.source_chat_id == chat_id) & (MessageLink.source_message_id == message_id),
                    (MessageLink.target_chat_id == chat_id) & (MessageLink.target_message_id == message_id),
                ),
            )
        )

    async def delete(self, ticket: Ticket) -> None:
        now = utcnow()
        ticket.status = "archive"
        ticket.deleted_at = now
        ticket.archived_at = ticket.archived_at or now
        ticket.user_id = None
        ticket.username = None
        ticket.user_display_name = None
        ticket.category = ""
        ticket.description = ""
        ticket.note = ""
        ticket.custom_title = None
        ticket.media_file_id = None
        ticket.media_type = None
        ticket.thread_id = None
        ticket.request_message_id = None
        ticket.request_media_message_id = None
        ticket.initial_message_ids = ""
        ticket.topic_media_message_id = None
        ticket.pinned_message_id = None
        await self.session.execute(delete(MessageLink).where(MessageLink.ticket_id == ticket.id))
        await self.session.execute(delete(Reminder).where(Reminder.ticket_id == ticket.id))
        reviews = (await self.session.execute(select(Review).where(Review.ticket_id == ticket.id))).scalars()
        for review in reviews:
            review.user_id = None
            review.comment = None
        await self.session.commit()

    async def mark_user_message(self, ticket: Ticket) -> None:
        now = utcnow()
        ticket.last_user_message_at = now
        ticket.updated_at = now
        await self.session.commit()

    async def mark_support_message(self, ticket: Ticket) -> None:
        now = utcnow()
        ticket.last_support_message_at = now
        ticket.updated_at = now
        await self.session.commit()
