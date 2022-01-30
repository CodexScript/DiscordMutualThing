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

import asyncio
import base64
import inspect
import logging
from threading import Thread
import time

from discord.backoff import ExponentialBackoff
from discord.errors import ClientException, ConnectionClosed, InvalidArgument
from discord.user import BaseUser as User
from discord.utils import ExpiringString

from .gateway import DiscordAuthWebSocket, ConnectionClosed

_import_cache = {}

def try_import(*names):
    names = list(names)
    ret = []
    for name in names:
        try:
            ret.append(_import_cache[name])
        except KeyError:
            pass
        else:
            names.remove(name)

    if len(names) == 0:
        return ret if len(ret) > 1 else ret[0]

    for name in names:
        var = name.split('.')[-1]
        try:
            globals()[var] = __import__(name, fromlist=['0'])
        except ImportError:
            _import_cache[name] = False
            ret.append(False)
        else:
            _import_cache[name] = True
            ret.append(True)

    return ret if len(ret) > 1 else ret[0]


class GUI(Thread):
    def __init__(self, constructor):
        super().__init__(daemon=True)
        self._create_qr = constructor
        self._window = None

    def _quit(self):
        self.stop()

    def _create_gui(self):
        qr = self._create_qr()
        window = tkinter.Tk()
        window.title('QR Code')
        window.protocol('WM_DELETE_WINDOW', self._quit)
        bmp = tkinter.BitmapImage(data=qr.xbm(scale=5, quiet_zone=1), background='white')
        label = tkinter.Label(image=bmp)
        label.image = bmp
        label.pack()
        return window

    def start(self):
        self._run = True
        super().start()

    def stop(self):
        self._run = False
        self._started.clear()

    def run(self):
        self._window = window = self._create_gui()
        while self._run:
            window.update()
        window.destroy()
        self._window = None


class QRCode:
    def __init__(self, url, client):
        if not try_import('pyqrcode'):
            raise RuntimeError('PyQRCode library needed in order to access the QR code')

        self.url = url
        self.client = client
        self._window = None

    async def _wait(self, window):
        await self.client.wait_until_scanned()
        window.stop()

    def _create_qr(self):
        if not self.url:
            raise ClientException('URL is currently expired, try again soon')
        return pyqrcode.create(self.url, error='M')

    def render(self, *, wait=False):
        if not try_import('tkinter'):
            raise RuntimeError('Tkinter library needed in order to render the QR code')

        if not (window := self._window):
            self._window = window = GUI(self._create_qr)

        window.start()

        if wait:
            return self._wait(window)

    def print(self, *, quiet_zone=1, **kwargs):
        print(self._create_qr().terminal(quiet_zone=quiet_zone, **kwargs))

    def save(self, *args, **kwargs):
        possible_types = {'svg', 'eps', 'png', 'xbm'}
        qr = self._create_qr()

        if len(args) == 1:
            type = args[0].split('.')[-1].lower()
        else:
            type = args.pop(0)
        if type not in possible_types:
            raise InvalidArgument(f'Cannot save to {type}')

        func = getattr(qr, type)
        return func(*args, **kwargs)


