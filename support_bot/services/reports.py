from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from support_bot.db.models import Employee, Review, Ticket
from support_bot.services.topics import esc

TELEGRAM_MESSAGE_LIMIT = 4096
_HTML_TOKEN_RE = re.compile(r"<[^>]+>|&(?:#\d+|#x[\da-fA-F]+|[a-zA-Z][\w]+);|.", re.DOTALL)
_OPEN_TAG_RE = re.compile(r"<([a-zA-Z][\w-]*)(?:\s[^>]*)?>")
_CLOSE_TAG_RE = re.compile(r"</([a-zA-Z][\w-]*)>")


def split_html(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if limit <= 0:
        raise ValueError("Лимит сообщения должен быть положительным")

    chunks: list[str] = []
    current = ""
    open_tags: list[str] = []

    def closing_tags(tags: list[str]) -> str:
        return "".join(f"</{_OPEN_TAG_RE.match(tag).group(1)}>" for tag in reversed(tags))

    def flush() -> None:
        nonlocal current
        chunks.append(current + closing_tags(open_tags))
        current = "".join(open_tags)

    for token in _HTML_TOKEN_RE.findall(text):
        opening = _OPEN_TAG_RE.fullmatch(token)
        closing = _CLOSE_TAG_RE.fullmatch(token)
        if opening:
            new_tags = [*open_tags, token]
            if len(current) + len(token) + len(closing_tags(new_tags)) > limit:
                flush()
            if len(current) + len(token) + len(closing_tags(new_tags)) > limit:
                raise ValueError("HTML-тег превышает лимит сообщения")
            current += token
            open_tags = new_tags
        elif closing and open_tags and _OPEN_TAG_RE.match(open_tags[-1]).group(1) == closing.group(1):
            remaining_tags = open_tags[:-1]
            if len(current) + len(token) + len(closing_tags(remaining_tags)) > limit:
                flush()
            current += token
            open_tags = remaining_tags
        else:
            if len(current) + len(token) + len(closing_tags(open_tags)) > limit:
                flush()
            if len(current) + len(token) + len(closing_tags(open_tags)) > limit:
                raise ValueError("HTML-токен превышает лимит сообщения")
            current += token

    if current or not chunks:
        chunks.append(current + closing_tags(open_tags))
    return chunks


async def build_report(session: AsyncSession, closed_statuses: set[str] | None = None) -> str:
    closed = closed_statuses if closed_statuses is not None else {"archive", "resolved"}
    employees = list((await session.execute(select(Employee).order_by(Employee.id))).scalars())
    lines = ["<b>Отчёт службы поддержки</b>"]
    for employee in employees:
        open_count = await session.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.employee_id == employee.id,
                Ticket.deleted_at.is_(None),
                Ticket.status.not_in(closed),
            )
        )
        closed_count = await session.scalar(
            select(func.count(Ticket.id)).where(Ticket.employee_id == employee.id, Ticket.resolved_at.is_not(None))
        )
        resolved = (
            await session.execute(
                select(Ticket.created_at, Ticket.resolved_at).where(
                    Ticket.employee_id == employee.id, Ticket.resolved_at.is_not(None)
                )
            )
        ).all()
        average_hours = (
            sum((resolved_at - created_at).total_seconds() for created_at, resolved_at in resolved) / len(resolved) / 3600
            if resolved
            else None
        )
        review_count, average_rating = (
            await session.execute(
                select(func.count(Review.id), func.avg(Review.rating)).where(Review.employee_id == employee.id)
            )
        ).one()
        lines.extend(
            [
                "",
                f"<b>{esc(employee.name)}</b> ({esc(employee.nickname)})",
                f"Открыто: {open_count or 0}; закрыто: {closed_count or 0}",
                f"Среднее время решения: {average_hours:.2f} ч" if average_hours is not None else "Среднее время решения: нет данных",
                f"Средняя оценка: {average_rating:.2f} ({review_count})" if review_count else "Средняя оценка: нет данных",
            ]
        )
    recent_reviews = (
        await session.execute(
            select(Review).options(selectinload(Review.employee)).order_by(Review.created_at.desc()).limit(10)
        )
    ).scalars()
    lines.append("\n<b>Последние отзывы</b>")
    if not recent_reviews:
        lines.append("Отзывов пока нет")
    for review in recent_reviews:
        lines.append(
            f"#{review.ticket_number}: {review.rating}/5, {esc(review.employee.name if review.employee else 'сотрудник не указан')}"
            + (f"\n{esc(review.comment)}" if review.comment else "")
        )
    return "\n".join(lines)
