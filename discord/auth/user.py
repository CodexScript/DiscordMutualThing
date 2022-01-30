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

from abc import ABCMeta

from discord.user import ClientUser as _User

class _FakeState:
    def __init__(self, http):
        self.http = http

class _HideMeta(ABCMeta):  # I know this is cursed
    def __new__(cls, cls_name, cls_bases, cls_dict):
        cls_dict.setdefault("__excluded__", set())
        out_cls = super(_HideMeta, cls).__new__(cls, cls_name, cls_bases, cls_dict)

        def __getattribute__(self, name):
            if name in cls_dict["__excluded__"]:
                raise AttributeError(name)
            else:
                return super(out_cls, self).__getattribute__(name)
        out_cls.__getattribute__ = __getattribute__

        def __dir__(self):
            return sorted((set(dir(out_cls)) | set(self.__dict__.keys())) - set(cls_dict["__excluded__"])) 
        out_cls.__dir__ = __dir__

        return out_cls


class ClientUser(_User, metaclass=_HideMeta):
    __excluded__ = {'get_relationship', 'relationships', 'friends', 'blocked',
                    'create_group', 'note', '_relationships', 'settings', 'edit_settings'}

    def __init__(self, http, data):
        super().__init__(state=_FakeState(http), data=data)