class RemoteAuthClient:
    def __init__(self, client=None, *, loop=None):
        imports = {'cryptography.hazmat.primitives.' + i for i in {'serialization', 'hashes', 'asymmetric.rsa', 'asymmetric.padding'}}
        if not any(try_import(*imports)):
            raise RuntimeError('Cryptography library needed in order to use remote auth')

        self._client = client
        self.loop = loop or asyncio.get_event_loop()

        self._parsers = parsers = {}
        for attr, func in inspect.getmembers(self):
            if attr.startswith('on_'):
                parsers[attr[3:]] = func

        self._reset()

    def _reset(self, full=True, data=True):
        if full:
            self.ws = None
            self.token = None
            self.user = None
            self._finished = None
            self._ready = None
            self._waiting = None
            self._finished = None
            self._runner = None
            self._last_status = None
        if data:
            self.qr_code = None
            self.url = None

    def _decrypt(self, data, *, decode=True):
        data = base64.b64decode(data)
        return self.private_key.decrypt(
            data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )

    @staticmethod
    def _decode_user_payload(payload):
        data = payload.decode('utf-8').split(':')
        return {
            'id': data[0],
            'discriminator': data[1],
            'avatar': data[2],
            'username': data[3],
        }

    @property
    def status(self):
        return self._last_status

    def ws_connect(self):
        return DiscordAuthWebSocket.from_client(self)

    async def poll_ws(self, reconnect):
        backoff = ExponentialBackoff()
        while True:
            try:
                await self.ws.poll_event()
            except ConnectionClosed as exc:
                """Codes:
                -1   = Internal
                1000 = Finished (accepted/denied)
                4001 = Handshake failure
                4003 = Timeout/QR expired
                """
                print('CLOSED')

                if exc.code == 1000:
                    log.info('Remote auth successful. Gateway disconncted.')
                    return
                elif exc.code == 4001:
                    raise RuntimeError('Please open an issue with this title: [RA] Handshake failure')

                if not reconnect:
                    await self.disconnect()
                    raise

                if exc.code == 4003:
                    log.info('Remote auth gateway timed out. Reconnecting...')
                else:
                    retry = backoff.delay()
                    log.info(f'Remote auth gateway disconnected with code {exc.code}. Reconnecting in {retry}s.')
                    await asyncio.sleep(retry)

                await self.disconnect()

                try:
                    await self.connect(reconnect=reconnect)
                except:
                    log.warning('Could not connect to the remote auth gateway. Retrying...')
                    raise

    async def connect(self, *, reconnect=False):
        self._reset()
        for attr in {'_ready', '_waiting', '_finished'}:
            setattr(self, attr, asyncio.Event())
        self.private_key = pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = pk.public_key()
        await self.ws_connect()

        if not self._runner:
            self._runner = self.loop.create_task(self.poll_ws(reconnect))

    async def disconnect(self, *, force=False):
        if self.ws:
            try:
                await self.ws.close()
            finally:
                self.url.destroy()
                self._reset()

    async def wait_until_ready(self):
        if not self._ready:
            raise InvalidArgument('Not connected to remote auth gateway')
        await self._ready.wait()

    async def wait_until_scanned(self):
        if not self._waiting:
            raise InvalidArgument('Not connected to remote auth gateway')
        await self._waiting.wait()

    async def wait_until_result(self):
        if not self._finished:
            raise InvalidArgument('Not connected to remote auth gateway')
        await self._finished.wait()

    async def get_qr_code(self):
        await self.wait_until_ready()
        return self.qr_code

    async def get_url(self):
        await self.wait_until_ready()
        return self.url

    async def get_user(self):
        await self.wait_until_scanned()
        return self.user

    async def get_token(self):
        await self.wait_until_result()
        return self.token

    async def on_hello(self, data):
        key = self.public_key.public_bytes(encoding=serialization.Encoding.PEM,
                                           format=serialization.PublicFormat.SubjectPublicKeyInfo)
        await self.ws.init(key.replace(b'\n', b'')[26:-24].decode('utf-8'))

    async def on_nonce_proof(self, data):
        digest = hashes.Hash(hashes.SHA256())
        digest.update(self._decrypt(data['encrypted_nonce']))
        nonce = base64.urlsafe_b64encode(digest.finalize()).decode('utf-8').rstrip('=')
        await self.ws.nonce_proof(nonce)

    async def on_pending_remote_init(self, data):
        args = (
            'https://discordapp.com/ra/' + data['fingerprint'],
            self.ws.timeout - time.time()
        )

        if not (url := self.url):
            self.url = url = ExpiringString(*args)
            self.qr_code = QRCode(url, self)
        else:
            url._update(*args)

        self._ready.set()

    async def on_pending_finish(self, data):
        user = self._decode_user_payload(self._decrypt(data['encrypted_user_payload']))
        self.user = User(state=None, data=user)
        self._waiting.set()

    async def on_finish(self, data):
        self.token = token = self._decrypt(data['encrypted_token']).decode('utf-8')
        self.url.destroy()
        self._last_status = True
        self._finished.set()
        self._reset(False)
        if self._client:
            await self._client.login(token)

    async def on_cancel(self, *_):
        self.url.destroy()
        self._last_status = False
        self._finished.set()
        self._reset(False)
