"""Bounded request correlation for one authenticated Version 3 session."""

import asyncio

from ..protocol.constants import FrameType
from ..protocol.frame import Frame


class RequestError(RuntimeError):
    """Raised when a request cannot receive a valid response."""


class RequestManager:
    """Track a bounded set of in-flight RPC responses by request ID."""

    def __init__(self, limit: int = 128):
        self._limit = limit
        self._pending: dict[str, asyncio.Future[Frame]] = {}

    @property
    def count(self) -> int:
        """Return the current number of requests awaiting a response."""
        return len(self._pending)

    def create(self, request_id: str) -> asyncio.Future[Frame]:
        """Reserve a response future for a newly sent request.

        Args:
            request_id: Unique outgoing frame identifier.

        Returns:
            Future to be resolved by the matching response.

        Raises:
            RequestError: If the request limit or ID uniqueness is violated.
        """
        if len(self._pending) >= self._limit:
            raise RequestError("too many pending requests")
        if request_id in self._pending:
            raise RequestError("duplicate pending request ID")
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    def resolve(self, frame: Frame) -> bool:
        """Resolve the matching request only when the response correlates.

        Args:
            frame: Received response frame.

        Returns:
            Whether this frame resolved a pending request.
        """
        if not frame.reply_to:
            return False
        future = self._pending.pop(frame.reply_to, None)
        if future is None or future.done():
            return False
        if frame.type is FrameType.ERROR:
            message = (
                frame.status.message if frame.status is not None else "protocol error"
            )
            future.set_exception(RequestError(message))
        else:
            future.set_result(frame)
        return True

    def cancel(self, request_id: str) -> None:
        """Remove a timed out or cancelled request without retaining it."""
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.cancel()

    def fail_all(self, reason: str) -> None:
        """Fail all pending requests when their connection generation ends."""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(RequestError(reason))
