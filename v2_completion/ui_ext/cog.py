from typing import TYPE_CHECKING
import enum
import discord
from discord import app_commands
from discord.ext import commands
from asgiref.sync import sync_to_async
from ballsdex.core.utils.sorting import FilteringChoices, SortingChoices, filter_balls, sort_balls
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallGroupTransform,
    BallInstanceTransform,
    EconomyTransform,
    RegimeTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.core.utils.utils import inventory_privacy, is_staff
from bd_models.models import BallInstance, Player, balls, groups
from settings.models import settings

from .paginator import FieldPageSource, Pages
from ballsdex.core.bot import BallsDexBot
if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
from django.db.models import Count, Exists, F, OuterRef, Q

class DuplicateType(enum.StrEnum):
    countryballs = settings.plural_collectible_name
    specials = "specials"

class V2UI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    async def completion(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        special: SpecialEnabledTransform | None = None,
        filter: FilteringChoices | None = None,
        regime: RegimeTransform | None = None,
        economy: EconomyTransform | None = None,
        group: BallGroupTransform | None = None,
        duplicates: bool = False,
        ephemeral: bool = False,
    ):
        """
        Show your current completion of the BallsDex.

        Parameters
        ----------
        user: discord.User
            The user whose completion you want to view, if not yours.
        special: Special
            The special you want to see the completion of
        filter: FilteringChoices
            Filter the list by a specific filter.
        regime: Regime
            The regime you want to see the completion of
        economy: Economy
            The economy you want to see the completion of
        group: BallGroup
            The group you want to see the completion of
        duplicates: bool
            Show the completion of duplicates.
        ephemeral: bool
            Whether or not to send the command ephemerally.
        """
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)
        extra_text = f"{special.name} " if special else ""
        if regime:
            extra_text += f"{regime.name} "
        if economy:
            extra_text += f"{economy.name} "
        if group:
            extra_text += f"{group.name} "
        if user is not None:
            try:
                player = await Player.objects.aget(discord_id=user_obj.id)
            except Player.DoesNotExist:
                await interaction.followup.send(
                    f"{user_obj.name} doesn't have any {extra_text}{settings.plural_collectible_name} yet."
                )
                return
            staff = await is_staff(interaction)
            if user.id in self.bot.blacklist and not staff:
                await interaction.followup.send("You cannot view the completion of a blacklisted user.", ephemeral=True)
                return

            interaction_player, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)

            blocked = await player.is_blocked(interaction_player)
            if blocked and not staff:
                await interaction.followup.send(
                    "You cannot view the completion of a user that has blocked you.", ephemeral=True
                )
                return

            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return
        # Filter disabled balls, they do not count towards progression
        # Only ID and emoji is interesting for us
        bot_countryballs = {x: y.emoji_id for x, y in balls.items() if y.enabled}

        # Set of ball IDs owned by the player
        filters = {"player__discord_id": user_obj.id, "ball__enabled": True}
        if special:
            filters["special"] = special
            bot_countryballs = {
                x: y.emoji_id
                for x, y in balls.items()
                if y.enabled and (special.end_date is None or y.created_at is None or y.created_at < special.end_date)
            }

        if regime:
            filters["ball__regime"] = regime
            bot_countryballs = {x: y for x, y in bot_countryballs.items() if balls[x].regime_id == regime.pk}

        if economy:
            filters["ball__economy"] = economy
            bot_countryballs = {x: y for x, y in bot_countryballs.items() if balls[x].economy_id == economy.pk}

        if group:
            filters["ball__groups"] = group
            group_ball_ids = {ball.pk for ball in groups[group.pk].balls} if group.pk in groups else set()
            bot_countryballs = {x: y for x, y in bot_countryballs.items() if x in group_ball_ids}

        if filter:
            query = filter_balls(filter, BallInstance.objects.filter(**filters), interaction.guild_id)
        else:
            query = BallInstance.objects.filter(**filters)

        if not bot_countryballs:
            await interaction.followup.send(
                f"There are no {extra_text}{settings.plural_collectible_name} registered on this bot yet.",
                ephemeral=True,
            )
            return

        if duplicates:
            query = query.values("ball_id").annotate(count=Count("ball_id")).filter(count__gt=1)

        owned_countryballs = set(
            [
                x[0]
                async for x in query.filter(**filters)
                .distinct()  # Do not query everything
                .values_list("ball_id")
            ]
        )
        entries: list[tuple[str, str]] = []

        def fill_fields(title: str, emoji_ids: set[int]):
            # check if we need to add "(continued)" to the field name
            first_field_added = False
            buffer = ""

            for emoji_id in emoji_ids:
                emoji = self.bot.get_emoji(emoji_id)
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        entries.append(("\u200b", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    entries.append(("\u200b", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if owned_countryballs:
            # Getting the list of emoji IDs from the IDs of the owned countryballs
            fill_fields(
                f"Owned {settings.plural_collectible_name}",
                set(bot_countryballs[x] for x in owned_countryballs),
            )
        else:
            entries.append((f"__**Owned {settings.plural_collectible_name}**__", "Nothing yet."))

        if missing := set(y for x, y in bot_countryballs.items() if x not in owned_countryballs):
            fill_fields(f"Missing {settings.plural_collectible_name}", missing)
        else:
            entries.append(
                (
                    f"__**:tada: No missing {settings.plural_collectible_name}, "
                    "congratulations! :tada:**__",
                    "\u200b",
                )
            )  # force empty field value

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)

        special_str = f" ({special.name})" if special else ""
        regime_str = f" ({regime.name})" if regime else ""
        economy_str = f" ({economy.name})" if economy else ""
        group_str = f" ({group.name})" if group else ""
        original_catcher_string = " " + filter.value.replace("_", " ") + " " if filter else ""
        duplicates_str = " duplicates" if duplicates else ""
        source.embed.description = (
            f"{settings.bot_name}{original_catcher_string}{special_str}{regime_str}{economy_str}{group_str}{duplicates_str} progression: "
            f"**{round(len(owned_countryballs) / len(bot_countryballs) * 100, 1)}%**"
        )
        source.embed.colour = discord.Colour.blurple()
        source.embed.set_author(name=user_obj.display_name, icon_url=user_obj.display_avatar.url)

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()

    async def cog_load(self):
        await self.bot.wait_until_ready()

        group = self.bot.tree.get_command(settings.balls_slash_name)

        if group and isinstance(group, app_commands.Group):
            group.remove_command("completion")
            group.add_command(app_commands.Command(
                name="completion",
                callback=self.completion,
                description=f"Show your current completion of the {settings.bot_name}"
            ))

        await self.bot.tree.sync()