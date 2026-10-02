import asyncio
import logging
from typing import Any, cast

import discord
from discord import app_commands

from ..acp_bridge import AcpBridge, SessionStream
from ..config import Settings
from ..db import Binding, Database
from ..devin_client import DevinClient
from ..embeds import status_embed
from ..github_client import GithubClient, PullRef, parse_issue_ref
from ..links import enrich as enrich_links
from ..relay import Relay
from ..scheduler import Scheduler
from ..spawn import SpawnError, spawn_session
from ..views import CompletionView, MergeConfirmView, dispatch
from ..webhook_server import WebhookServer, maybe_start
from .commands import (
    _is_operator,
    _may_destroy,
    register_commands,
    user_role_ids,
)

log = logging.getLogger(__name__)


class DevinMobileBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.settings = settings
        self.db: Database
        self.devin: DevinClient
        self.bridge: AcpBridge
        self.github: GithubClient | None = None
        self.relay: Relay
        self._relay_task: asyncio.Task | None = None
        self._webhook: WebhookServer | None = None
        self._scheduler: Scheduler | None = None
        self._stream: SessionStream | None = None

    async def setup_hook(self) -> None:
        self.db = await Database.connect(self.settings.db_path)
        self.devin = DevinClient(
            self.settings.devin_api_key,
            self.settings.devin_org_id,
            self.settings.devin_base_url,
        )
        self.bridge = AcpBridge(
            self.settings.devin_credentials_path,
            api_url=self.settings.devin_api_url_override,
            timeout=self.settings.bridge_timeout,
        )
        if not self.bridge.available:
            log.warning(
                "no CLI credentials at %s — the model: option on /devin is disabled "
                "until `devin auth login` runs on this host",
                self.settings.devin_credentials_path,
            )
        else:
            # Warm the repo/model catalog so slash-command autocomplete is
            # instant — the first keystroke shouldn't pay a WS round-trip.
            async def _warm_catalog() -> None:
                try:
                    await self.bridge.catalog()
                except Exception:
                    log.warning("bridge catalog prefetch failed", exc_info=True)

            asyncio.create_task(_warm_catalog())
        if self.settings.github_enabled:
            self.github = GithubClient(
                self.settings.github_app_id,
                self.settings.github_app_private_key_path,
                self.settings.github_app_installation_id,
            )
        elif any([
            self.settings.github_app_id,
            self.settings.github_app_private_key_path,
            self.settings.github_app_installation_id,
        ]):
            log.warning("partial GITHUB_APP_* config — GitHub features disabled")
        self.relay = Relay(
            self, self.devin, self.db, self.settings, self.github,
            component_handler=self.handle_component,
        )
        if self.bridge.available and self.settings.acp_progress:
            # Live turn progress via the bridge's session/update stream.
            self._stream = SessionStream(self.bridge, on_update=self.relay.on_progress)
            self.relay.streamer = self._stream
        self._scheduler = Scheduler(self)
        self._scheduler.start()
        self._webhook = await maybe_start(self, self.db, self.settings)
        register_commands(self)
        if self.settings.command_guild_id:
            guild = discord.Object(id=self.settings.command_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()  # global: makes commands usable in DMs
        self._relay_task = asyncio.create_task(self.relay.run_forever())

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("online as %s (%s)", self.user, self.user.id)

    async def close(self) -> None:
        if self._relay_task:
            self._relay_task.cancel()
        if self.relay:
            await self.relay.aclose()
        if self.db:
            await self.db.close()
        if self.devin:
            await self.devin.aclose()
        if self.github:
            await self.github.aclose()
        if self._webhook:
            await self._webhook.stop()
        if self._scheduler:
            await self._scheduler.stop()
        await super().close()

    # ---- steering: anything an allowed user types in a bound thread goes to
    # the session; attachments ride along as attachment_urls.

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or (not message.content and not message.attachments):
            return
        # a DM (or group DM — no guild) is the phone-first intake surface;
        # unbound guild channels/threads stay inert
        if message.guild is None:
            await self._dm_intake(message)
            return
        binding = await self.db.get_binding_by_thread(message.channel.id)
        # a deleted session's thread (or a non-thread channel message) is
        # inert — a stray reply must not reactivate a deleted binding
        if binding is None or binding.deleted:
            return
        if not await _is_operator(
            self, message.author.id, user_role_ids(message.author)
        ):
            return
        content = message.content
        attachments = [a.url for a in message.attachments] or None
        voice = next(
            (a for a in message.attachments
             if (a.content_type or "").startswith("audio/")
             or getattr(a, "waveform", None)),
            None,
        )
        # Voice notes land as audio/* attachments — transcribe and steer.
        # A captioned voice note merges transcript + caption rather than
        # shipping Devin an audio URL it can't use.
        if voice is not None and self.settings.openai_api_key:
            from ..transcribe import transcribe  # local import: httpx lazily

            try:
                transcript = await transcribe(
                    voice.url, self.settings.openai_api_key
                )
                attachments = [
                    a.url for a in message.attachments if a is not voice
                ] or None
                if transcript:
                    content = f"{transcript}\n\n{content}".strip()
                    await message.reply(
                        f"🎤 _{transcript}_", mention_author=False
                    )
            except Exception:
                log.exception("voice transcription failed")
                await message.add_reaction("🎤")
                return
        elif voice is not None:
            # no transcription key — don't ship Devin an inert audio URL
            attachments = [
                a.url for a in message.attachments if a is not voice
            ] or None
        if message.reference and message.reference.message_id:
            # Reply-quoting: "yes" / "that one" means nothing to the session
            # without what it refers to — the referenced text rides along.
            quote = await self._reply_quote(message)
            if quote:
                content = f're: "{quote}"\n\n{content}'
        n_links = 0
        if content and ("http://" in content or "https://" in content):
            # Fetch link contents here — Devin's sandbox browser can't reach
            # private GitHub (our App can) or anything paywalled, so the
            # extracted text rides inside the prompt.
            try:
                content, n_links = await enrich_links(content, self.github)
            except Exception:
                log.exception("link enrichment failed")
        try:
            session = await self.devin.send_message(
                binding.session_id,
                content or "(attachment)",
                attachment_urls=attachments,
                message_as_user_id=self.settings.create_as_user_id,
            )
            # Same lock as the poll loop — an in-flight poll can't overwrite
            # this reactivation with a stale parked status.
            async with self.relay._lock(binding.session_id):
                binding.status = session.status
                binding.status_detail = session.status_detail
                binding.active = True
                await self.db.upsert_binding(binding)
            await message.add_reaction("\u2705")
            if n_links:
                await message.channel.send(
                    f"📎 Read {n_links} link{'s' if n_links > 1 else ''} for Devin"
                )
            # Placeholder covers the send→first-output dead air; it morphs
            # into the Working list on the first streamed tool call and is
            # deleted when the reply lands.
            await self.relay.progress.thinking(binding)
            # Devin's ack typically lands within seconds — don't make the
            # reply wait for the next scheduled tick.
            self.relay.request_poll(binding)
        except Exception:
            log.exception("forward failed for %s", binding.session_id)
            await message.add_reaction("\u274C")

    # ---- DM intake ----------------------------------------------------------
    # Phone-first entry: DM an attachment (screenshot, photo, file) or a
    # `devin:`-prefixed text and a session spawns into the hub channel.
    # Stray chatter stays inert — no prefix + no attachment → one-line hint.

    _DM_HINT = "DM me `devin: <task>` or attach a file to start a session."

    async def _dm_intake(self, message: discord.Message) -> None:
        # DM contexts carry no Member.roles — allowlist ∪ db only, a
        # guild role can't grant DM access
        if not await _is_operator(self, message.author.id):
            return  # silence — a stranger's DM dies quietly
        content = (message.content or "").strip()
        attachments = list(message.attachments)
        # prefix strips before voice merges — "devin: x" in a caption stays
        # a prefix even when a transcript lands in front of it
        prefixed = content.lower().startswith("devin:")
        if prefixed:
            content = content[6:].strip()
        voice = next(
            (a for a in attachments
             if (a.content_type or "").startswith("audio/")
             or getattr(a, "waveform", None)),
            None,
        )
        from_voice = False
        if voice is not None:
            if not self.settings.openai_api_key:
                if not content:
                    await message.reply(
                        "Voice notes need OPENAI_API_KEY on the bot host.",
                        mention_author=False,
                    )
                    return
                # caption+voice without a key: drop the inert audio URL
                # rather than attach a file Devin can't use — and say so
                attachments = [a for a in attachments if a is not voice]
                await message.reply(
                    "No OPENAI_API_KEY — skipping the voice note.",
                    mention_author=False,
                )
            else:
                from ..transcribe import transcribe  # local: httpx lazily

                try:
                    transcript = await transcribe(
                        voice.url, self.settings.openai_api_key
                    )
                except Exception:
                    log.exception("dm voice transcription failed")
                    await message.add_reaction("🎤")
                    return
                attachments = [a for a in attachments if a is not voice]
                from_voice = True
                if transcript:
                    # voice IS the task; a caption adds context. A dictated
                    # transcript starting "devin:" gets prefix-stripped just
                    # like typed text — natural.
                    content = f"{transcript}\n\n{content}".strip()
                    await message.reply(
                        f"🎤 _{transcript}_", mention_author=False
                    )
        if not (prefixed or attachments or from_voice):
            await message.reply(self._DM_HINT, mention_author=False)
            return
        if content:
            prompt = content
        elif attachments:
            prompt = (
                "The user sent attached file(s) with no instructions — "
                "analyze them and report what you find / what you'd need "
                "next."
            )
        else:
            # "devin:" with nothing after it — a hint, not a spawn
            await message.reply(self._DM_HINT, mention_author=False)
            return
        if "http://" in prompt or "https://" in prompt:
            try:
                prompt, _ = await enrich_links(prompt, self.github)
            except Exception:
                log.exception("dm link enrichment failed")
        try:
            _, thread = await spawn_session(
                self,
                prompt=prompt,
                attachment_urls=[a.url for a in attachments] or None,
                spawned_by=str(message.author.id),
            )
        except SpawnError as e:
            await message.reply(str(e), mention_author=False)
            return
        except Exception:
            # create_thread HTTPException et al aren't SpawnError — the
            # phone user still needs a failure signal, not silence
            log.exception("dm spawn crashed")
            await message.reply(
                "Spawn failed — check the bot log.", mention_author=False
            )
            return
        await message.reply(
            f"Session started → {thread.jump_url}", mention_author=False
        )
        try:
            await thread.send("Spawned via DM.")
        except Exception:
            log.warning("dm provenance note failed for %s", thread.id)

    async def _reply_quote(self, message: discord.Message) -> str | None:
        """Resolve a reply's referenced message to a ≤300-char quote."""
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        ref_msg = ref.resolved
        if ref_msg is None:
            try:
                ref_msg = await message.channel.fetch_message(ref.message_id)
            except (discord.HTTPException, AttributeError):
                return None
        if isinstance(ref_msg, discord.DeletedReferencedMessage):
            return None
        text = (ref_msg.content or "").strip()
        if not text and ref_msg.embeds:
            e = ref_msg.embeds[0]
            text = " — ".join(
                p for p in (e.title or "", e.description or "", e.url or "") if p
            )
        if not text:
            return None
        return " ".join(text.split())[:300]

    # ---- emoji-reaction commands: one-tap steering on phone ---------------
    # Raw events fire regardless of message cache; Intents.reactions is in
    # Intents.default() so no extra intent wiring is needed.

    _REACT_EMOJI = frozenset({"👍", "🔁", "⏸️"})

    async def on_raw_reaction_add(self, event: discord.RawReactionActionEvent) -> None:
        emoji = event.emoji.name or ""
        if emoji not in self._REACT_EMOJI:
            return  # cheap filter first — the operator check hits the db
        # event.member is a Member in guilds, None in DMs (id-only there)
        if not await _is_operator(
            self, event.user_id, user_role_ids(event.member)
        ):
            return
        binding = await self.db.get_binding_by_thread(event.channel_id)
        if binding is None:
            return
        chan = self.get_channel(event.channel_id) or await self.fetch_channel(
            event.channel_id
        )
        if not isinstance(chan, discord.abc.Messageable):
            return
        try:
            msg = await chan.fetch_message(event.message_id)
        except (discord.HTTPException, AttributeError):
            return
        await self._handle_reaction(binding, msg, emoji, event.user_id)

    async def _handle_reaction(
        self, binding, msg: discord.Message, emoji: str, user_id: int
    ) -> None:
        is_anchor = msg.id == binding.anchor_msg_id
        pr_row = (
            None
            if is_anchor
            else await self.db.get_pr_by_card(binding.session_id, msg.id)
        )
        try:
            if emoji == "👍":
                if pr_row is not None:
                    if self.github is None:
                        return
                    await self.github.approve_pr(
                        PullRef(owner=pr_row.owner, repo=pr_row.repo,
                                number=pr_row.number)
                    )
                    await msg.channel.send(
                        f"👍 {pr_row.repo}#{pr_row.number} approved"
                    )
                elif is_anchor:
                    await self.devin.send_message(
                        binding.session_id,
                        "Approved — please proceed.",
                        message_as_user_id=self.settings.create_as_user_id,
                    )
                    await self.relay.progress.thinking(binding)
                    self.relay.request_poll(binding)
            elif emoji == "🔁":
                if is_anchor:
                    self.relay.request_poll(binding)
                elif pr_row is not None and self.github is not None:
                    await self.relay._poll_pr(
                        binding, pr_row,
                        PullRef(owner=pr_row.owner, repo=pr_row.repo,
                                number=pr_row.number),
                    )
                elif any(
                    r.me and str(r.emoji) == "❌" for r in msg.reactions
                ):
                    # resend a message whose original forward failed
                    content = msg.content
                    if "http://" in content or "https://" in content:
                        content, _ = await enrich_links(content, self.github)
                    await self.devin.send_message(
                        binding.session_id,
                        content or "(attachment)",
                        message_as_user_id=self.settings.create_as_user_id,
                    )
                    await msg.add_reaction("✅")
                    if self.user is not None:
                        try:
                            await msg.remove_reaction("❌", self.user)
                        except discord.HTTPException:
                            pass
                    await self.relay.progress.thinking(binding)
                    self.relay.request_poll(binding)
            elif emoji == "⏸️" and is_anchor:
                # same destructive-op gate as /kill — parking someone
                # else's session needs owner-or-admin
                if not _may_destroy(self.settings, user_id, binding.spawned_by):
                    await msg.channel.send(
                        "⏸️ only the owner or an admin can park this "
                        "session."
                    )
                    return
                binding.active = False
                await self.db.upsert_binding(binding)
                await msg.channel.send(
                    "⏸️ parked — reply in this thread to resume."
                )
        except Exception as e:  # noqa: BLE001 — reactions can't go ephemeral
            log.exception("reaction %s failed on %s", emoji, binding.session_id)
            try:
                await msg.channel.send(f"`{e}`")
            except discord.HTTPException:
                pass

    # ---- component clicks (post-restart path; live views dedupe via _INFLIGHT)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        await dispatch(interaction, self.handle_component)

    async def handle_component(
        self,
        interaction: discord.Interaction,
        action: str,
        session_id: str,
        extra: str | None = None,
    ) -> None:
        if not await _is_operator(
            self, interaction.user.id, user_role_ids(interaction.user)
        ):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This bot is locked to its owner.", ephemeral=True
                )
            return
        binding = await self.db.get_binding(session_id)
        if action.startswith("pr_"):
            await self._handle_pr_action(interaction, action, session_id, extra)
            return
        if action == "ssh":
            await interaction.response.send_message(
                f"```\nssh {session_id}@ssh.devin.ai\n```\n"
                f"CLI: `devin ssh {session_id}` · "
                f"forward a port: `devin forward {session_id} 3000`\n"
                "The gateway prints a URL — open it in a browser to approve the connection.",
                ephemeral=True,
            )
        elif action == "refresh":
            await interaction.response.defer()
            try:
                session = await self.devin.get_session(session_id)
            except Exception as e:  # noqa: BLE001 — surface the API error
                await interaction.followup.send(f"Refresh failed: `{e}`", ephemeral=True)
                return
            if interaction.message:
                await interaction.message.edit(
                    embed=status_embed(
                        session, fallback_title=binding.title if binding else None
                    )
                )
            if binding:
                binding.status = session.status
                binding.status_detail = session.status_detail
                await self.db.upsert_binding(binding)
        elif action == "approve":
            await interaction.response.defer(ephemeral=True)
            try:
                await self.devin.send_message(
                    session_id,
                    "Approved — please proceed.",
                    message_as_user_id=self.settings.create_as_user_id,
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"Approve failed: `{e}`", ephemeral=True)
                return
            await interaction.followup.send("Sent an approval to the session.", ephemeral=True)
        elif action == "post_review":
            if self.github is None or not extra:
                await interaction.response.send_message(
                    "GitHub App isn't configured (GITHUB_APP_*).", ephemeral=True
                )
                return
            ref_parsed = parse_issue_ref(extra)
            if ref_parsed is None:
                await interaction.response.send_message(
                    f"Bad PR reference {extra!r}.", ephemeral=True
                )
                return
            o, r, n = ref_parsed
            await interaction.response.defer(ephemeral=True)
            try:
                sess = await self.devin.get_session(session_id)
                so = sess.structured_output or {}
                body = (
                    (binding.last_msg if binding else None)
                    or so.get("summary")
                    or "(review completed — see the session for details)"
                )
                await self.github.create_pr_review(
                    PullRef(owner=o, repo=r, number=n),
                    f"**Devin review** ({sess.url or session_id}):\n\n{body}",
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(
                    f"Posting failed: `{e}`", ephemeral=True
                )
                return
            await interaction.followup.send(
                f"Posted the review as a comment on {extra}.", ephemeral=True
            )
        elif action == "choose":
            await self._handle_choice(interaction, session_id, extra, binding)
        elif action == "chain_next":
            if (
                binding is None
                or not binding.chain
                or binding.chain.get("pending") is None
            ):
                await interaction.response.send_message(
                    "This chain already moved on.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            # The check above is only a fast path — two taps can both pass
            # it before pending clears. Take the per-session poll lock and
            # re-read so the loser sees the consumed marker and stops.
            async with self.relay._lock(session_id):
                binding = await self.db.get_binding(session_id)
                if (
                    binding is None
                    or not binding.chain
                    or binding.chain.get("pending") is None
                ):
                    await interaction.followup.send(
                        "This chain already moved on.", ephemeral=True
                    )
                    return
                try:
                    session = await self.devin.get_session(session_id)
                except Exception as e:  # noqa: BLE001
                    await interaction.followup.send(f"`{e}`", ephemeral=True)
                    return
                try:
                    spawned = await self.relay._advance_chain(
                        binding, session, force=True
                    )
                except Exception as e:  # noqa: BLE001
                    await interaction.followup.send(
                        f"Advance failed: `{e}`", ephemeral=True
                    )
                    return
            # swap the Continue→ button out of the card — chain moved on
            if interaction.message:
                review_ok = bool(
                    binding.review_of and self.github is not None
                )
                try:
                    await interaction.message.edit(
                        view=(
                            CompletionView(
                                session_id,
                                self.handle_component,
                                review_of=binding.review_of if review_ok else "",
                            )
                            if review_ok
                            else None
                        )
                    )
                except discord.HTTPException:
                    pass
            await interaction.followup.send(
                (
                    f"Phase spawned → {spawned.mention}"
                    if spawned is not None
                    else "Chain advanced."
                ),
                ephemeral=True,
            )

    async def _handle_choice(
        self,
        interaction: discord.Interaction,
        session_id: str,
        idx: str | None,
        binding: Binding | None,
    ) -> None:
        """Choice-button tap on a Devin question.

        The option text is stored on the button's own label — the message
        carries the state, so post-restart clicks resolve identically. The
        label goes to Devin verbatim ("2. the caddy config"): it reads as
        the numbered answer the question asked for.
        """
        answer = None
        cid = str(cast(dict[str, Any], interaction.data or {}).get("custom_id") or "")
        if interaction.message:
            for row in interaction.message.components:
                for comp in getattr(row, "children", []):
                    if getattr(comp, "custom_id", None) == cid:
                        answer = getattr(comp, "label", None) or None
        if answer is None:
            # label gone — the bare index still answers a numbered list
            answer = idx
        if not answer:
            await interaction.response.send_message(
                "Couldn't recover which option that was.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await self.devin.send_message(
                session_id,
                answer,
                message_as_user_id=self.settings.create_as_user_id,
            )
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                f"Couldn't send: `{e}`", ephemeral=True
            )
            return  # view stays in place — the tap can be retried
        # The tap is its own confirmation: buttons come off so a second tap
        # can't double-send, and the transcript records the pick.
        if interaction.message:
            note = f" *(answered: {answer})*"
            content = interaction.message.content or ""
            try:
                await interaction.message.edit(
                    content=(
                        content + note
                        if len(content) + len(note) <= 2000
                        else discord.utils.MISSING
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass
        if binding is not None:
            # same fast turnaround as typed steering
            await self.relay.progress.thinking(binding)
            self.relay.request_poll(binding)

    async def _handle_pr_action(
        self,
        interaction: discord.Interaction,
        action: str,
        session_id: str,
        pr_key: str | None,
    ) -> None:
        if self.github is None or not pr_key:
            await interaction.response.send_message(
                "GitHub App isn't configured (GITHUB_APP_*).", ephemeral=True
            )
            return
        ref_parsed = parse_issue_ref(pr_key)
        if ref_parsed is None:
            await interaction.response.send_message(
                f"Bad PR reference {pr_key!r}.", ephemeral=True
            )
            return
        o, r, n = ref_parsed
        row = await self.db.get_pr_by_ref(session_id, o, r, n)
        if row is None:
            await interaction.response.send_message(
                f"No PR {pr_key} tracked for this session.", ephemeral=True
            )
            return
        ref = PullRef(owner=row.owner, repo=row.repo, number=row.number)

        if action == "pr_merge":
            # first click just asks for confirmation — the real merge happens
            # on pr_merge_go below
            await interaction.response.send_message(
                f"Merge **{row.pr_title or row.pr_url}** "
                f"({self.settings.github_merge_method})?",
                view=MergeConfirmView(session_id, pr_key, self.handle_component),
                ephemeral=True,
            )
            return
        if action == "pr_automerge":
            row.auto_merge = not row.auto_merge
            await self.db.upsert_pr(row)
            prod = await self.db.get_binding(session_id)
            if row.auto_merge and prod is not None and not prod.active:
                # armed on a dead session — resume polling so the merge can
                # fire when CI greens (the poll's armed-PR check sustains it)
                prod.active = True
                await self.db.upsert_binding(prod)
            state = "ON — merges when CI goes green" if row.auto_merge else "off"
            if interaction.message and prod is not None:
                try:
                    await self.relay._refresh_pr_card(prod, row)
                except Exception:  # noqa: BLE001
                    pass
            await interaction.response.send_message(
                f"Auto-merge {state} for {ref.key}.", ephemeral=True
            )
            return
        if action == "fix_ci":
            await interaction.response.defer(ephemeral=True)
            try:
                fails = await self.github.get_failed_checks(ref)
                lines = "\n".join(
                    f"- {f['name']}: {f['conclusion']} ({f['url']})"
                    for f in fails[:10]
                ) or "- (no failed check runs found — inspect the PR)"
                await self.devin.send_message(
                    session_id,
                    f"CI failed on {ref.key} — please investigate and fix:\n{lines}",
                    message_as_user_id=self.settings.create_as_user_id,
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"Failed: `{e}`", ephemeral=True)
                return
            await interaction.followup.send(
                "Sent the failing checks to Devin.", ephemeral=True
            )
            return
        if action == "send_review":
            await interaction.response.defer(ephemeral=True)
            try:
                feedback = await self.github.get_review_feedback(ref)
                lines = "\n".join(
                    f"- {f['author']}"
                    + (f" on `{f['path']}`" if f.get("path") else "")
                    + (f" [{f['state']}]" if f.get("state") else "")
                    + f": {f['body'][:400]}"
                    for f in feedback
                ) or "- (no review comments found)"
                await self.devin.send_message(
                    session_id,
                    f"Review feedback on {ref.key} — please address it:\n{lines}",
                    message_as_user_id=self.settings.create_as_user_id,
                )
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"Failed: `{e}`", ephemeral=True)
                return
            await interaction.followup.send(
                "Sent the review feedback to Devin.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if action == "pr_merge_go":
                await self.github.merge_pr(ref, method=self.settings.github_merge_method)
                row.state = "merged"
                msg = f"Merged {ref.key}."
            elif action == "pr_approve":
                await self.github.approve_pr(ref)
                msg = f"Approved {ref.key}."
            elif action == "pr_close":
                await self.github.close_pr(ref)
                row.state = "closed"
                msg = f"Closed {ref.key}."
            else:
                return
            await self.db.upsert_pr(row)
            if interaction.message:
                await interaction.message.edit(view=None)  # drop stale buttons
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:  # noqa: BLE001 — show GitHub's reason
            await interaction.followup.send(f"`{e}`", ephemeral=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    if not settings.devin_api_key or not settings.devin_org_id:
        raise SystemExit("DEVIN_API_KEY / DEVIN_ORG_ID are not set (see .env.example)")
    if not settings.allowed_user_id_set:
        raise SystemExit("ALLOWED_USER_IDS is empty — refusing to start unlocked")
    DevinMobileBot(settings).run(settings.discord_token)


if __name__ == "__main__":
    main()
