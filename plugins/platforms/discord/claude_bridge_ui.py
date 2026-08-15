"""Discord UI for the Claude Code CLI bridge's escalation-decision outbox.

``DiscordAdapter.post_claude_bridge_decision`` and its button ``View`` live
here, not in ``adapter.py`` — see ``gateway/claude_bridge/__init__.py`` for
the fork-maintenance rule this follows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

# Matches plugins.platforms.discord.adapter's own logger name: these classes
# and functions moved out of that module, and their logs belong under the
# same name for continuity with existing log filtering/dashboards.
logger = logging.getLogger("plugins.platforms.discord.adapter")


def define_decision_outbox_view() -> type:
    """Build the ``DecisionOutboxView`` class.

    A function (not a module-level class) for the same reason
    ``plugins.platforms.discord.adapter._define_discord_view_classes``
    defines its sibling views this way: ``discord.ui.View`` subclassing
    requires ``discord`` to be importable, which is only guaranteed once
    Discord support has been installed/detected — importing it eagerly at
    module load would break on installs without the optional dependency.
    """
    import discord

    from plugins.platforms.discord.adapter import (
        _DISCORD_BUTTON_LABEL_LIMIT,
        _component_check_auth,
    )

    class DecisionOutboxView(discord.ui.View):
        """Buttons for a claude_bridge escalation decision.

        Deliberately does not resolve anything in-process on click — it only
        writes the answer to ``claude_bridge/decisions/<id>.json`` via
        ``gateway.claude_bridge.write_decision_answer`` for a later resume to
        pick up (see that module's docstring: no ``threading.Event`` /
        in-process wait, file IPC only). Auth gating mirrors
        ``ExecApprovalView``: the decision *content* comes from the bridge's
        own working directory, but the *answer* steers a running Claude
        session, so the clicker must pass the same user/role/pairing
        allowlist as every other component view.
        """

        def __init__(
            self,
            decision_id: str,
            options: list,
            recommended_id: str | None,
            timeout_seconds: float | None,
            allowed_user_ids: set | None = None,
            allowed_role_ids: set | None = None,
        ):
            has_timeout = isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0
            super().__init__(timeout=float(timeout_seconds) if has_timeout else None)
            self.decision_id = decision_id
            self.allowed_user_ids = allowed_user_ids or set()
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False

            for opt in options[:25]:
                opt_id = str(opt.get("id", "")).strip()
                if not opt_id:
                    continue
                label = str(opt.get("label") or opt_id)[:_DISCORD_BUTTON_LABEL_LIMIT]
                is_recommended = recommended_id is not None and opt_id == recommended_id
                button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary if is_recommended else discord.ButtonStyle.secondary,
                    custom_id=f"claude_bridge_decision:{decision_id}:{opt_id}",
                )
                button.callback = self._make_callback(opt_id, label)
                self.add_item(button)

        def _make_callback(self, opt_id: str, label: str):
            async def _callback(interaction: "discord.Interaction"):
                await self._answer(interaction, opt_id, label)
            return _callback

        async def _answer(
            self, interaction: "discord.Interaction", opt_id: str, label: str,
        ) -> None:
            if not _component_check_auth(
                interaction, self.allowed_user_ids, self.allowed_role_ids,
            ):
                await interaction.response.send_message(
                    "You're not authorized to answer this decision~", ephemeral=True,
                )
                return
            if self.resolved:
                await interaction.response.send_message(
                    "This decision has already been answered~", ephemeral=True,
                )
                return

            user = getattr(interaction, "user", None)
            display_name = getattr(user, "display_name", "user")
            user_id = getattr(user, "id", None)

            # Persist BEFORE flipping resolved/disabling buttons: if the write
            # fails, the message must not look answered while
            # claude_bridge/decisions/ has nothing recorded — that would be
            # unrecoverable (resolved=True blocks every future click). Leave
            # the buttons live so the click can be retried.
            try:
                from gateway.claude_bridge import write_decision_answer
                write_decision_answer(
                    self.decision_id, opt_id, str(user_id) if user_id else None,
                )
            except Exception:
                logger.exception(
                    "claude_bridge: failed to write decision answer for %s",
                    self.decision_id,
                )
                embed = interaction.message.embeds[0] if (
                    interaction.message and interaction.message.embeds
                ) else None
                if embed:
                    embed.color = discord.Color.red()
                    embed.set_footer(text="⚠ Failed to record your answer — please try again")
                try:
                    await interaction.response.edit_message(embed=embed, view=self)
                except Exception:
                    try:
                        await interaction.response.defer()
                    except Exception:
                        pass
                return

            self.resolved = True
            for child in self.children:
                child.disabled = True

            embed = interaction.message.embeds[0] if (
                interaction.message and interaction.message.embeds
            ) else None
            if embed:
                embed.color = discord.Color.green()
                embed.set_footer(text=f"Answered by {display_name}: {label}")

            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True
            msg = getattr(self, "_message", None)
            if msg:
                try:
                    embed = msg.embeds[0] if msg.embeds else None
                    if embed:
                        embed.color = discord.Color.greyple()
                        embed.set_footer(text="⏱ Decision expired — no response recorded")
                    await msg.edit(embed=embed, view=self)
                except Exception:
                    pass
            try:
                from gateway.claude_bridge import write_decision_timeout
                write_decision_timeout(self.decision_id)
            except Exception:
                logger.exception(
                    "claude_bridge: failed to write decision timeout for %s",
                    self.decision_id,
                )

    return DecisionOutboxView


async def post_claude_bridge_decision(adapter: Any, decision: Dict[str, Any]) -> None:
    """Post a claude_bridge escalation decision as a button prompt.

    Called by ``gateway.claude_bridge.OutboxWatcher`` for each
    ``outbox/*.json`` file matching the ``{decision_id, channel_id,
    question, options, recommended?, timeout_seconds?}`` schema. Raising
    here tells the watcher to move the source file to ``outbox/failed/``
    instead of ``outbox/processed/``.
    """
    from plugins.platforms.discord.adapter import DISCORD_AVAILABLE

    # Guard before importing discord/DecisionOutboxView: with the optional
    # discord dependency missing, those imports fail and would replace the
    # intended domain error with a confusing ImportError.
    if not adapter._client or not DISCORD_AVAILABLE:
        raise RuntimeError("Discord adapter not connected")

    import discord

    from plugins.platforms.discord.adapter import DecisionOutboxView

    decision_id = str(decision["decision_id"])
    channel_id = str(decision["channel_id"])
    channel = adapter._client.get_channel(int(channel_id))
    if not channel:
        channel = await adapter._client.fetch_channel(int(channel_id))

    question = str(decision.get("question") or "").strip()
    max_desc = 4088
    if len(question) > max_desc:
        question = question[: max_desc - 3] + "..."

    recommended = decision.get("recommended")
    options = [o for o in (decision.get("options") or []) if isinstance(o, dict) and o.get("id")]
    # Recommended option first, so its (primary-styled) button leads.
    options.sort(key=lambda o: 0 if o.get("id") == recommended else 1)

    embed = discord.Embed(
        title="🔀 Claude bridge needs a decision",
        description=question,
        color=discord.Color.blue(),
    )
    if options:
        lines = []
        for opt in options:
            marker = " ⭐ recommended" if recommended and opt.get("id") == recommended else ""
            detail = f" — {opt['detail']}" if opt.get("detail") else ""
            lines.append(f"**{opt.get('label') or opt['id']}**{marker}{detail}")
        embed.add_field(name="Options", value="\n".join(lines)[:1024], inline=False)

    view = DecisionOutboxView(
        decision_id=decision_id,
        options=options,
        recommended_id=str(recommended) if recommended else None,
        timeout_seconds=decision.get("timeout_seconds"),
        allowed_user_ids=adapter._allowed_user_ids,
        allowed_role_ids=adapter._allowed_role_ids,
    )
    msg = await channel.send(embed=embed, view=view)
    view._message = msg  # store for on_timeout expiration editing
