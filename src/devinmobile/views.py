from collections.abc import Awaitable, Callable
from typing import Any, cast

import discord

PREFIX = "dvm"
ACTIONS = ("ssh", "refresh", "approve", "choose", "chain_next",
           "pr_merge", "pr_merge_go", "pr_approve", "pr_close", "pr_automerge",
           "fix_ci", "send_review", "post_review")

ComponentHandler = Callable[[discord.Interaction, str, str, str | None], Awaitable[None]]

# discord.py fires BOTH a live View's callback and Client.on_interaction for
# the same click on the same Interaction object. First entrant claims the
# interaction id; the loser no-ops. Post-restart the View is gone and
# on_interaction is the only path -- which is why custom_ids carry the
# session id instead of relying on view state.
_INFLIGHT: set[int] = set()


def make_custom_id(action: str, session_id: str, extra: str | None = None) -> str:
    cid = f"{PREFIX}:{action}:{session_id}"
    return f"{cid}:{extra}" if extra else cid


def parse_custom_id(custom_id: str) -> tuple[str, str, str | None] | None:
    parts = custom_id.split(":", 3)
    if len(parts) >= 3 and parts[0] == PREFIX and parts[1] in ACTIONS:
        return parts[1], parts[2], parts[3] if len(parts) == 4 else None
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
        self,
        *,
        label: str,
        custom_id: str,
        handler: ComponentHandler,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(style=style, label=label, custom_id=custom_id)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await dispatch(interaction, self._handler)


class PRView(discord.ui.View):
    """Buttons on a PR card posted into a session thread.

    `Merge` asks `handle_component` to open an ephemeral confirm (a second
    click on the `pr_merge_go` button is the actual merge — keeps a fat-finger
    tap from merging).
    """

    def __init__(
        self, session_id: str, pr_key: str, pr_url: str, handler: ComponentHandler
    ) -> None:
        """pr_key is `owner/repo#number` — carries full PR identity so a
        multi-repo session can't act on the wrong repo's same-numbered PR."""
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, label="Open on GitHub", url=pr_url,
        ))
        for action, label, style in (
            ("pr_merge", "Merge", discord.ButtonStyle.success),
            ("pr_approve", "Approve", discord.ButtonStyle.secondary),
            ("pr_automerge", "Auto-merge", discord.ButtonStyle.secondary),
            ("pr_close", "Close PR", discord.ButtonStyle.danger),
        ):
            self.add_item(_CbButton(
                label=label,
                custom_id=make_custom_id(action, session_id, pr_key),
                handler=handler,
                style=style,
            ))


class FixCIView(discord.ui.View):
    """One button posted with a CI-failure notice — forwards the failing
    check names back to the Devin session so it can fix them."""

    def __init__(self, session_id: str, pr_key: str, handler: ComponentHandler) -> None:
        super().__init__(timeout=None)
        self.add_item(_CbButton(
            label="Ask Devin to fix",
            custom_id=make_custom_id("fix_ci", session_id, pr_key),
            handler=handler,
            style=discord.ButtonStyle.primary,
        ))


class ReviewNotifyView(discord.ui.View):
    """Button on a relayed PR review — sends the review feedback to the
    session as a steering message."""

    def __init__(self, session_id: str, pr_key: str, handler: ComponentHandler) -> None:
        super().__init__(timeout=None)
        self.add_item(_CbButton(
            label="Send to Devin",
            custom_id=make_custom_id("send_review", session_id, pr_key),
            handler=handler,
            style=discord.ButtonStyle.secondary,
        ))


class PostReviewView(discord.ui.View):
    """Button on a review session's completion card — posts the review
    findings upstream as a COMMENT review on the human's PR."""

    def __init__(
        self, session_id: str, pr_key: str, handler: ComponentHandler
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(_CbButton(
            label="Post review to GitHub",
            custom_id=make_custom_id("post_review", session_id, pr_key),
            handler=handler,
            style=discord.ButtonStyle.primary,
        ))


class CompletionView(discord.ui.View):
    """Buttons on a session's completion card — any subset of:

    - Post review to GitHub (when `review_of` is set)
    - Continue → {phase} (when a playbook chain is awaiting a human gate)

    Both params optional; a view with neither is never constructed.
    """

    def __init__(
        self,
        session_id: str,
        handler: ComponentHandler,
        *,
        review_of: str = "",
        chain_next_label: str = "",
    ) -> None:
        super().__init__(timeout=None)
        if review_of:
            self.add_item(_CbButton(
                label="Post review to GitHub",
                custom_id=make_custom_id("post_review", session_id, review_of),
                handler=handler,
                style=discord.ButtonStyle.primary,
            ))
        if chain_next_label:
            self.add_item(_CbButton(
                label=f"Continue → {chain_next_label}"[:80],
                custom_id=make_custom_id("chain_next", session_id),
                handler=handler,
                style=discord.ButtonStyle.success,
            ))


class ChoiceView(discord.ui.View):
    """One button per option on a Devin question.

    Stateless like every dvm: view — the option text lives on the button's
    own label, so post-restart clicks (on_interaction) still send the right
    answer; `extra` carries the 1-based index only as a fallback.
    """

    def __init__(
        self, session_id: str, options: list[str], handler: ComponentHandler
    ) -> None:
        super().__init__(timeout=None)
        for i, opt in enumerate(options, 1):
            self.add_item(_CbButton(
                label=f"{i}. {opt}"[:80],
                custom_id=make_custom_id("choose", session_id, str(i)),
                handler=handler,
            ))


class MergeConfirmView(discord.ui.View):
    """Ephemeral confirm for PR merges."""

    def __init__(
        self, session_id: str, pr_key: str, handler: ComponentHandler
    ) -> None:
        super().__init__(timeout=60)
        btn = _CbButton(
            label="Confirm merge",
            custom_id=make_custom_id("pr_merge_go", session_id, pr_key),
            handler=handler,
        )
        btn.style = discord.ButtonStyle.danger
        self.add_item(btn)


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
