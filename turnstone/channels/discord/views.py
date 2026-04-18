"""Persistent interactive views for Discord approval and plan review.

These views use static ``custom_id`` values so they survive bot restarts.
Correlation information (``ws_id`` and ``correlation_id``) is stored in the
embed footer of the message the view is attached to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    import discord

    from turnstone.channels.discord.bot import TurnstoneBot

log = get_logger(__name__)


def _parse_footer(interaction: discord.Interaction) -> tuple[str, str, str] | None:
    """Extract ``(ws_id, correlation_id, owner_id)`` from the first embed's footer.

    Footer format is ``"{ws_id}|{correlation_id}|{owner_id}"``.  Older
    posts that pre-date the owner-check upgrade may have only two
    fields; in that case ``owner_id`` is returned as an empty string
    and the caller rejects the interaction (fail-closed).
    """
    if not interaction.message or not interaction.message.embeds:
        return None
    footer = interaction.message.embeds[0].footer.text
    if not footer or "|" not in footer:
        return None
    parts = footer.split("|", 2)
    ws_id = parts[0]
    correlation_id = parts[1] if len(parts) > 1 else ""
    owner_id = parts[2] if len(parts) > 2 else ""
    return ws_id, correlation_id, owner_id


async def _deny_non_owner(interaction: discord.Interaction, verb: str) -> None:
    """Reply with an ephemeral non-owner rejection."""
    await interaction.response.send_message(
        f"Only the session owner can {verb} this.",
        ephemeral=True,
    )


async def disable_message_buttons(message: discord.Message, label: str) -> None:
    """Disable all buttons on *message* and append *label* to the embed title.

    Used both from interaction callbacks (via the message attribute) and
    from bot event handlers when the server resolves an approval externally
    (e.g. timeout).
    """
    import discord

    view = discord.ui.View()
    for item in message.components or []:
        for child in item.children:  # type: ignore[union-attr]
            button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
                label=getattr(child, "label", ""),
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id=getattr(child, "custom_id", None),
            )
            view.add_item(button)

    embed = message.embeds[0] if message.embeds else None
    if embed is not None:
        embed.color = discord.Color.greyple()
        embed.title = f"{embed.title} - {label}"

    await message.edit(embed=embed, view=view)


async def _disable_buttons(interaction: discord.Interaction, label: str) -> None:
    """Edit the interaction message to disable all buttons and append *label* to the embed title."""
    if interaction.message is None:
        return
    await disable_message_buttons(interaction.message, label)


# ---------------------------------------------------------------------------
# ApprovalView
# ---------------------------------------------------------------------------


class ApprovalView:
    """Persistent view with Approve / Reject / Always Approve buttons."""

    def __init__(self, bot: TurnstoneBot) -> None:
        import discord

        self.bot = bot
        view_self = self

        class _View(discord.ui.View):
            def __init__(inner_self) -> None:  # noqa: N805
                super().__init__(timeout=None)

            @discord.ui.button(
                label="Approve",
                style=discord.ButtonStyle.green,
                custom_id="ts:approve",
            )
            async def approve(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle(interaction, approved=True, always=False)

            @discord.ui.button(
                label="Reject",
                style=discord.ButtonStyle.red,
                custom_id="ts:reject",
            )
            async def reject(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle(interaction, approved=False, always=False)

            @discord.ui.button(
                label="Always Approve",
                style=discord.ButtonStyle.secondary,
                custom_id="ts:always",
            )
            async def always_approve(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle(interaction, approved=True, always=True)

        self._view = _View()

    async def _handle(
        self,
        interaction: discord.Interaction,
        *,
        approved: bool,
        always: bool,
    ) -> None:
        """Process an approval button click."""
        parsed = _parse_footer(interaction)
        if parsed is None:
            await interaction.response.send_message(
                "Could not determine workstream context.",
                ephemeral=True,
            )
            return

        ws_id, correlation_id, owner_id = parsed

        # Owner check: the gateway forwards approvals using its own
        # service-scoped JWT, and the server short-circuits scope checks
        # for service tokens — so the adapter is the only place this
        # can be enforced.  Reject any other clicker, including linked
        # users, with an ephemeral message.
        if not owner_id or str(interaction.user.id) != owner_id:
            verb = "always-approve" if always else ("approve" if approved else "reject")
            await _deny_non_owner(interaction, verb)
            return

        # Verify user is linked (session owner should already be linked).
        user_id = await self.bot.router.resolve_user("discord", str(interaction.user.id))
        if user_id is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first.",
                ephemeral=True,
            )
            return

        # Defer before doing async work so Discord doesn't time out.
        await interaction.response.defer(ephemeral=True)

        await self.bot.router.send_approval(
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
            always=always,
        )

        label = "Always Approved" if always else ("Approved" if approved else "Rejected")
        # Pop pending approval so ApprovalResolvedEvent doesn't double-update.
        self.bot._pending_approval_msgs.pop(ws_id, None)
        await _disable_buttons(interaction, label)
        await interaction.followup.send(
            f"Tool execution **{label.lower()}**.",
            ephemeral=True,
        )
        log.info(
            "discord.approval_response",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
            always=always,
        )


# ---------------------------------------------------------------------------
# PlanReviewView
# ---------------------------------------------------------------------------


class PlanReviewView:
    """Persistent view with Approve Plan / Request Changes buttons."""

    def __init__(self, bot: TurnstoneBot) -> None:
        import discord

        self.bot = bot
        view_self = self

        class _FeedbackModal(discord.ui.Modal, title="Request Changes"):  # type: ignore[call-arg]
            feedback: discord.ui.TextInput[_FeedbackModal] = discord.ui.TextInput(
                label="Feedback",
                style=discord.TextStyle.paragraph,
                placeholder="Describe the changes you'd like...",
                required=True,
                max_length=2000,
            )

            def __init__(modal_self, ws_id: str, correlation_id: str) -> None:  # noqa: N805
                super().__init__()
                modal_self.ws_id = ws_id
                modal_self.correlation_id = correlation_id

            async def on_submit(modal_self, interaction: discord.Interaction) -> None:  # noqa: N805
                await view_self._send_feedback(
                    interaction,
                    modal_self.ws_id,
                    modal_self.correlation_id,
                    str(modal_self.feedback),
                )

        self._modal_cls = _FeedbackModal

        class _View(discord.ui.View):
            def __init__(inner_self) -> None:  # noqa: N805
                super().__init__(timeout=None)

            @discord.ui.button(
                label="Approve Plan",
                style=discord.ButtonStyle.green,
                custom_id="ts:plan_approve",
            )
            async def approve_plan(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle_approve(interaction)

            @discord.ui.button(
                label="Request Changes",
                style=discord.ButtonStyle.secondary,
                custom_id="ts:plan_changes",
            )
            async def request_changes(
                inner_self,  # noqa: N805
                interaction: discord.Interaction,
                button: discord.ui.Button[_View],
            ) -> None:
                await view_self._handle_changes(interaction)

        self._view = _View()

    async def _handle_approve(self, interaction: discord.Interaction) -> None:
        """Approve the plan (empty feedback = approved)."""
        parsed = _parse_footer(interaction)
        if parsed is None:
            await interaction.response.send_message(
                "Could not determine workstream context.",
                ephemeral=True,
            )
            return

        ws_id, correlation_id, owner_id = parsed

        if not owner_id or str(interaction.user.id) != owner_id:
            await _deny_non_owner(interaction, "approve")
            return

        user_id = await self.bot.router.resolve_user("discord", str(interaction.user.id))
        if user_id is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first.",
                ephemeral=True,
            )
            return

        # Defer before doing async work so Discord doesn't time out.
        await interaction.response.defer(ephemeral=True)

        await self.bot.router.send_plan_feedback(
            ws_id=ws_id,
            correlation_id=correlation_id,
            feedback="",
        )

        await _disable_buttons(interaction, "Approved")
        await interaction.followup.send("Plan **approved**.", ephemeral=True)
        log.info(
            "discord.plan_approved",
            ws_id=ws_id,
            correlation_id=correlation_id,
        )

    async def _handle_changes(self, interaction: discord.Interaction) -> None:
        """Open a modal for feedback text."""
        parsed = _parse_footer(interaction)
        if parsed is None:
            await interaction.response.send_message(
                "Could not determine workstream context.",
                ephemeral=True,
            )
            return

        ws_id, correlation_id, owner_id = parsed

        if not owner_id or str(interaction.user.id) != owner_id:
            await _deny_non_owner(interaction, "request changes on")
            return

        modal = self._modal_cls(ws_id, correlation_id)
        await interaction.response.send_modal(modal)

    async def _send_feedback(
        self,
        interaction: discord.Interaction,
        ws_id: str,
        correlation_id: str,
        feedback: str,
    ) -> None:
        """Send plan feedback after the modal is submitted."""
        user_id = await self.bot.router.resolve_user("discord", str(interaction.user.id))
        if user_id is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first.",
                ephemeral=True,
            )
            return

        # Defer before doing async work so Discord doesn't time out.
        await interaction.response.defer(ephemeral=True)

        await self.bot.router.send_plan_feedback(
            ws_id=ws_id,
            correlation_id=correlation_id,
            feedback=feedback,
        )

        await interaction.followup.send(
            "Feedback submitted. The plan will be revised.",
            ephemeral=True,
        )
        log.info(
            "discord.plan_changes_requested",
            ws_id=ws_id,
            correlation_id=correlation_id,
        )
