from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError

from support_bot.config import Config
from support_bot.db.session import Database
from support_bot.services.employees import EmployeeService
from support_bot.services.tickets import TicketService

ROOT = Path(__file__).resolve().parents[1]


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.failures: set[str] = set()
        self.next_message_id = 1000
        self.next_thread_id = 500
        self.token = "test-token"
        self.media_contents: dict[str, bytes] = {}
        self.media_metadata: dict[str, dict] = {}
        self.closed_streams = 0
        self._file_urls: dict[str, str] = {}
        self.session = SimpleNamespace(
            api=SimpleNamespace(file_url=self._file_url),
            stream_content=self._stream_content,
        )

    def fail_next(self, method: str) -> None:
        self.failures.add(method)

    def called(self, method: str) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for name, args, kwargs in self.calls if name == method]

    def _file_url(self, token: str, file_path: str) -> str:
        self.calls.append(("file_url", (token, file_path), {}))
        url = f"https://files.test/{file_path}"
        self._file_urls[url] = file_path
        return url

    async def _stream_content(self, url: str, *, chunk_size: int = 65536, **kwargs):
        self.calls.append(("stream_content", (url,), {"chunk_size": chunk_size, **kwargs}))
        try:
            if "stream_content" in self.failures:
                self.failures.remove("stream_content")
                raise TelegramAPIError(SimpleNamespace(), "Сбой stream_content в тесте")
            file_id = self._file_urls[url].removeprefix("files/")
            content = self.media_contents.get(file_id, b"fake media")
            for offset in range(0, len(content), chunk_size):
                yield content[offset:offset + chunk_size]
        finally:
            self.closed_streams += 1

    async def _call(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method in self.failures:
            self.failures.remove(method)
            raise TelegramAPIError(SimpleNamespace(), f"Сбой {method} в тесте")
        if method.startswith("send_") and method.removeprefix("send_") in {
            "photo", "video", "document", "animation", "audio", "voice", "video_note",
        }:
            media_type = method.removeprefix("send_")
            self.next_message_id += 1
            media = kwargs.get(media_type)
            if isinstance(media, str):
                file_id = media
                self.media_contents.setdefault(file_id, b"fake media")
                metadata = dict(self.media_metadata.get(file_id, {}))
            else:
                file_id = f"{media_type}-{self.next_message_id}"
                content = Path(media.path).read_bytes()
                self.media_contents[file_id] = content
                metadata = {
                    "file_name": media.filename,
                    "mime_type": None,
                    "file_size": len(content),
                    "thumbnail": None,
                }
            self.media_metadata[file_id] = metadata
            file = SimpleNamespace(
                file_id=file_id,
                file_name=metadata.get("file_name"),
                mime_type=metadata.get("mime_type"),
                file_size=metadata.get("file_size", len(self.media_contents[file_id])),
                thumbnail=metadata.get("thumbnail"),
            )
            return SimpleNamespace(
                message_id=self.next_message_id,
                content_type=media_type,
                caption=kwargs.get("caption"),
                **{media_type: [file] if media_type == "photo" else file},
            )
        if method in {"send_message", "copy_message"}:
            self.next_message_id += 1
            return SimpleNamespace(message_id=self.next_message_id)
        if method == "get_file":
            file_id = args[0]
            content = self.media_contents.setdefault(file_id, b"fake media")
            return SimpleNamespace(file_id=file_id, file_path=f"files/{file_id}", file_size=len(content))
        if method == "create_forum_topic":
            self.next_thread_id += 1
            return SimpleNamespace(message_thread_id=self.next_thread_id)
        if method == "edit_message_text":
            return SimpleNamespace(message_id=kwargs["message_id"])
        return True

    def __getattr__(self, method: str):
        async def telegram_method(*args, **kwargs):
            return await self._call(method, *args, **kwargs)

        return telegram_method


class FakeState:
    def __init__(self, data: dict | None = None, state: str | None = None) -> None:
        self.data = dict(data or {})
        self.state = state
        self.clear_count = 0

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_data(self, data: dict) -> None:
        self.data = dict(data)

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def set_state(self, state) -> None:
        self.state = state.state if hasattr(state, "state") else state

    async def clear(self) -> None:
        self.clear_count += 1
        self.data = {}
        self.state = None


class FakeMessage:
    def __init__(
        self,
        user_id: int,
        text: str = "",
        *,
        chat_id: int | None = None,
        thread_id: int | None = None,
        message_id: int = 100,
    ) -> None:
        self.from_user = SimpleNamespace(id=user_id, is_bot=False, full_name=f"User {user_id}")
        self.chat = SimpleNamespace(id=chat_id if chat_id is not None else user_id, type="private" if chat_id is None else "supergroup")
        self.message_thread_id = thread_id
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.entities = None
        self.caption_entities = None
        self.photo = None
        self.video = None
        self.document = None
        self.audio = None
        self.animation = None
        self.voice = None
        self.video_note = None
        self.sticker = None
        self.content_type = ContentType.TEXT
        self.reply_to_message = None
        self.bot = FakeBot()
        self.answers: list[dict] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=9000 + len(self.answers))

    async def edit_text(self, text: str, **kwargs):
        self.text = text
        self.edited_kwargs = kwargs
        return self

    async def delete(self):
        self.deleted = True
        return True


class FakeCallback:
    def __init__(self, user_id: int, data: str, bot: FakeBot | None = None) -> None:
        self.from_user = SimpleNamespace(
            id=user_id,
            username=f"user{user_id}",
            full_name=f"User {user_id}",
            is_bot=False,
        )
        self.data = data
        self.bot = bot or FakeBot()
        self.message = FakeMessage(user_id)
        self.message.bot = self.bot
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **kwargs) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        bot_token="test-token",
        admin_id=9000,
        support_group_id=-100500,
        database_path=tmp_path / "test.db",
        timezone_name="Europe/Moscow",
        log_level="INFO",
        root=ROOT,
        content=yaml.safe_load((ROOT / "config" / "content.yaml").read_text(encoding="utf-8")),
        defaults=yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")),
    )


@pytest_asyncio.fixture
async def db(config: Config):
    database = Database(config.database_url, config.database_path)
    await database.init()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def employee(db: Database, config: Config):
    async with db.sessions() as session:
        return await EmployeeService(session, config.admin_id).create(1001, "Оператор", "f", "Оператор")


@pytest_asyncio.fixture
async def ticket_number(db: Database, config: Config, employee) -> int:
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).create(
            user_id=2002,
            username="client",
            display_name="Клиент",
            category="Другое",
            description="Проверка закрытия",
        )
        await TicketService(session, config.admin_id).assign(ticket, employee)
        ticket.thread_id = 777
        ticket.pinned_message_id = 555
        await session.commit()
        return ticket.number
