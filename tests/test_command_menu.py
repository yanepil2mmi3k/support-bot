from __future__ import annotations

from aiogram.types import BotCommandScopeChat, BotCommandScopeChatMember
from conftest import FakeBot

from support_bot.app import configure_commands


def names(call) -> set[str]:
    return {command.command for command in call[0][0]}


async def test_command_menu_is_scoped_and_documented(config) -> None:
    bot = FakeBot()

    await configure_commands(bot, config)

    calls = bot.called("set_my_commands")
    assert len(calls) == 5
    default_commands = names(calls[0])
    assert default_commands == {"start", "help", "delete"}
    assert "admin" not in default_commands
    assert "profile" not in default_commands

    staff_call = next(call for call in calls if isinstance(call[1].get("scope"), BotCommandScopeChat))
    staff_commands = names(staff_call)
    assert {"ticket", "profile", "status", "note", "description", "rename", "transfer", "remind", "remind_off", "pin", "close", "open", "delete"} <= staff_commands
    assert "admin" not in staff_commands

    admin_call = next(call for call in calls if isinstance(call[1].get("scope"), BotCommandScopeChatMember))
    admin_commands = names(admin_call)
    assert staff_commands <= admin_commands
    assert {"admin", "setup", "report", "reviews", "archive", "reset_counter"} <= admin_commands

    for call in calls:
        for command in call[0][0]:
            assert command.description.strip()
            assert "—" not in command.description
            assert "–" not in command.description
