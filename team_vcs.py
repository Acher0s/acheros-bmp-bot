"""One private voice channel per team, that only that team's members can see or join.

Locked down with permission overwrites, so nobody outside the team can see the
channel at all: not who's in it, not its stream/camera previews, not its text chat.
  * @everyone is denied View Channel (plus Connect, chat and invites as backup).
    A channel overwrite on @everyone also cancels any server-wide role that would
    otherwise grant viewing, so other players' roles can't get them in.
  * The team's role is the only role allowed in.
  * The bot only gets View Channel, so it can repair the channel later.
The category is private too, so a channel accidentally re-synced to its category
stays hidden.

Discord limits: members with the Administrator permission (and the server owner)
bypass every channel permission and can always see these channels; so can anyone
whose server-wide role has Manage Channels/Manage Roles, since they could edit the
overwrites. Keep those permissions to organizers.

Restart safety: the channel id lives on Team.vc_id. If the bot died after creating a
channel but before saving, the channel is found again by name in the category and
reused. Running it again also resets the permissions, undoing manual edits.

The bot needs Manage Channels and Manage Roles (to set channel permissions), plus
View Channel, Connect, Speak, Video, Send Messages and Read Message History,
since Discord only lets it hand out permissions it has itself.
"""
import asyncio

import discord

from models.tournament import Team

CATEGORY = "Conjoined Team VCs"

_locks: dict[int, asyncio.Lock] = {}


def _lock(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


def channel_name(team: Team) -> str:
    return team.name[:100]


# Everyone outside the team: can't see the channel, so can't see members, streams,
# previews or chat. The extra denies only matter if View Channel is ever re-granted.
_OUTSIDERS = discord.PermissionOverwrite(
    view_channel=False, connect=False, read_message_history=False, send_messages=False,
    create_instant_invite=False,
)
_TEAM = discord.PermissionOverwrite(
    view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True,
    send_messages=True, read_message_history=True, embed_links=True, attach_files=True, add_reactions=True,
    create_instant_invite=False,
)
_BOT = discord.PermissionOverwrite(view_channel=True)


def overwrites(guild: discord.Guild, role: discord.Role) -> dict:
    return {guild.default_role: _OUTSIDERS, guild.me: _BOT, role: _TEAM}


async def _category(guild: discord.Guild) -> discord.CategoryChannel:
    private = {guild.default_role: discord.PermissionOverwrite(view_channel=False), guild.me: _BOT}
    category = discord.utils.get(guild.categories, name=CATEGORY)
    if category is None:
        return await guild.create_category(CATEGORY, overwrites=private, reason="Conjoined team voice channels")
    if category.overwrites != private:
        await category.edit(overwrites=private, reason="Keep team voice channels private")
    return category


def _existing(guild: discord.Guild, team: Team, category: discord.CategoryChannel):
    channel = guild.get_channel(team.vc_id) if team.vc_id else None
    if isinstance(channel, discord.VoiceChannel):
        return channel
    return discord.utils.get(category.voice_channels, name=channel_name(team))


async def sync_team_vc(guild: discord.Guild, team: Team, role: discord.Role) -> tuple[discord.VoiceChannel, bool]:
    """Create the team's voice channel, or reset an existing one to the locked-down permissions.

    Returns (channel, created). The caller must store channel.id on the team and save.
    """
    async with _lock(guild.id):
        category = await _category(guild)
        wanted = overwrites(guild, role)
        channel = _existing(guild, team, category)
        if channel is None:
            channel = await guild.create_voice_channel(
                channel_name(team), category=category, overwrites=wanted,
                reason=f"Private voice channel for {team.name}")
            return channel, True
        if channel.overwrites != wanted or channel.category != category or channel.name != channel_name(team):
            await channel.edit(name=channel_name(team), category=category, overwrites=wanted,
                               sync_permissions=False, reason=f"Reset {team.name}'s voice channel permissions")
        return channel, False
