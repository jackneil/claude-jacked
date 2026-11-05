"""The dashboard socket protocol: uvicorn's websockets Sans-I/O protocol with
a limit on the fragments of one message.

uvicorn (0.54) keeps each continuation frame of a message in a list and does
not count the frames. websockets limits the total size of a message
(``ws_max_size``), but an empty fragment adds zero bytes to that total. Thus a
client that sends empty continuation frames makes the list grow without a
limit, and the service uses more and more memory. websockets 17.4 limits its
own message assemblers to 4096 fragments; the uvicorn protocol does not use
those assemblers, so jacked sets the same limit here.

``SERVER_SETTINGS["ws"]`` (jacked/api/serving.py) names this class, so each
jacked server uses it.
"""

from __future__ import annotations

from uvicorn.protocols.websockets.websockets_sansio_impl import (
    WebSocketsSansIOProtocol,
)
from websockets.frames import CloseCode, Frame

#: The maximum number of frames in one message, the first frame included.
#: This is the same value as ``MAX_FRAGMENTS`` in websockets 17.4.
MAX_FRAGMENTS = 4096


class BoundedFragmentsProtocol(WebSocketsSansIOProtocol):
    """Close the connection with 1009 (message too big) at fragment 4097.

    >>> issubclass(BoundedFragmentsProtocol, WebSocketsSansIOProtocol)
    True
    >>> MAX_FRAGMENTS
    4096
    """

    def handle_cont(self, event: Frame) -> None:
        if self.close_sent:
            # The connection is closing. Do not keep the frames of a message
            # that nobody will read.
            return
        if len(self.frames) >= MAX_FRAGMENTS:
            self.conn.fail(CloseCode.MESSAGE_TOO_BIG, "too many fragments")
            self.handle_parser_exception()
            return
        super().handle_cont(event)
