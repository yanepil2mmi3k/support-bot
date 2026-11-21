from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote

import yaml
from aiogram import Bot
from aiogram.types import FSInputFile
from aiohttp import web
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from support_bot.config import Config
from support_bot.db.models import Employee, MessageLink, QuickReply, QuickReplyItem, Review, Ticket, TicketReadState
from support_bot.db.session import Database
from support_bot.services.employees import EmployeeService, parse_schedule
from support_bot.services.quick_replies import QuickReplyService, QuickReplyUpload
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import PermissionDenied, TicketService, extract_message_media
from support_bot.services.topics import TopicService

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

MEDIA_TYPES = frozenset({"photo", "video", "document", "animation", "audio", "voice"})
PHOTO_MAX_SIZE = 10 * 1024 * 1024
MEDIA_MAX_SIZE = 50 * 1024 * 1024
MIME_TYPES = {
    "photo": "image/jpeg",
    "video": "video/mp4",
    "document": "application/octet-stream",
    "animation": "video/mp4",
    "audio": "audio/mpeg",
    "voice": "audio/ogg",
    "video_note": "video/mp4",
    "sticker": "image/webp",
}


def _media_type(value: object) -> str:
    return str(value or "").split(".")[-1].lower()


def _safe_mime_type(kind: str, value: str | None, *, thumbnail: bool = False) -> str:
    if thumbnail:
        return "image/jpeg"
    candidate = str(value or "").split(";", 1)[0].strip().lower()
    if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", candidate):
        return candidate
    return MIME_TYPES.get(kind, "application/octet-stream")


def _safe_file_name(value: str | None) -> str:
    name = str(value or "document").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character >= " " and character != "\x7f").strip()
    return name[:255] or "document"


