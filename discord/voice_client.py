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

"""Some documentation to refer to:

- Our main web socket (mWS) sends opcode 4 with a guild ID and channel ID.
- The mWS receives VOICE_STATE_UPDATE and VOICE_SERVER_UPDATE.
- We pull the session_id from VOICE_STATE_UPDATE.
- We pull the token, endpoint and server_id from VOICE_SERVER_UPDATE.
- Then we initiate the voice web socket (vWS) pointing to the endpoint.
- We send opcode 0 with the user_id, server_id, session_id and token using the vWS.
- The vWS sends back opcode 2 with an ssrc, port, modes(array) and hearbeat_interval.
- We send a UDP discovery packet to endpoint:port and receive our IP and our port in LE.
- Then we send our IP and port via vWS with opcode 1.
- When that's all done, we receive opcode 4 from the vWS.
- Finally we can transmit data to endpoint:port.
"""

import asyncio
from dataclasses import dataclass
import socket
import logging
import struct
import threading

from . import opus, utils
from .backoff import ExponentialBackoff
from .gateway import *
from .errors import ClientException, ConnectionClosed
from .player import AudioPlayer, AudioSource

try:
    import nacl.secret
    has_nacl = True
except ImportError:
    has_nacl = False

log = logging.getLogger(__name__)

class VoiceProtocol:
    """A class that represents the Discord voice protocol.

    This is an abstract class. The library provides a concrete implementation
    under :class:`VoiceClient`.

    This class allows you to implement a protocol to allow for an external
    method of sending voice, such as Lavalink_ or a native library implementation.

    These classes are passed to :meth:`abc.Connectable.connect`.

    .. _Lavalink: https://github.com/freyacodes/Lavalink

    Parameters
    -----------
    client: :class:`Client`
        The client (or its subclasses) that started the connection request.
    channel: :class:`abc.Connectable`
        The voice channel that is being connected to.
    """

    def __init__(self, client, channel):
        self.client = client
        self.channel = channel

    async def on_voice_state_update(self, data):
        """|coro|

        An abstract method that is called when the client's voice state
        has changed. This corresponds to ``VOICE_STATE_UPDATE``.

        Parameters
        ------------
        data: :class:`dict`
            The raw `voice state payload`__.

            .. _voice_state_update_payload: https://discord.com/developers/docs/resources/voice#voice-state-object

            __ voice_state_update_payload_
        """
        raise NotImplementedError

    async def on_voice_server_update(self, data):
        """|coro|

        An abstract method that is called when initially connecting to voice.
        This corresponds to ``VOICE_SERVER_UPDATE``.

        Parameters
        ------------
        data: :class:`dict`
            The raw `voice server update payload`__.

            .. _voice_server_update_payload: https://discord.com/developers/docs/topics/gateway#voice-server-update-voice-server-update-event-fields

            __ voice_server_update_payload_
        """
        raise NotImplementedError

    async def connect(self, *, timeout, reconnect):
        """|coro|

        An abstract method called when the client initiates the connection request.

        When a connection is requested initially, the library calls the constructor
        under ``__init__`` and then calls :meth:`connect`. If :meth:`connect` fails at
        some point then :meth:`disconnect` is called.

        Within this method, to start the voice connection flow it is recommended to
        use :meth:`Guild.change_voice_state` to start the flow. After which,
        :meth:`on_voice_server_update` and :meth:`on_voice_state_update` will be called.
        The order that these two are called is unspecified.

        Parameters
        ------------
        timeout: :class:`float`
            The timeout for the connection.
        reconnect: :class:`bool`
            Whether reconnection is expected.
        """
        raise NotImplementedError

    async def disconnect(self, *, force):
        """|coro|

        An abstract method called when the client terminates the connection.

        See :meth:`cleanup`.

        Parameters
        ------------
        force: :class:`bool`
            Whether the disconnection was forced.
        """
        raise NotImplementedError

    def cleanup(self):
        """This method *must* be called to ensure proper clean-up during a disconnect.

        It is advisable to call this from within :meth:`disconnect` when you are
        completely done with the voice protocol instance.

        This method removes it from the internal state cache that keeps track of
        currently alive voice clients. Failure to clean-up will cause subsequent
        connections to report that it's still connected.
        """
        key_id, _ = self.channel._get_voice_client_key()
        self.client._connection._remove_voice_client(key_id)

