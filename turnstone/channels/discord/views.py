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


def _parse_footer(interaction: discord.Interaction) -> tuple[str, str] | None:
    """Extract ``(ws_id, correlation_id)`` from the first embed's footer."""
    if not interaction.message or not interaction.message.embeds:
        return None
    footer = interaction.message.embeds[0].footer.text
    if not footer or "|" not in footer:
        return None
    parts = footer.split("|", 1)
    return parts[0], parts[1]


async def _disable_buttons(interaction: discord.Interaction, label: str) -> None:
    """Edit the message to disable all buttons and append a result label."""
    import discord

    if interaction.message is None:
        return

    view = discord.ui.View()
    for item in interaction.message.components or []:
        for child in item.children:  # type: ignore[union-attr]
            button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
                label=getattr(child, "label", ""),
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id=getattr(child, "custom_id", None),
            )
            view.add_item(button)

    embed = interaction.message.embeds[0] if interaction.message.embeds else None
    if embed is not None:
        embed.color = discord.Color.greyple()
        embed.title = f"{embed.title} - {label}"

    await interaction.message.edit(embed=embed, view=view)


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

        ws_id, correlation_id = parsed

        # Verify user is linked.  Scope enforcement (approve) happens
        # server-side when the tool approval is executed.
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

        ws_id, correlation_id = parsed

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

        ws_id, correlation_id = parsed
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
