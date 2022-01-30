"""
The MIT License (MIT)

Copyright (c) 2021-present Dolfies

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

import aiohttp
import asyncio
import json
import logging
import math
import time

from discord import utils
from discord.errors import ConnectionClosed
from discord.gateway import KeepAliveHandler as _KeepAliveHandler, WebSocketClosure

log = logging.getLogger(__name__)

__all__ = ('DiscordAuthWebSocket',)


class KeepAliveHandler(_KeepAliveHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.msg = 'Keeping remote auth websocket alive.'
        self.block_msg = 'Remote auth heartbeat blocked for more than %s seconds'
        self.behind_msg = 'Can\'t keep up, remote auth websocket is %.1fs behind.'
        self.not_responding_msg = 'Remote auth gateway has stopped responding. Closing and restarting.'
        self.no_stop_msg = 'An error occurred while stopping the auth gateway. Ignoring.'

    def get_payload(self):
        return {
            'op': self.ws.HEARTBEAT
        }


class DiscordAuthWebSocket:
    HELLO               = 'hello'
    INIT                = 'init'
    NONCE_PROOF         = 'nonce_proof'
    PENDING_REMOTE_INIT = 'pending_remote_init'
    PENDING_FINISH      = 'pending_finish'
    FINISH              = 'finish'
    CANCEL              = 'cancel'
    HEARTBEAT           = 'heartbeat'
    HEARTBEAT_ACK       = 'heartbeat_ack'

    def __init__(self, socket, loop):
        self.ws = socket
        self.loop = loop

        self._keep_alive = None
        self._close_code = None

    @classmethod
    async def from_client(cls, client):
        """Creates a websocket for the :class:`RemoteAuthClient`."""
        gateway = 'wss://remote-auth-gateway.discord.gg/?v=1'
        http = client._client.http
        await http.startup()
        socket = await http.ws_connect(gateway, host='remote-auth-gateway.discord.gg')
        ws = cls(socket, loop=client.loop)
        ws.gateway = gateway
        ws._parsers = client._parsers
        ws._max_heartbeat_timeout = 60.0

        client.ws = ws

        await ws.poll_event()

    async def send_as_json(self, data):
        await self.ws.send_str(utils.to_json(data))

    send_heartbeat = send_as_json

    async def init(self, key):
        payload = {
            'op': self.INIT,
            'encoded_public_key': key
        }
        await self.send_as_json(payload)
        log.info('Remote auth gateway has sent the INIT payload.')

    async def nonce_proof(self, nonce):
        payload = {
            'op': self.NONCE_PROOF,
            'proof': nonce
        }
        await self.send_as_json(payload)
        log.info('Remote auth gateway has sent the NONCE_PROOF payload.')

    async def _call_handler(self, op, data):
        try:
            func = self._parsers[op]
        except KeyError:
            log.debug('Unknown auth event %s.', event)
        else:
            await func(data)

    async def received_message(self, msg):
        log.debug('Remote auth received: %s.', msg)
        op = msg.pop('op')

        if self._keep_alive:
            self._keep_alive.tick()

        if op == self.HELLO:
            interval = msg['heartbeat_interval'] / 1000.0
            self.timeout = time.time() + math.floor(msg['timeout_ms'] / 1000)
            self._keep_alive = KeepAliveHandler(ws=self, interval=interval)
            await self.send_as_json(self._keep_alive.get_payload())
            self._keep_alive.start()
            await self._call_handler(op, msg)
        elif op == self.HEARTBEAT_ACK:
            if self._keep_alive:
                self._keep_alive.ack()
        else:
            await self._call_handler(op, msg)

    async def poll_event(self):
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=self._max_heartbeat_timeout)
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self.received_message(json.loads(msg.data))
            elif msg.type is aiohttp.WSMsgType.ERROR:
                log.debug('Remote auth received %s.', msg)
                raise msg.data
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                log.debug('Remote auth received %s.', msg)
                raise WebSocketClosure()
        except (asyncio.TimeoutError, WebSocketClosure) as e:
            self._cleanup()

            if isinstance(e, asyncio.TimeoutError):
                log.info('Remote auth gateway timed out. Signalling to reconnect.')
                code = -1

            code = self._close_code or self.ws.close_code
            raise ConnectionClosed(self.ws, code=code)

    @property
    def latency(self):
        """:class:`float`: Latency between a HEARTBEAT and its HEARTBEAT_ACK in seconds."""
        heartbeat = self._keep_alive
        return float('inf') if heartbeat is None else heartbeat.latency

    @property
    def average_latency(self):
        """:class:`list`: Average of last 20 HEARTBEAT latencies."""
        heartbeat = self._keep_alive
        if heartbeat is None or not heartbeat.recent_ack_latencies:
            return float('inf')

        return sum(heartbeat.recent_ack_latencies) / len(heartbeat.recent_ack_latencies)

    def _cleanup(self):
        if self._keep_alive is not None:
            self._keep_alive.stop()

    async def close(self, code=1000):
        self._cleanup()
        self._close_code = code
        await self.ws.close(code=code)