class Player:
    def __init__(self, client):
        self.client = client
        self.loop = client.loop

        self.encoder = None
        self._player = None

    def send(self, data, encode=True):
        """Sends an audio packet composed of the data.

        You must be connected to play audio.

        Parameters
        ----------
        data: :class:`bytes`
            The :term:`py:bytes-like object` denoting PCM or Opus voice data.
        encode: :class:`bool`
            Indicates if ``data`` should be encoded into Opus.

        Raises
        -------
        ClientException
            You are not connected.
        opus.OpusError
            Encoding the data failed.
        """
        if encode:
            data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)

        self.client.send_audio_packet(data)

    @property
    def ws(self):
        return self.client.ws

    @property
    def source(self):
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        return self._player.source if self._player else None

    @source.setter
    def source(self, value):
        if not isinstance(value, AudioSource):
            raise TypeError('Expected AudioSource not {0.__class__.__name__}'.format(value))

        if self._player is None:
            raise ValueError('Not playing anything')

        self._player._set_source(value)

    @property
    def playing(self):
        return self.is_playing()

    def is_playing(self):
        """Indicates if we're currently playing audio."""
        return self._player is not None and self._player.is_playing()

    @property
    def paused(self):
        return self.is_paused()

    def is_paused(self):
        """Indicates if we're playing audio, but if we're paused."""
        return self._player is not None and self._player.is_paused()

    def play(self, source, *, after=None):
        """Plays an :class:`AudioSource`.

        The finalizer, ``after`` is called after the source has been exhausted
        or an error occurred.

        If an error happens while the audio player is running, the exception is
        caught and the audio player is then stopped.  If no after callback is
        passed, any caught exception will be displayed as if it were raised.

        Parameters
        -----------
        source: :class:`AudioSource`
            The audio source we're reading from.
        after: Callable[[:class:`Exception`], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.

        Raises
        -------
        ClientException
            Already playing audio or not connected.
        TypeError
            Source is not a :class:`AudioSource` or after is not a callable.
        OpusNotLoaded
            Source is not opus encoded and opus is not loaded.
        """

        if not self.client.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_playing():
            raise ClientException('Already playing audio.')

        if not isinstance(source, AudioSource):
            raise TypeError('source must an AudioSource not {0.__class__.__name__}'.format(source))

        if not self.encoder and not source.is_opus():
            self.encoder = opus.Encoder()

        self._player = AudioPlayer(source, self, after=after)
        self._player.start()

    def pause(self):
        """Pauses the audio playing."""
        if self._player:
            self._player.pause()

    def resume(self):
        """Resumes the audio playing."""
        if self._player:
            self._player.resume()

    def stop(self):
        """Stops playing audio."""
        if self._player:
            self._player.stop()
            self._player = None

class Listener:
    def __init__(self, client):
        self.client = client
        self.loop = client.loop

        self.decoder = None
        self._listener = None

    @property
    def ws(self):
        return self.client.ws

    @property
    def listening(self):
        return self.is_playing()

    def is_listening(self):
        """Indicates if we're currently listening."""
        return self._listener is not None and self._listener.is_listening()

    @property
    def paused(self):
        return self.is_paused()

    def is_paused(self):
        """Indicates if we're listening, but we're paused."""
        return self._listener is not None and self._listener.is_paused()

    def listen(self, sink, *, callback=None):
        if not self.client.is_connected():
            raise ClientException('Not connected to voice.')

        if self.is_listening():
            raise ClientException('Already listening.')

        if not isinstance(sink, AudioSink):
            raise TypeError(f'sink must an AudioSink not {sink.__class__.__name__}')