def _attachment(file_name: str | None) -> str:
    name = _safe_file_name(file_name)
    fallback = name.encode("ascii", "ignore").decode().replace('"', "_").replace("\\", "_")
    if not fallback.strip(" ."):
        fallback = "document"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _json(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat()
    return value


def _ticket(ticket: Ticket, *, unread_count: int = 0) -> dict:
    reminder = ticket.reminder
    media_type = _media_type(ticket.media_type)
    return {
        "id": ticket.id,
        "number": ticket.number,
        "user_id": ticket.user_id,
        "username": ticket.username,
        "client": ticket.user_display_name or "Удалённый клиент",
        "category": ticket.category,
        "description": ticket.description,
        "media_url": f"/api/tickets/{ticket.number}/media" if ticket.media_file_id and media_type in MIME_TYPES else None,
        "media_type": media_type or None,
        "note": ticket.note,
        "title": ticket.custom_title,
        "status": ticket.status,
        "thread_id": ticket.thread_id,
        "employee_id": ticket.employee_id,
        "employee": ticket.employee.nickname if ticket.employee else None,
        "reminder": {
            "active": bool(reminder and reminder.active),
            "next_run_at": _json(reminder.next_run_at) if reminder else None,
        },
        "created_at": _json(ticket.created_at),
        "updated_at": _json(ticket.updated_at),
        "last_user_message_at": _json(ticket.last_user_message_at),
        "last_support_message_at": _json(ticket.last_support_message_at),
        "unread_count": unread_count,
    }


def _employee(employee: Employee) -> dict:
    return {
        "id": employee.id,
        "telegram_id": employee.telegram_id,
        "name": employee.name,
        "nickname": employee.nickname,
        "gender": employee.gender,
        "greeting": employee.greeting or "",
        "active": bool(employee.active),
        "schedule": EmployeeService.format_schedule(employee),
    }


def _require_admin(request: web.Request) -> None:
    if request["telegram_id"] != request.app["config"].admin_id:
        raise PermissionDenied("Управлять сотрудниками может только администратор")


async def _profile(session, config: Config, employee: Employee) -> dict:
    visible = employee.telegram_id == config.admin_id or await SettingsService(session, config.defaults).get(
        "employee_ratings_visible", False
    )
    reviews = []
    if visible:
        rows = await session.execute(
            select(Review)
            .where(Review.employee_id == employee.id)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(20)
        )
        reviews = [
            {
                "ticket_number": review.ticket_number,
                "rating": review.rating,
                "comment": review.comment,
                "created_at": _json(review.created_at),
            }
            for review in rows.scalars()
        ]
    return {
        "name": employee.name,
        "nickname": employee.nickname,
        "gender": employee.gender,
        "schedule": EmployeeService.format_schedule(employee),
        "greeting": employee.greeting or config.message("default_greeting", name=employee.name),
        "ratings_visible": bool(visible),
        "reviews": reviews,
    }


def _message(link: MessageLink) -> dict:
    labels = {
        "photo": "Фотография",
        "video": "Видео",
        "document": "Документ",
        "animation": "GIF-анимация",
        "audio": "Аудио",
        "voice": "Голосовое сообщение",
        "video_note": "Видеосообщение",
        "sticker": "Стикер",
    }
    kind = _media_type(link.content_type or "text")
    media_url = f"/api/messages/{link.id}/media" if link.media_file_id else None
    return {
        "id": link.id,
        "body": link.body if link.body is not None else "Сообщение создано до появления истории в Mini App",
        "content_type": kind,
        "media_label": labels.get(kind),
        "media_url": media_url,
        "thumbnail_url": f"{media_url}?thumbnail=1" if media_url and link.thumbnail_file_id else None,
        "file_name": link.file_name,
        "mime_type": link.mime_type,
        "file_size": link.file_size,
        "from_staff": bool(link.from_staff),
        "created_at": _json(link.created_at),
        "legacy": link.body is None,
    }


def _telegram_user(init_data: str, token: str) -> int | None:
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash", "")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        actual_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not received_hash or not hmac.compare_digest(actual_hash, received_hash):
            return None
        if time.time() - int(values.get("auth_date", "0")) > 86400:
            return None
        return int(json.loads(values["user"])["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


@web.middleware
async def security_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        dev_mode = os.getenv("MINIAPP_DEV_MODE", "1") == "1"
        if init_data:
            telegram_id = _telegram_user(init_data, request.app["config"].bot_token)
        elif dev_mode:
            try:
                telegram_id = int(request.headers.get("X-Dev-User-ID", request.app["config"].admin_id))
            except ValueError:
                telegram_id = None
        else:
            telegram_id = None
        if telegram_id is None:
            response = web.json_response({"detail": "Не удалось подтвердить пользователя Telegram"}, status=401)
        else:
            async with request.app["db"].sessions() as session:
                employee = await EmployeeService(session, request.app["config"].admin_id).get(telegram_id)
                if not employee or not employee.active:
                    response = web.json_response({"detail": "Активный профиль сотрудника не найден"}, status=403)
                else:
                    request["employee_id"] = employee.id
                    request["telegram_id"] = telegram_id
                    try:
                        response = await handler(request)
                    except web.HTTPException:
                        raise
                    except (PermissionDenied, ValueError) as exc:
                        response = web.json_response({"detail": str(exc)}, status=400)
                    except Exception:
                        logger.exception("Ошибка Mini App API")
                        response = web.json_response({"detail": "Действие не выполнено"}, status=500)
    response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "http://localhost:7100")
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, X-Dev-User-ID"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    response.headers["Vary"] = "Origin"
    return response


async def _context(request: web.Request):
    session = request.app["db"].sessions()
    config: Config = request.app["config"]
    tickets = TicketService(session, config.admin_id)
    employee = await EmployeeService(session, config.admin_id).get_by_id(request["employee_id"])
    return session, tickets, SettingsService(session, config.defaults), employee


def _check(ticket: Ticket, employee: Employee, admin_id: int) -> None:
    if employee.telegram_id != admin_id and ticket.employee_id != employee.id:
        raise PermissionDenied("Можно управлять только своими обращениями")


def _check_read(ticket: Ticket, employee: Employee, admin_id: int) -> None:
    if employee.telegram_id != admin_id and ticket.employee_id not in {None, employee.id}:
        raise PermissionDenied("Можно управлять только своими обращениями")


async def _mark_ticket_read(session, ticket: Ticket, employee: Employee, through_message_id: int = 0) -> None:
    state = await session.get(TicketReadState, (ticket.id, employee.id))
    if state is None:
        state = TicketReadState(ticket_id=ticket.id, employee_id=employee.id)
        session.add(state)
    state.last_read_message_id = max(state.last_read_message_id or 0, through_message_id)
    await session.commit()


async def _unread_counts(session, employee_id: int, tickets: list[Ticket]) -> dict[int, int]:
    if not tickets:
        return {}
    ticket_ids = [ticket.id for ticket in tickets]
    states = (await session.execute(select(TicketReadState).where(
        TicketReadState.employee_id == employee_id,
        TicketReadState.ticket_id.in_(ticket_ids),
    ))).scalars()
    read_states = {state.ticket_id: state.last_read_message_id for state in states}
    cursor = select(TicketReadState.last_read_message_id).where(
        TicketReadState.ticket_id == MessageLink.ticket_id,
        TicketReadState.employee_id == employee_id,
    ).scalar_subquery()
    rows = await session.execute(
        select(MessageLink.ticket_id, func.count(MessageLink.id))
        .where(
            MessageLink.ticket_id.in_(ticket_ids),
            MessageLink.from_staff.is_(False),
            MessageLink.id > func.coalesce(cursor, 0),
        )
        .group_by(MessageLink.ticket_id)
    )
    message_counts = dict(rows.all())
    return {
        ticket.id: int(message_counts.get(ticket.id, 0)) + int(ticket.id not in read_states)
        for ticket in tickets
    }


def _not_found(detail: str) -> web.HTTPNotFound:
    return web.HTTPNotFound(text=json.dumps({"detail": detail}), content_type="application/json")


QUICK_REPLY_MAX_FILES = 10


def _quick_reply_item(item: QuickReplyItem) -> dict:
    media_url = f"/api/quick-reply-items/{item.id}/media" if item.media_file_id else None
    return {
        "id": item.id,
        "position": item.position,
        "text": item.text,
        "content_type": item.content_type,
        "source_chat_id": item.source_chat_id,
        "source_message_id": item.source_message_id,
        "media_url": media_url,
        "thumbnail_url": f"{media_url}?thumbnail=1" if media_url and item.thumbnail_file_id else None,
        "file_name": item.file_name,
        "mime_type": item.mime_type,
        "file_size": item.file_size,
    }


def _quick_reply(reply: QuickReply) -> dict:
    items = sorted(reply.items, key=lambda item: (item.position, item.id))
    return {
        "id": reply.id,
        "title": reply.title,
        "position": reply.position,
        "text": items[0].text if items else "",
        "items": [_quick_reply_item(item) for item in items],
    }


async def _quick_reply_form(
    request: web.Request, *, metadata: bool
) -> tuple[str | None, str, list[QuickReplyUpload], list[Path]]:
    fields: dict[str, str] = {}
    numbered: dict[int, dict[str, object]] = {}
    paths: list[Path] = []
    seen: set[str] = set()
    try:
        if not request.content_type.startswith("multipart/"):
            if not metadata:
                raise ValueError("Передайте файлы как multipart/form-data")
            data = await request.post()
            if set(data) - {"title", "text"} or "title" not in data:
                raise ValueError("Передайте title и необязательный text")
            if any(len(data.getall(name)) != 1 for name in data.keys()):
                raise ValueError("Поля title и text должны быть переданы один раз")
            return str(data["title"]), str(data.get("text", "")), [], paths
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            name = part.name or ""
            if name in seen:
                raise ValueError(f"Поле {name} передано несколько раз")
            seen.add(name)
            if metadata and name in {"title", "text"}:
                fields[name] = await part.text()
                continue
            match = re.fullmatch(r"(media_type|file)_([0-9]+)", name)
            if match is None:
                expected = "title, text и пары media_type_N/file_N" if metadata else "пары media_type_N/file_N"
                raise ValueError(f"Передайте {expected}")
            index = int(match.group(2))
            if index > QUICK_REPLY_MAX_FILES:
                raise ValueError(f"Можно передать не более {QUICK_REPLY_MAX_FILES} файлов")
            kind = match.group(1)
            values = numbered.setdefault(index, {})
            if kind == "media_type":
                media_type = (await part.text()).strip().lower()
                if media_type not in MEDIA_TYPES:
                    raise ValueError("Неподдерживаемый тип медиа")
                values[kind] = media_type
                continue
            file_name = _safe_file_name(part.filename)
            mime_type = part.headers.get("Content-Type")
            upload_dir = request.app["config"].database_path.parent / ".uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.NamedTemporaryFile(dir=upload_dir, prefix="quick-reply-", delete=False)
            path = Path(temporary.name)
            paths.append(path)
            file_size = 0
            try:
                while chunk := await part.read_chunk(65536):
                    file_size += len(chunk)
                    if file_size > MEDIA_MAX_SIZE:
                        raise ValueError("Размер файла превышает 50 MiB")
                    temporary.write(chunk)
            finally:
                temporary.close()
            values.update(path=path, file_name=file_name, mime_type=mime_type, file_size=file_size)
        if metadata and "title" not in fields:
            raise ValueError("Укажите название быстрого ответа")
        uploads = []
        for index in sorted(numbered):
            values = numbered[index]
            if "path" not in values or "media_type" not in values:
                raise ValueError(f"Для элемента {index} нужны media_type_{index} и file_{index}")
            file_size = int(values["file_size"])
            media_type = str(values["media_type"])
            if file_size == 0:
                raise ValueError("Файл не должен быть пустым")
            if media_type == "photo" and file_size > PHOTO_MAX_SIZE:
                raise ValueError("Размер фотографии превышает 10 MiB")
            uploads.append(QuickReplyUpload(
                media_type=media_type,
                path=Path(values["path"]),
                file_name=str(values["file_name"]),
                mime_type=str(values["mime_type"]) if values["mime_type"] is not None else None,
                file_size=file_size,
            ))
        return fields.get("title"), fields.get("text", ""), uploads, paths
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise


async def _stream_media(
    request: web.Request,
    file_id: str,
    *,
    kind: str,
    mime_type: str | None = None,
    file_name: str | None = None,
    thumbnail: bool = False,
):
    bot: Bot = request.app["bot"]
    telegram_file = await bot.get_file(file_id)
    if not telegram_file.file_path:
        raise _not_found("Файл не найден")
    url = bot.session.api.file_url(bot.token, telegram_file.file_path)
    stream = bot.session.stream_content(url)
    try:
        try:
            first_chunk = await anext(stream)
        except StopAsyncIteration:
            first_chunk = None
        headers = {
            "Content-Type": _safe_mime_type(kind, mime_type, thumbnail=thumbnail),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        file_size = getattr(telegram_file, "file_size", None)
        if isinstance(file_size, int) and not isinstance(file_size, bool) and file_size >= 0:
            headers["Content-Length"] = str(file_size)
        if kind == "document" and not thumbnail:
            headers["Content-Disposition"] = _attachment(file_name)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        if first_chunk:
            await response.write(first_chunk)
        async for chunk in stream:
            await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


@routes.get("/api/messages/{link_id}/media")
async def get_message_media(request: web.Request):
    session, tickets, _, employee = await _context(request)
    async with session:
        link = await session.get(MessageLink, int(request.match_info["link_id"]))
        if not link:
            raise _not_found("Сообщение не найдено")
        ticket = await tickets.get_by_id(link.ticket_id)
        if not ticket:
            raise _not_found("Обращение не найдено")
        _check(ticket, employee, request.app["config"].admin_id)
        thumbnail = request.query.get("thumbnail") == "1"
        file_id = link.thumbnail_file_id if thumbnail else link.media_file_id
        if not file_id:
            raise _not_found("Файл не найден")
        return await _stream_media(
            request,
            file_id,
            kind=_media_type(link.content_type),
            mime_type=link.mime_type,
            file_name=link.file_name,
            thumbnail=thumbnail,
        )


@routes.get("/api/tickets/{number}/media")
async def get_ticket_media(request: web.Request):
    session, tickets, _, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise _not_found("Обращение не найдено")
        _check(ticket, employee, request.app["config"].admin_id)
        kind = _media_type(ticket.media_type)
        if not ticket.media_file_id or kind not in MIME_TYPES:
            raise _not_found("Файл не найден")
        return await _stream_media(request, ticket.media_file_id, kind=kind)


@routes.get("/api/quick-replies")
async def list_quick_replies(request: web.Request):
    session, _, _, _ = await _context(request)
    async with session:
        replies = await QuickReplyService(session, request.app["bot"], request.app["config"]).list()
        return web.json_response({"quick_replies": [_quick_reply(reply) for reply in replies]})


@routes.post("/api/quick-replies")
async def create_quick_reply(request: web.Request):
    title, text, uploads, paths = await _quick_reply_form(request, metadata=True)
    try:
        session, _, _, _ = await _context(request)
        async with session:
            reply = await QuickReplyService(session, request.app["bot"], request.app["config"]).create(
                title or "", text, uploads
            )
            return web.json_response({"quick_reply": _quick_reply(reply)}, status=201)
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


@routes.patch("/api/quick-replies/{reply_id}")
async def update_quick_reply(request: web.Request):
    data = await request.json()
    if not isinstance(data, dict) or not data or set(data) - {"title", "text", "item_order"}:
        raise ValueError("Передайте title, text или item_order")
    session, _, _, _ = await _context(request)
    async with session:
        service = QuickReplyService(session, request.app["bot"], request.app["config"])
        reply = await service.get(int(request.match_info["reply_id"]))
        if reply is None:
            raise _not_found("Быстрый ответ не найден")
        values = {key: data[key] for key in ("title", "text", "item_order") if key in data}
        reply = await service.update(reply, **values)
        return web.json_response({"quick_reply": _quick_reply(reply)})


@routes.delete("/api/quick-replies/{reply_id}")
async def delete_quick_reply(request: web.Request):
    session, _, _, _ = await _context(request)
    async with session:
        service = QuickReplyService(session, request.app["bot"], request.app["config"])
        reply = await service.get(int(request.match_info["reply_id"]))
        if reply is None:
            raise _not_found("Быстрый ответ не найден")
        await service.delete_reply(reply)
        return web.json_response({"ok": True})


@routes.post("/api/quick-replies/{reply_id}/items")
async def add_quick_reply_items(request: web.Request):
    session, _, _, _ = await _context(request)
    async with session:
        service = QuickReplyService(session, request.app["bot"], request.app["config"])
        reply = await service.get(int(request.match_info["reply_id"]))
        if reply is None:
            raise _not_found("Быстрый ответ не найден")
        _, _, uploads, paths = await _quick_reply_form(request, metadata=False)
        try:
            reply = await service.add_items(reply, uploads)
            return web.json_response({"quick_reply": _quick_reply(reply)}, status=201)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)


@routes.delete("/api/quick-reply-items/{item_id}")
async def delete_quick_reply_item(request: web.Request):
    session, _, _, _ = await _context(request)
    async with session:
        service = QuickReplyService(session, request.app["bot"], request.app["config"])
        item = await session.get(QuickReplyItem, int(request.match_info["item_id"]))
        if item is None:
            raise _not_found("Элемент быстрого ответа не найден")
        await service.delete_item(item)
        return web.json_response({"ok": True})


@routes.get("/api/quick-reply-items/{item_id}/media")
async def get_quick_reply_item_media(request: web.Request):
    session, _, _, _ = await _context(request)
    async with session:
        item = await session.get(QuickReplyItem, int(request.match_info["item_id"]))
        if item is None:
            raise _not_found("Элемент быстрого ответа не найден")
        thumbnail = request.query.get("thumbnail") == "1"
        file_id = item.thumbnail_file_id if thumbnail else item.media_file_id
        if not file_id:
            raise _not_found("Файл не найден")
        return await _stream_media(
            request,
            file_id,
            kind=item.content_type,
            mime_type=item.mime_type,
            file_name=item.file_name,
            thumbnail=thumbnail,
        )


@routes.post("/api/tickets/{number}/quick-replies/{reply_id}")
async def send_quick_reply(request: web.Request):
    session, tickets, settings, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket or not ticket.user_id or not ticket.thread_id:
            raise ValueError("Обращение ещё не готово для переписки")
        _check(ticket, employee, request.app["config"].admin_id)
        if await settings.is_closed(ticket.status):
            raise ValueError("Сначала переоткройте обращение")
        service = QuickReplyService(session, request.app["bot"], request.app["config"])
        reply = await service.get(int(request.match_info["reply_id"]))
        if reply is None:
            raise _not_found("Быстрый ответ не найден")
        links = await service.send(reply, ticket)
        await request.app["topics"].update_pinned(tickets, settings, ticket)
        return web.json_response({"messages": [_message(link) for link in links]}, status=201)


@routes.get("/api/employees")
async def list_employees(request: web.Request):
    _require_admin(request)
    session, _, _, _ = await _context(request)
    async with session:
        employees = await EmployeeService(session, request.app["config"].admin_id).all(include_inactive=True)
        return web.json_response({"employees": [_employee(item) for item in employees]})


@routes.post("/api/employees")
async def create_employee(request: web.Request):
    _require_admin(request)
    data = await request.json()
    session, _, _, _ = await _context(request)
    async with session:
        employee = await EmployeeService(session, request.app["config"].admin_id).create(
            data.get("telegram_id"), data.get("name"), data.get("gender"), data.get("nickname")
        )
        await session.refresh(employee, ["shifts"])
        return web.json_response({"employee": _employee(employee)}, status=201)


@routes.patch("/api/employees/{employee_id}")
async def update_employee(request: web.Request):
    _require_admin(request)
    data = await request.json()
    allowed = {"name", "nickname", "gender", "greeting", "schedule", "active"}
    if not data or set(data) - allowed:
        raise ValueError("Передайте поддерживаемые поля сотрудника")
    if "schedule" in data and not isinstance(data["schedule"], str):
        raise ValueError("График должен быть строкой")
    if "greeting" in data and not isinstance(data["greeting"], str):
        raise ValueError("Приветствие должно быть строкой")
    shifts = parse_schedule(data["schedule"]) if "schedule" in data else None
    session, _, _, _ = await _context(request)
    async with session:
        config: Config = request.app["config"]
        employees = EmployeeService(session, config.admin_id)
        target = await employees.get_by_id(int(request.match_info["employee_id"]))
        if not target:
            raise _not_found("Сотрудник не найден")
        if target.telegram_id == config.admin_id and data.get("active") is False:
            raise ValueError("Администратора нельзя деактивировать")
        if "greeting" in data:
            await employees.update_greeting(target, data["greeting"])
        values = {key: data[key] for key in ("name", "nickname", "gender", "active") if key in data}
        if values:
            await employees.update(target, **values)
        if shifts is not None:
            await employees.set_schedule(target, shifts)
        return web.json_response({"employee": _employee(target)})


@routes.get("/api/profile")
async def get_profile(request: web.Request):
    session, _, _, employee = await _context(request)
    async with session:
        return web.json_response(await _profile(session, request.app["config"], employee))


@routes.patch("/api/profile")
async def update_profile(request: web.Request):
    data = await request.json()
    if "greeting" not in data or not isinstance(data["greeting"], str):
        raise ValueError("Укажите текст приветствия")
    session, _, _, employee = await _context(request)
    async with session:
        await EmployeeService(session, request.app["config"].admin_id).update_greeting(employee, data["greeting"])
        return web.json_response(await _profile(session, request.app["config"], employee))


@routes.get("/api/bootstrap")
async def bootstrap(request: web.Request):
    session, tickets, settings, employee = await _context(request)
    async with session:
        config: Config = request.app["config"]
        query = (
            select(Ticket)
            .options(selectinload(Ticket.employee), selectinload(Ticket.reminder))
            .where(Ticket.deleted_at.is_(None))
            .order_by(Ticket.updated_at.desc())
        )
        if employee.telegram_id != config.admin_id:
            query = query.where(or_(Ticket.employee_id == employee.id, Ticket.employee_id.is_(None)))
        items = list((await session.execute(query)).scalars())
        unread_counts = await _unread_counts(session, employee.id, items)
        employees = list((await session.execute(select(Employee).where(Employee.active))).scalars())
        return web.json_response({
            "employee": {"id": employee.id, "telegram_id": employee.telegram_id, "name": employee.name, "nickname": employee.nickname, "admin": employee.telegram_id == config.admin_id},
            "tickets": [_ticket(item, unread_count=unread_counts[item.id]) for item in items],
            "statuses": await settings.statuses(),
            "employees": [{"id": item.id, "name": item.name, "nickname": item.nickname} for item in employees],
            "faq": config.faq,
        })


@routes.get("/api/tickets/{number}/messages")
async def ticket_messages(request: web.Request):
    session, tickets, _, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise web.HTTPNotFound(text=json.dumps({"detail": "Обращение не найдено"}), content_type="application/json")
        _check(ticket, employee, request.app["config"].admin_id)
        links = list((await session.execute(
            select(MessageLink).where(MessageLink.ticket_id == ticket.id).order_by(MessageLink.created_at, MessageLink.id)
        )).scalars())
        await _mark_ticket_read(session, ticket, employee, max((link.id for link in links), default=0))
        return web.json_response({"ticket": _ticket(ticket), "messages": [_message(link) for link in links]})


@routes.post("/api/tickets/{number}/read")
async def read_ticket(request: web.Request):
    session, tickets, _, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise _not_found("Обращение не найдено")
        _check_read(ticket, employee, request.app["config"].admin_id)
        await _mark_ticket_read(session, ticket, employee)
        unread_counts = await _unread_counts(session, employee.id, [ticket])
        return web.json_response({"ticket": _ticket(ticket, unread_count=unread_counts[ticket.id])})


@routes.post("/api/tickets/{number}/claim")
async def claim_ticket(request: web.Request):
    session, tickets, settings, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise web.HTTPNotFound()
        newly_claimed = False
        if ticket.employee_id != employee.id:
            if ticket.employee_id is not None or not await tickets.claim(ticket, employee):
                raise ValueError("Обращение уже принято другим сотрудником")
            newly_claimed = True
        await request.app["topics"].create_ticket_topic(tickets, settings, ticket, employee)
        should_greet = newly_claimed and await settings.get("auto_greeting", True)
        if ticket.user_id:
            await request.app["bot"].send_message(ticket.user_id, request.app["config"].message("ticket_accepted", number=ticket.number))
            if should_greet:
                await request.app["topics"].greet(ticket, employee)
        return web.json_response({"ticket": _ticket(ticket)})


@routes.patch("/api/tickets/{number}")
async def update_ticket(request: web.Request):
    data = await request.json()
    session, tickets, settings, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise web.HTTPNotFound()
        _check(ticket, employee, request.app["config"].admin_id)
        topics: TopicService = request.app["topics"]
        if "note" in data:
            note = str(data["note"]).strip()
            if len(note) > 3000:
                raise ValueError("Описание должно быть не длиннее 3000 символов")
            ticket.note = note
            await session.commit()
        if "title" in data:
            await topics.rename(tickets, settings, ticket, str(data["title"]))
        if "employee_id" in data:
            target = await session.get(Employee, int(data["employee_id"]))
            if not target or not target.active or target.id == ticket.employee_id:
                raise ValueError("Выберите другого активного сотрудника")
            await topics.transfer(tickets, settings, ticket, target)
        if "status" in data and data["status"] != ticket.status:
            status = await settings.status(str(data["status"]))
            if status.get("closed") or status.get("archive"):
                await topics.close(tickets, settings, ticket, status)
            else:
                if await settings.is_closed(ticket.status):
                    await topics.reopen(tickets, settings, ticket)
                await tickets.set_status(ticket, status["key"], status)
        await topics.update_pinned(tickets, settings, ticket)
        return web.json_response({"ticket": _ticket(ticket)})


@routes.post("/api/tickets/{number}/pin")
async def pin_ticket(request: web.Request):
    session, tickets, settings, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise web.HTTPNotFound()
        _check(ticket, employee, request.app["config"].admin_id)
        await request.app["topics"].update_pinned(tickets, settings, ticket)
        return web.json_response({"ticket": _ticket(ticket)})


@routes.put("/api/tickets/{number}/reminder")
async def set_ticket_reminder(request: web.Request):
    data = await request.json()
    try:
        minutes = int(data["minutes"])
        if isinstance(data["minutes"], bool) or not 1 <= minutes <= 10080:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Интервал напоминания должен быть от 1 до 10080 минут") from exc
    session, tickets, _, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise web.HTTPNotFound()
        _check(ticket, employee, request.app["config"].admin_id)
        await tickets.set_reminder(ticket, employee, minutes)
        return web.json_response({"ticket": _ticket(ticket)})


@routes.delete("/api/tickets/{number}/reminder")
async def delete_ticket_reminder(request: web.Request):
    session, tickets, _, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket:
            raise web.HTTPNotFound()
        _check(ticket, employee, request.app["config"].admin_id)
        await tickets.disable_reminder(ticket)
        return web.json_response({"ticket": _ticket(ticket)})


@routes.post("/api/tickets/{number}/messages")
async def send_ticket_message(request: web.Request):
    data = await request.json()
    text = str(data.get("text", "")).strip()
    if not text or len(text) > 4096:
        raise ValueError("Сообщение должно содержать от 1 до 4096 символов")
    session, tickets, settings, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket or not ticket.user_id or not ticket.thread_id:
            raise ValueError("Обращение ещё не готово для переписки")
        _check(ticket, employee, request.app["config"].admin_id)
        if await settings.is_closed(ticket.status):
            raise ValueError("Сначала переоткройте обращение")
        bot: Bot = request.app["bot"]
        user_message = await bot.send_message(ticket.user_id, text)
        staff_message = await bot.send_message(
            request.app["config"].support_group_id,
            text,
            message_thread_id=ticket.thread_id,
        )
        link = await tickets.save_message_link(
            ticket,
            request.app["config"].support_group_id,
            staff_message.message_id,
            ticket.user_id,
            user_message.message_id,
            body=text,
            from_staff=True,
        )
        await tickets.mark_support_message(ticket)
        await request.app["topics"].update_pinned(tickets, settings, ticket)
        return web.json_response({"message": _message(link)}, status=201)


@routes.post("/api/tickets/{number}/messages/media")
async def send_ticket_media(request: web.Request):
    session, tickets, settings, employee = await _context(request)
    async with session:
        ticket = await tickets.get(int(request.match_info["number"]))
        if not ticket or not ticket.user_id or not ticket.thread_id:
            raise ValueError("Обращение ещё не готово для переписки")
        _check(ticket, employee, request.app["config"].admin_id)
        if await settings.is_closed(ticket.status):
            raise ValueError("Сначала переоткройте обращение")

        temporary_path: Path | None = None
        try:
            reader = await request.multipart()
            media_type = None
            caption = ""
            file_name = None
            mime_type = None
            file_size = 0
            seen = set()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name not in {"file", "caption", "media_type"} or part.name in seen:
                    raise ValueError("Передайте один файл, media_type и необязательную caption")
                seen.add(part.name)
                if part.name == "file":
                    file_name = _safe_file_name(part.filename)
                    mime_type = part.headers.get("Content-Type")
                    upload_dir = request.app["config"].database_path.parent / ".uploads"
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    temporary = tempfile.NamedTemporaryFile(dir=upload_dir, prefix="media-", delete=False)
                    temporary_path = Path(temporary.name)
                    try:
                        while chunk := await part.read_chunk(65536):
                            file_size += len(chunk)
                            if file_size > MEDIA_MAX_SIZE:
                                raise ValueError("Размер файла превышает 50 MiB")
                            temporary.write(chunk)
                    finally:
                        temporary.close()
                elif part.name == "caption":
                    caption = await part.text()
                    if len(caption) > 1024:
                        raise ValueError("Подпись должна быть не длиннее 1024 символов")
                else:
                    media_type = (await part.text()).strip().lower()
                    if media_type not in MEDIA_TYPES:
                        raise ValueError("Неподдерживаемый тип медиа")
            if media_type not in MEDIA_TYPES or temporary_path is None:
                raise ValueError("Передайте файл и поддерживаемый media_type")
            if file_size == 0:
                raise ValueError("Файл не должен быть пустым")
            if media_type == "photo" and file_size > PHOTO_MAX_SIZE:
                raise ValueError("Размер фотографии превышает 10 MiB")

            bot: Bot = request.app["bot"]
            sender = getattr(bot, f"send_{media_type}")
            user_message = await sender(
                ticket.user_id,
                **{media_type: FSInputFile(temporary_path, filename=file_name)},
                caption=caption or None,
                parse_mode=None,
            )
            media = extract_message_media(user_message)
            if not media.get("media_file_id"):
                raise ValueError("Telegram не вернул идентификатор файла")
            media["file_name"] = media["file_name"] or file_name
            media["mime_type"] = media["mime_type"] or mime_type
            media["file_size"] = media["file_size"] or file_size
            staff_message = await sender(
                request.app["config"].support_group_id,
                **{media_type: media["media_file_id"]},
                caption=caption or None,
                parse_mode=None,
                message_thread_id=ticket.thread_id,
            )
            link = await tickets.save_message_link(
                ticket,
                request.app["config"].support_group_id,
                staff_message.message_id,
                ticket.user_id,
                user_message.message_id,
                body=caption,
                content_type=media_type,
                from_staff=True,
                **media,
            )
            await tickets.mark_support_message(ticket)
            await request.app["topics"].update_pinned(tickets, settings, ticket)
            return web.json_response({"message": _message(link)}, status=201)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


@routes.patch("/api/messages/{link_id}")
async def edit_message(request: web.Request):
    data = await request.json()
    text = str(data.get("text", "")).strip()
    session, tickets, _, employee = await _context(request)
    async with session:
        link = await session.get(MessageLink, int(request.match_info["link_id"]))
        if not link:
            raise web.HTTPNotFound()
        ticket = await tickets.get_by_id(link.ticket_id)
        _check(ticket, employee, request.app["config"].admin_id)
        kind = _media_type(link.content_type)
        if not link.from_staff or (kind != "text" and kind not in MEDIA_TYPES):
            raise ValueError("Можно изменять только текст и подписи сообщений сотрудника")
        if kind == "text" and (not text or len(text) > 4096):
            raise ValueError("Сообщение должно содержать от 1 до 4096 символов")
        if kind != "text" and len(text) > 1024:
            raise ValueError("Подпись должна быть не длиннее 1024 символов")
        bot: Bot = request.app["bot"]
        for chat_id, message_id in (
            (link.target_chat_id, link.target_message_id),
            (link.source_chat_id, link.source_message_id),
        ):
            if kind == "text":
                await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, parse_mode=None)
            else:
                await bot.edit_message_caption(
                    caption=text or None, chat_id=chat_id, message_id=message_id, parse_mode=None
                )
        link.body = text
        await session.commit()
        return web.json_response({"message": _message(link)})


@routes.delete("/api/messages/{link_id}")
async def delete_message(request: web.Request):
    session, tickets, _, employee = await _context(request)
    async with session:
        link = await session.get(MessageLink, int(request.match_info["link_id"]))
        if not link:
            raise web.HTTPNotFound()
        ticket = await tickets.get_by_id(link.ticket_id)
        _check(ticket, employee, request.app["config"].admin_id)
        if not link.from_staff:
            raise ValueError("Из Mini App можно удалять только сообщения сотрудника")
        await request.app["topics"].delete_relayed(link, delete_source=True)
        await session.delete(link)
        await session.commit()
        return web.json_response({"ok": True})


@routes.put("/api/faq")
async def update_faq(request: web.Request):
    if request["telegram_id"] != request.app["config"].admin_id:
        raise PermissionDenied("Редактировать быстрые ответы может только администратор")
    data = await request.json()
    items = data.get("faq")
    if not isinstance(items, list) or not 1 <= len(items) <= 30:
        raise ValueError("Добавьте от 1 до 30 быстрых ответов")
    clean = []
    ids = set()
    for index, item in enumerate(items, start=1):
        faq_id = str(item.get("id", "")).strip()
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if not faq_id or not question or not answer or len(question) > 200 or len(answer) > 4000:
            raise ValueError(f"Проверьте поля быстрого ответа №{index}")
        if faq_id in ids:
            raise ValueError("Идентификаторы быстрых ответов должны быть уникальны")
        ids.add(faq_id)
        clean.append({"id": faq_id, "question": question, "answer": answer})
    config: Config = request.app["config"]
    config.content["faq"] = clean
    path = Path(os.getenv("CONTENT_PATH", "config/content.yaml"))
    if not path.is_absolute():
        path = config.root / path
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(config.content, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temporary.replace(path)
    return web.json_response({"faq": clean})


def create_miniapp(config: Config, db: Database, bot: Bot, topics: TopicService) -> web.Application:
    application = web.Application(
        middlewares=[security_middleware],
        client_max_size=QUICK_REPLY_MAX_FILES * MEDIA_MAX_SIZE + 1024 * 1024,
    )
    application["config"] = config
    application["db"] = db
    application["bot"] = bot
    application["topics"] = topics
    application.add_routes(routes)
    return application


async def start_miniapp(config: Config, db: Database, bot: Bot, topics: TopicService):
    runner = web.AppRunner(create_miniapp(config, db, bot, topics))
    await runner.setup()
    host = os.getenv("MINIAPP_API_HOST", "127.0.0.1")
    port = int(os.getenv("MINIAPP_API_PORT", "7101"))
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Mini App API запущен на http://%s:%s", host, port)
    return runner
