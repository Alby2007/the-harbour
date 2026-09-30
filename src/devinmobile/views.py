from collections.abc import Awaitable, Callable
from typing import Any, cast

import discord

PREFIX = "dvm"
ACTIONS = ("ssh", "refresh", "approve")

ComponentHandler = Callable[[discord.Interaction, str, str], Awaitable[None]]

# discord.py fires BOTH a live View's callback and Client.on_interaction for
# the same click on the same Interaction object. First entrant claims the
# interaction id; the loser no-ops. Post-restart the View is gone and
# on_interaction is the only path -- which is why custom_ids carry the
# session id instead of relying on view state.
_INFLIGHT: set[int] = set()


def make_custom_id(action: str, session_id: str) -> str:
    return f"{PREFIX}:{action}:{session_id}"


def parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    parts = custom_id.split(":", 2)
    if len(parts) == 3 and parts[0] == PREFIX and parts[1] in ACTIONS:
        return parts[1], parts[2]
    return None


async def dispatch(
    interaction: discord.Interaction,
    handler: ComponentHandler,
) -> None:
    data = cast(dict[str, Any], interaction.data or {})
    custom_id = data.get("custom_id")
    parsed = parse_custom_id(str(custom_id)) if custom_id else None
    if parsed is None or interaction.id in _INFLIGHT:
        return
    _INFLIGHT.add(interaction.id)
    try:
        await handler(interaction, *parsed)
    finally:
        _INFLIGHT.discard(interaction.id)


class _CbButton(discord.ui.Button):
    def __init__(
        self, *, label: str, custom_id: str, handler: ComponentHandler
    ) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary, label=label, custom_id=custom_id
        )
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await dispatch(interaction, self._handler)


class SessionView(discord.ui.View):
    """Buttons on a session's anchor message.

    Each button's callback routes through dispatch() so clicks work both
    while this view is alive and after a restart (via on_interaction).
    """

    def __init__(self, session_id: str, url: str, handler: ComponentHandler) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, label="Open in Devin", url=url,
        ))
        for action, label in (("refresh", "Refresh"), ("approve", "Approve"), ("ssh", "SSH")):
            self.add_item(_CbButton(
                label=label,
                custom_id=make_custom_id(action, session_id),
                handler=handler,
            ))
