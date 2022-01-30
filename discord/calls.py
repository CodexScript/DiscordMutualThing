# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import copy
import datetime

from . import utils
from .enums import VoiceRegion, try_enum
from .errors import ClientException

def _running_only(func):
    def decorator(self, *args, **kwargs):
        if self._ended:
            raise ClientException('Call is over')
        else:
            return func(self, *args, **kwargs)
    return decorator


class CallMessage:
    """Represents a group call message from Discord.

    This is only received in cases where the message type is equivalent to
    :attr:`MessageType.call`.

    Attributes
    -----------
    ended_timestamp: Optional[:class:`datetime.datetime`]
        A naive UTC datetime object that represents the time that the call has ended.
    participants: List[:class:`User`]
        A list of users that participated in the call.
    message: :class:`Message`
        The message associated with this call message.
    """

    def __init__(self, message, **kwargs):
        self.message = message
        self.ended_timestamp = utils.parse_time(kwargs.get('ended_timestamp'))
        self.participants = kwargs.get('participants')

    @property
    def call_ended(self):
        """:class:`bool`: Indicates if the call has ended."""
        return self.ended_timestamp is not None

    @property
    def initiator(self):
        """:class:`User`: Returns the user that started the call."""
        return self.message.author

    @property
    def channel(self):
        r""":class:`GroupChannel`\: The private channel associated with this message."""
        return self.message.channel

    @property
    def duration(self):
        """Queries the duration of the call.

        If the call has not ended then the current duration will
        be returned.

        Returns
        ---------
        :class:`datetime.timedelta`
            The timedelta object representing the duration.
        """
        if self.ended_timestamp is None:
            return datetime.datetime.utcnow() - self.message.created_at
        else:
            return self.ended_timestamp - self.message.created_at


class PrivateCall:
    """Represents the actual group call from Discord.

    This is accompanied with a :class:`CallMessage` denoting the information.

    Attributes
    -----------
    channel: :class:`GroupChannel`
        The channel the group call is in.
    message: Optional[:class:`Message`]
        The message associated with this group call (if available).
    unavailable: :class:`bool`
        Denotes if this group call is unavailable.
    ringing: List[:class:`User`]
        A list of users that are currently being rung to join the call.
    region: :class:`VoiceRegion`
        The guild region the group call is being hosted on.
    """

    def __init__(self, state, **kwargs):
        self._state = state
        self._message_id = int(kwargs.pop('message_id'))
        self._channel_id = int(kwargs.pop('channel_id'))
        self.message = kwargs.pop('message', None)
        self.channel = kwargs.pop('channel')
        self.unavailable = kwargs.pop('unavailable', None)
        self._ended = False

        for vs in kwargs.get('voice_states', []):
            state._update_voice_state(vs)

        self._update(**kwargs)

    def _deleteup(self):
        self.ringing = []
        self._ended = True

    def _update(self, **kwargs):
        self.region = try_enum(VoiceRegion, kwargs.get('region'))
        channel = self.channel
        recipients = {channel.me, channel.recipient}
        lookup = {u.id: u for u in recipients}
        self.ringing = list(filter(None, map(lookup.get, kwargs.get('ringing', []))))

    @property
    def initiator(self):
        """Optional[:class:`User`]: Returns the user that started the call. The call message must be available to obtain this information."""
        if self.message:
            return self.message.author

    @property
    def connected(self):
        """:class:`bool`: Returns whether you're in the call (this does not mean you're in the call through the lib)."""
        return self.voice_state_for(self.channel.me).channel.id == self._channel_id

    @property
    def members(self):
        """List[:class:`User`]: Returns all users that are currently in this call."""
        channel = self.channel
        recipients = {channel.me, channel.recipient}
        ret = [u for u in recipients if self.voice_state_for(u).channel.id == self._channel_id]

        return ret

    @property
    def voice_states(self):
        """Mapping[:class:`int`, :class:`VoiceState`]: Returns a mapping of user IDs who have voice states in this call."""
        return set(self._voice_states)

    async def fetch_message(self):
        message = await self.channel.fetch_message(self._message_id)
        if message is not None and self.message is None:
            self.message = message
        return message

    @_running_only
    async def change_region(self, region):
        """|coro|

        Changes the channel's voice region.

        Parameters
        -----------
        region: :class:`VoiceRegion`
            A :class:`VoiceRegion` to change the voice region to.

        Raises
        -------
        HTTPException
            Failed to change the channel's voice region.
        """
        await self._state.http.change_call_voice_region(self.channel.id, str(region))

    @_running_only
    async def ring(self):
        channel = self.channel
        await self._state.http.ring(channel.id, channel.recipient.id)

    @_running_only
    async def stop_ringing(self):
        channel = self.channel
        await self._state.http.stop_ringing(channel.id, channel.recipient.id)

    @_running_only
    async def join(self, **kwargs):
        return await self.channel._connect(**kwargs)

    connect = join

    @_running_only
    async def leave(self, **kwargs):
        state = self._state
        if not (client := state._get_voice_client(self.channel.me.id)):
            return

        return await client.disconnect(**kwargs)

    disconnect = leave

    def voice_state_for(self, user):
        """Retrieves the :class:`VoiceState` for a specified :class:`User`.

        If the :class:`User` has no voice state then this function returns
        ``None``.

        Parameters
        ------------
        user: :class:`User`
            The user to retrieve the voice state for.

        Returns
        --------
        Optional[:class:`VoiceState`]
            The voice state associated with this user.
        """

        return self._state._voice_state_for(user.id)


class GroupCall(PrivateCall):
    """Represents a Discord group call.

    This is accompanied with a :class:`CallMessage` denoting the information.

    Attributes
    -----------
    channel: :class:`GroupChannel`
        The channel the group call is in.
    message: Optional[:class:`Message`]
        The message associated with this group call (if available).
    unavailable: :class:`bool`
        Denotes if this group call is unavailable.
    ringing: List[:class:`User`]
        A list of users that are currently being rung to join the call.
    region: :class:`VoiceRegion`
        The guild region the group call is being hosted on.
    """

    def _update(self, **kwargs):
        self.region = try_enum(VoiceRegion, kwargs.get('region'))
        lookup = {u.id: u for u in self.channel.recipients}
        me = self.channel.me
        lookup[me.id] = me
        self.ringing = list(filter(None, map(lookup.get, kwargs.get('ringing', []))))

    @property
    def members(self):
        """List[:class:`User`]: Returns all users that are currently in this call."""
        ret = [u for u in self.channel.recipients if self.voice_state_for(u).channel.id == self._channel_id]
        me = self.channel.me
        if self.voice_state_for(me).channel.id == self._channel_id:
            ret.append(me)

        return ret

    @_running_only
    async def ring(self, *recipients):
        await self._state.http.ring(self._channel_id, *{r.id for r in recipients})

    @_running_only
    async def stop_ringing(self, *recipients):
        await self._state.http.stop_ringing(self._channel_id, *{r.id for r in recipients})
