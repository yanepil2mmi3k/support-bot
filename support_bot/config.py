from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_id: int
    support_group_id: int
    database_path: Path
    timezone_name: str
    log_level: str
    root: Path
    content: dict[str, Any]
    defaults: dict[str, Any]
    miniapp_url: str | None = None

    @classmethod
    def load(cls) -> Config:
        load_dotenv()
        root = Path.cwd()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("Укажите BOT_TOKEN в .env")
        try:
            admin_id = int(os.getenv("ADMIN_ID", ""))
            group_id = int(os.getenv("SUPPORT_GROUP_ID", ""))
        except ValueError as exc:
            raise ConfigError("ADMIN_ID и SUPPORT_GROUP_ID должны быть целыми числами") from exc
        if admin_id <= 0:
            raise ConfigError("ADMIN_ID должен быть положительным Telegram ID")
        timezone_name = os.getenv("TIMEZONE", "Europe/Moscow")
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise ConfigError(f"Неизвестный часовой пояс: {timezone_name}") from exc
        content_path = Path(os.getenv("CONTENT_PATH", "config/content.yaml"))
        settings_path = Path(os.getenv("SETTINGS_PATH", "config/settings.yaml"))
        content = _load_yaml(root / content_path if not content_path.is_absolute() else content_path)
        defaults = _load_yaml(root / settings_path if not settings_path.is_absolute() else settings_path)
        _validate(content, defaults)
        database_path = Path(os.getenv("DATABASE_PATH", "data/support.db"))
        if not database_path.is_absolute():
            database_path = root / database_path
        return cls(
            bot_token=token,
            admin_id=admin_id,
            support_group_id=group_id,
            database_path=database_path,
            timezone_name=timezone_name,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            root=root,
            content=content,
            defaults=defaults,
            miniapp_url=os.getenv("MINIAPP_URL", "").strip() or None,
        )

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path.as_posix()}"

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def message(self, key: str, **values: Any) -> str:
        template = self.content["messages"].get(key, key)
        return str(template).format(**values)

    @property
    def faq(self) -> list[dict[str, str]]:
        return list(self.content.get("faq", []))


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Не найден конфигурационный файл: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка YAML в {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Корень YAML должен быть объектом: {path}")
    return data


def _validate(content: dict[str, Any], defaults: dict[str, Any]) -> None:
    if not isinstance(content.get("messages"), dict) or not isinstance(content.get("faq"), list):
        raise ConfigError("В content.yaml нужны разделы messages и faq")
    statuses = defaults.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        raise ConfigError("В settings.yaml нужен непустой список statuses")
    keys = {item.get("key") for item in statuses if isinstance(item, dict)}
    if not {"new", "work", "archive"}.issubset(keys):
        raise ConfigError("Обязательные статусы: new, work, archive")
    if not isinstance(defaults.get("categories"), list):
        raise ConfigError("В settings.yaml нужен список categories")


def setup_logging(config: Config) -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = config.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "support-bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=config.log_level,
        handlers=[console_handler, file_handler],
        force=True,
    )
