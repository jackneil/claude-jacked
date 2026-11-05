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

import logging

from uvicorn.protocols.websockets.websockets_sansio_impl import (
    WebSocketsSansIOProtocol,
)
from websockets.frames import CloseCode, Frame

logger = logging.getLogger(__name__)

#: The maximum number of frames in one message, the first frame included.
#: This is the same value as ``MAX_FRAGMENTS`` in websockets 17.4.
MAX_FRAGMENTS = 4096

# The uvicorn members that this subclass overrides or calls. They are not a
# public API, so a uvicorn release can rename them. If one is missing, the
# limit does not work: the service logs that once when it imports this module.
_BASE_MEMBERS = ("handle_cont", "handle_parser_exception", "handle_events")
_MISSING = [name for name in _BASE_MEMBERS if not hasattr(WebSocketsSansIOProtocol, name)]
if _MISSING:
    logger.warning(
        "websocket fragment limit is not active: uvicorn has no %s", ", ".join(_MISSING)
    )


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
            self.frames = []
            return
        if len(self.frames) >= MAX_FRAGMENTS:
            self._refuse_long_message()
            return
        super().handle_cont(event)

    def _refuse_long_message(self) -> None:
        opened = self.handshake_complete
        logger.warning(
            "websocket closed: a message passed %d fragments (close code 1009, handshake %s)",
            MAX_FRAGMENTS,
            "complete" if opened else "not complete",
        )
        self.frames = []
        self.conn.fail(CloseCode.MESSAGE_TOO_BIG, "too many fragments")
        if self.conn.close_sent is not None:
            # The handshake is complete: send the 1009 close frame, tell the
            # app, and close the transport.
            self.handle_parser_exception()
            return
        # The frames came in the same read as the upgrade request, before the
        # 101 response. There is no open connection to send a close frame on,
        # so close the transport. connection_lost tells the app.
        self.transport.write(b"".join(self.conn.data_to_send()))
        self.close_sent = True
        self.transport.close()