class VoiceClient(VoiceProtocol):
    """Represents a Discord voice connection.

    You do not create these, you typically get them from
    e.g. :meth:`VoiceChannel.connect`.

    Warning
    --------
    In order to use PCM based AudioSources, you must have the opus library
    installed on your system and loaded through :func:`opus.load_opus`.
    Otherwise, your AudioSources must be opus encoded (e.g. using :class:`FFmpegOpusAudio`)
    or the library will not be able to transmit audio.

    Attributes
    -----------
    session_id: :class:`str`
        The voice connection session ID.
    token: :class:`str`
        The voice connection token.
    endpoint: :class:`str`
        The endpoint we are connecting to.
    channel: :class:`abc.Connectable`
        The voice channel connected to.
    loop: :class:`asyncio.AbstractEventLoop`
        The event loop that the voice client is running on.
    """
    def __init__(self, client, channel):
        if not has_nacl:
            raise RuntimeError("PyNaCl library needed in order to use voice")

        super().__init__(client, channel)
        state = client._connection
        self.token = None
        self.socket = None
        self.loop = state.loop
        self._state = state

        # This will be used in the threads
        self._connected = threading.Event()
        self._handshaking = False
        self._potentially_reconnecting = False
        self._voice_state_complete = asyncio.Event()
        self._voice_server_complete = asyncio.Event()

        self.mode = None
        self._connections = 0
        self.sequence = 0
        self.timestamp = 0
        self.player = Player(self)
        self.listener = Listener(self)
        self._runner = None
        self._lite_nonce = 0
        self.ws = None
        self.idrcs = {}
        self.ssids = {}

    warn_nacl = not has_nacl
    supported_modes = (
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    )

    @property
    def ssrc(self):
        """:class:`str`: Our ssrc."""
        return self.idrcs.get(self.user.id)

    @ssrc.setter
    def ssrc(self, value):
        self.idrcs[self.user.id] = value
        self.ssids[value] = self.user.id

    @property
    def guild(self):
        """Optional[:class:`Guild`]: The guild we're connected to, if applicable."""
        return getattr(self.channel, 'guild', None)

    @property
    def user(self):
        """:class:`ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self._state.user

    # Connection related

    async def on_voice_state_update(self, data):
        self.session_id = data['session_id']
        channel_id = data['channel_id']

        if not self._handshaking or self._potentially_reconnecting:
            # If we're done handshaking then we just need to update ourselves
            # If we're potentially reconnecting due to a 4014, then we need to differentiate
            # a channel move and an actual force disconnect
            if channel_id is None:
                # We're being disconnected so cleanup
                await self.disconnect()
            else:
                guild = self.guild
                if guild is not None:
                    self.channel = channel_id and guild.get_channel(int(channel_id))
                else:
                    self.channel = channel_id and self._state._get_private_channel(int(channel_id))
        else:
            self._voice_state_complete.set()

    async def on_voice_server_update(self, data):
        if self._voice_server_complete.is_set():
            log.info('Ignoring extraneous voice server update.')
            return

        self.token = data.get('token')
        self.server_id = server_id = utils._get_as_snowflake(data, 'guild_id')
        if server_id is None:
            self.server_id = utils._get_as_snowflake(data, 'channel_id')
        endpoint = data.get('endpoint')

        if endpoint is None or self.token is None:
            log.warning('Awaiting endpoint... This requires waiting. ' \
                        'If timeout occurred considering raising the timeout and reconnecting.')
            return

        self.endpoint, _, _ = endpoint.rpartition(':')
        if self.endpoint.startswith('wss://'):  # Shouldn't ever be there...
            self.endpoint = self.endpoint[6:]

        self.endpoint_ip = None

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)

        if not self._handshaking:
            # If we're not handshaking then we need to terminate our previous connection to the websocket
            await self.ws.close(4000)
            return

        self._voice_server_complete.set()

    async def voice_connect(self):
        if self.guild:
            await self.guild.change_voice_state(channel=self.channel)
        else:
            await self._state.client.change_voice_state(channel=self.channel, self_video=False)

    async def voice_disconnect(self):
        log.info('The voice handshake is being terminated for Channel ID %s (Guild ID %s).', self.channel.id, getattr(self.guild, 'id', None))
        if self.guild:
            await self.guild.change_voice_state(channel=None)
        else:
            await self._state.client.change_voice_state(channel=None, self_video=False)

    def prepare_handshake(self):
        self._voice_state_complete.clear()
        self._voice_server_complete.clear()
        self._handshaking = True
        log.info('Starting voice handshake (connection attempt %d)...', self._connections + 1)
        self._connections += 1

    def finish_handshake(self):
        log.info('Voice handshake complete. Endpoint found: %s.', self.endpoint)
        self._handshaking = False
        self._voice_server_complete.clear()
        self._voice_state_complete.clear()

    async def connect_websocket(self, resume=False):
        ws = await DiscordVoiceWebSocket.from_client(self, resume=resume)
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws

    async def connect(self, *, reconnect, timeout):
        log.info('Connecting to voice...')
        self.timeout = timeout

        for i in range(5):
            self.prepare_handshake()

            # This has to be created before we start the flow.
            futures = [
                self._voice_state_complete.wait(),
                self._voice_server_complete.wait(),
            ]

            # Start the connection flow
            await self.voice_connect()

            try:
                await utils.sane_wait_for(futures, timeout=timeout)
            except asyncio.TimeoutError:
                await self.disconnect(force=True)
                raise

            self.finish_handshake()

            try:
                self.ws = await self.connect_websocket()
                break
            except (ConnectionClosed, asyncio.TimeoutError):
                if reconnect:
                    log.exception('Failed to connect to voice. Retrying...')
                    await asyncio.sleep(1 + i * 2.0)
                    await self.voice_disconnect()
                    continue
                else:
                    await self.disconnect(force=True)
                    raise

        if self._runner is None:
            self._runner = self.loop.create_task(self.poll_voice_ws(reconnect))

    async def potential_reconnect(self):
        # Attempt to stop the player thread from playing early
        self._connected.clear()
        self.prepare_handshake()
        self._potentially_reconnecting = True
        try:
            # We only care about VOICE_SERVER_UPDATE since VOICE_STATE_UPDATE can come before we get disconnected
            await asyncio.wait_for(self._voice_server_complete.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self._potentially_reconnecting = False
            await self.disconnect(force=True)
            return False

        self.finish_handshake()
        self._potentially_reconnecting = False
        try:
            self.ws = await self.connect_websocket()
        except (ConnectionClosed, asyncio.TimeoutError):
            return False
        else:
            return True

    async def potential_resume(self):
        # Attempt to stop the player thread from playing early
        self._connected.clear()
        self._potentially_reconnecting = True

        try:
            self.ws = await self.connect_websocket(resume=True)
        except (ConnectionClosed, asyncio.TimeoutError):
            return False  # Reconnect normally if RESUME failed
        else:
            return True
        finally:
            self._potentially_reconnecting = False

    @property
    def latency(self):
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds.

        This could be referred to as the Discord Voice WebSocket latency and is
        an analogue of user's voice latencies as seen in the Discord client.

        .. versionadded:: 1.4
        """
        ws = self.ws
        return float('inf') if not ws else ws.latency

    @property
    def average_latency(self):
        """:class:`float`: Average of most recent 20 HEARTBEAT latencies in seconds.

        .. versionadded:: 1.4
        """
        ws = self.ws
        return float('inf') if not ws else ws.average_latency

    async def poll_voice_ws(self, reconnect):
        backoff = ExponentialBackoff()
        while True:
            try:
                await self.ws.poll_event()
            except (ConnectionClosed, asyncio.TimeoutError) as exc:
                if isinstance(exc, ConnectionClosed):
                    if exc.code == 1000:  # Normal closure (obviously)
                        log.info('Disconnecting from voice normally, close code %d.', exc.code)
                        await self.disconnect()
                        break
                    if exc.code == 4015:
                        log.info('Disconnected from voice (close code %d)... potentially RESUMEing.', exc.code)
                        successful = await self.potential_resume()
                        if not successful:
                            log.info('RESUME was unsuccessful, disconnecting from voice normally...')
                            await self.disconnect()
                            break
                        else:
                            continue
                    if exc.code == 4014:
                        log.info('Disconnected from voice by force (close code %d)... potentially reconnecting.', exc.code)
                        successful = await self.potential_reconnect()
                        if not successful:
                            log.info('Reconnect was unsuccessful, disconnecting from voice normally...')
                            await self.disconnect()
                            break
                        else:
                            continue

                if not reconnect:
                    await self.disconnect()
                    raise

                retry = backoff.delay()
                log.exception('Disconnected from voice... Reconnecting in %.2fs.', retry)
                self._connected.clear()
                await asyncio.sleep(retry)
                try:
                    await self.connect(reconnect=True, timeout=self.timeout)
                except asyncio.TimeoutError:
                    # at this point we've retried 5 times... let's continue the loop.
                    log.warning('Could not connect to voice... Retrying...')
                    continue

    async def disconnect(self, *, force=False):
        """|coro|

        Disconnects this voice client from voice.
        """
        if not force and not self.is_connected():
            return

        self.player.stop()
        self._connected.clear()

        try:
            if self.ws:
                await self.ws.close()

            await self.voice_disconnect()
        finally:
            self.cleanup()
            if self.socket:
                self.socket.close()

    async def move_to(self, channel):
        """|coro|

        Moves you to a different voice channel.

        Parameters
        -----------
        channel: :class:`abc.Snowflake`
            The channel to move to. Must be a :class:`abc.Connectable`.
        """
        if self.guild:
            await self.guild.change_voice_state(channel=channel)
        else:
            await self._state.client.change_voice_state(channel=channel, self_video=False)

    @property
    def connected(self):
        return self.is_connected()

    def is_connected(self):
        """Indicates if the voice client is connected to voice."""
        return self._connected.is_set()

    # Audio related

    def _flip_ssrc(self, query):
        value = self.idrcs.get(query)
        if value is None:
            value = self.ssids.get(query)
        return value

    def _set_ssrc(self, user_id, ssrc):
        self.idrcs[user_id] = ssrc
        self.ssids[ssrc] = user_id

    def _checked_add(self, attr, value, limit):
        val = getattr(self, attr)
        if val + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, val + value)

    @staticmethod
    def _strip_header(data):
        if data[0] == 0xbe and data[1] == 0xde and len(data) > 4:
            _, length = struct.unpack_from('>HH', data)
            offset = 4 + length * 4
            data = data[offset:]
        return data

    def _get_voice_packet(self, data):
        header = bytearray(12)

        # Formulate RTP header
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, self.sequence)
        struct.pack_into('>I', header, 4, self.timestamp)
        struct.pack_into('>I', header, 8, self.ssrc)

        encrypt_packet = getattr(self, '_encrypt_' + self.mode)
        return encrypt_packet(header, data)

    def _encrypt_xsalsa20_poly1305(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)
        nonce[:12] = header

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext

    def _decrypt_xsalsa20_poly1305(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)
        nonce[:12] = header

        return self._strip_header(box.decrypt(bytes(data), bytes(nonce)))

    def _encrypt_xsalsa20_poly1305_suffix(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

        return header + box.encrypt(bytes(data), nonce).ciphertext + nonce

    def _decrypt_xsalsa20_poly1305_suffix(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce_size = nacl.secret.SecretBox.NONCE_SIZE
        nonce = data[-nonce_size:]

        return self._strip_header(box.decrypt(bytes(data[:-nonce_size]), nonce))

    def _encrypt_xsalsa20_poly1305_lite(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)
        nonce[:4] = struct.pack('>I', self._lite_nonce)
        self._checked_add('_lite_nonce', 1, 4294967295)

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext + nonce[:4]

    def _decrypt_xsalsa20_poly1305_lite(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        data = data[:-4]

        return self._strip_header(box.decrypt(bytes(data), bytes(nonce)))

    def play(self, *args, **kwargs):
        """Plays an :class:`AudioSource`.

        The finalizer, ``after`` is called after the source has been exhausted
        or an error occurred.

        If an error happens while the audio player is running, the exception is
        caught and the audio player is then stopped.  If no after callback is
        passed, any caught exception will be displayed as if it were raised.

        Parameters
        -----------
        source: :class:`AudioSource`
            The audio source we're reading from.
        after: Callable[[:class:`Exception`], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.

        Raises
        -------
        ClientException
            Already playing audio or not connected.
        TypeError
            Source is not a :class:`AudioSource` or after is not a callable.
        OpusNotLoaded
            Source is not opus encoded and opus is not loaded.
        """

        return self.player.play(*args, **kwargs)

    def listen(self, *args, **kwargs):
        return self.listener.listen(*args, **kwargs)

    @property
    def source(self):
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        return self.player.source

    @source.setter
    def source(self, value):
        self.player.source = value

    @property
    def sink(self):
        """Optional[:class:`AudioSink`]: Where received audio is being sent.

        This property can also be used to change the value.
        """
        return self.listener.sink

    @sink.setter
    def sink(self, value):
        self.listener.sink = value
        #if not isinstance(value, AudioSink):
            #raise TypeError('Expected AudioSink not {value.__class__.__name__}')
        #if self._recorder is None:
            #raise ValueError('Not listening')

    def send_audio_packet(self, data):
        """Sends an audio packet composed of the data.

        You must be connected to play audio.

        Parameters
        ----------
        data: :class:`bytes`
            The :term:`py:bytes-like object` denoting Opus voice data.

        Raises
        -------
        ClientException
            You are not connected.
        """

        self._checked_add('sequence', 1, 65535)
        packet = self._get_voice_packet(data)

        try:
            self.socket.sendto(packet, (self.endpoint_ip, self.voice_port))
        except BlockingIOError:
            log.warning('A packet has been dropped (seq: %s, timestamp: %s).', self.sequence, self.timestamp)

        self._checked_add('timestamp', opus.Encoder.SAMPLES_PER_FRAME, 4294967295)
