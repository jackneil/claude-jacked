"""Unit tests for jacked.api.log_capture — ring buffer + handler."""

import asyncio
import logging
import threading

from jacked.api.log_capture import MAX_FLUSH_ENTRIES, ServerLogBuffer, _format_entry


# ------------------------------------------------------------------
# _format_entry
# ------------------------------------------------------------------


def test_format_entry_basic():
    """Converts a LogRecord into the expected dict shape.

    >>> record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    >>> entry = _format_entry(record)
    >>> entry["level"]
    'INFO'
    >>> entry["msg"]
    'hello'
    >>> entry["logger"]
    'test'
    >>> "ts" in entry
    True
    """
    record = logging.LogRecord("test.module", logging.INFO, "", 0, "hello world", (), None)
    entry = _format_entry(record)
    assert entry["level"] == "INFO"
    assert entry["msg"] == "hello world"
    assert entry["logger"] == "test.module"
    assert "ts" in entry
    assert entry["ts"].endswith("+00:00"), "timestamp must be UTC"


def test_format_entry_truncation():
    """Long messages are truncated to MAX_MSG_LENGTH.

    >>> record = logging.LogRecord("t", logging.INFO, "", 0, "x" * 5000, (), None)
    >>> entry = _format_entry(record)
    >>> len(entry["msg"]) < 5000
    True
    """
    record = logging.LogRecord("t", logging.INFO, "", 0, "x" * 5000, (), None)
    entry = _format_entry(record)
    assert len(entry["msg"]) <= 4097  # 4096 + ellipsis char
    assert entry["msg"].endswith("…")


def test_format_entry_with_args():
    """Record with % formatting args is resolved.

    >>> record = logging.LogRecord("t", logging.INFO, "", 0, "val=%d", (42,), None)
    >>> _format_entry(record)["msg"]
    'val=42'
    """
    record = logging.LogRecord("t", logging.INFO, "", 0, "val=%d", (42,), None)
    entry = _format_entry(record)
    assert entry["msg"] == "val=42"


# ------------------------------------------------------------------
# ServerLogBuffer — basic operations
# ------------------------------------------------------------------


def test_empty_buffer():
    """New buffer returns empty list.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> buf.get_recent()
    []
    """
    buf = ServerLogBuffer(maxlen=10)
    assert buf.get_recent() == []
    assert buf.buffer_size == 10


def test_get_recent_respects_limit():
    """get_recent returns at most *limit* entries.

    >>> buf = ServerLogBuffer(maxlen=100)
    >>> for i in range(20):
    ...     buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None))
    >>> len(buf.get_recent(limit=5))
    5
    """
    buf = ServerLogBuffer(maxlen=100)
    for i in range(20):
        buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None))
    result = buf.get_recent(limit=5)
    assert len(result) == 5
    # Should be the 5 most recent
    assert result[0]["msg"] == "m15"
    assert result[-1]["msg"] == "m19"


def test_get_recent_full_buffer():
    """get_recent with limit >= buffer size returns all entries.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> for i in range(5):
    ...     buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None))
    >>> len(buf.get_recent(limit=100))
    5
    """
    buf = ServerLogBuffer(maxlen=10)
    for i in range(5):
        buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None))
    result = buf.get_recent(limit=100)
    assert len(result) == 5


# ------------------------------------------------------------------
# Deque eviction
# ------------------------------------------------------------------


def test_deque_eviction():
    """Oldest entries evicted when buffer is full.

    >>> buf = ServerLogBuffer(maxlen=3)
    >>> for i in range(5):
    ...     buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None))
    >>> msgs = [e["msg"] for e in buf.get_recent()]
    >>> msgs
    ['m2', 'm3', 'm4']
    """
    buf = ServerLogBuffer(maxlen=3)
    for i in range(5):
        buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None))
    result = buf.get_recent()
    assert len(result) == 3
    assert [e["msg"] for e in result] == ["m2", "m3", "m4"]


# ------------------------------------------------------------------
# Handler emit
# ------------------------------------------------------------------


def test_handler_emit():
    """Handler.emit() appends formatted entry to the buffer.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> record = logging.LogRecord("app", logging.WARNING, "", 0, "oops", (), None)
    >>> buf.handler.emit(record)
    >>> entries = buf.get_recent()
    >>> len(entries)
    1
    >>> entries[0]["level"]
    'WARNING'
    """
    buf = ServerLogBuffer(maxlen=10)
    record = logging.LogRecord("app", logging.WARNING, "", 0, "oops", (), None)
    buf.handler.emit(record)
    entries = buf.get_recent()
    assert len(entries) == 1
    assert entries[0]["level"] == "WARNING"
    assert entries[0]["msg"] == "oops"
    assert entries[0]["logger"] == "app"


def test_handler_via_logger():
    """Handler captures log messages when attached to a Python logger.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> logger = logging.getLogger("test_handler_via_logger")
    >>> logger.addHandler(buf.handler)
    >>> logger.setLevel(logging.DEBUG)
    >>> logger.info("test message")
    >>> entries = buf.get_recent()
    >>> len(entries)
    1
    """
    buf = ServerLogBuffer(maxlen=10)
    test_logger = logging.getLogger("test_handler_via_logger_" + str(id(buf)))
    test_logger.addHandler(buf.handler)
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False
    try:
        test_logger.info("test message")
        entries = buf.get_recent()
        assert len(entries) == 1
        assert entries[0]["msg"] == "test message"
    finally:
        test_logger.removeHandler(buf.handler)


# ------------------------------------------------------------------
# Recursive logging guard
# ------------------------------------------------------------------


def test_websocket_logger_skips_broadcast_scheduling():
    """Events from jacked.api.websocket logger only buffer, don't schedule.

    The _maybe_schedule_flush is not called for these loggers. Without
    loop/registry set, this is a no-op anyway, but we verify the entry
    is still buffered.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> record = logging.LogRecord("jacked.api.websocket", logging.DEBUG, "", 0, "prune", (), None)
    >>> buf._handle_record(record)
    >>> len(buf.get_recent())
    1
    """
    buf = ServerLogBuffer(maxlen=10)
    record = logging.LogRecord("jacked.api.websocket", logging.DEBUG, "", 0, "prune", (), None)
    buf._handle_record(record)
    # Entry is buffered
    assert len(buf.get_recent()) == 1
    # But flush was not scheduled (no loop set, so _flush_scheduled stays False)
    assert not buf._flush_scheduled


def test_broadcasting_flag_skips_schedule():
    """When _broadcasting is True, new records don't schedule flush.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> buf._broadcasting = True
    >>> buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, "m", (), None))
    >>> buf._flush_scheduled
    False
    """
    buf = ServerLogBuffer(maxlen=10)
    buf._broadcasting = True
    buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None))
    # Entry is buffered
    assert len(buf.get_recent()) == 1
    # But no flush was scheduled
    assert not buf._flush_scheduled


# ------------------------------------------------------------------
# Thread safety
# ------------------------------------------------------------------


def test_concurrent_append_and_read():
    """Concurrent appends and reads don't crash.

    >>> # Verified via threading test below
    """
    buf = ServerLogBuffer(maxlen=100)
    errors = []

    def writer():
        for i in range(200):
            try:
                buf._handle_record(
                    logging.LogRecord("w", logging.INFO, "", 0, f"w{i}", (), None)
                )
            except Exception as e:
                errors.append(e)

    def reader():
        for _ in range(200):
            try:
                buf.get_recent(limit=50)
            except Exception as e:
                errors.append(e)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == [], f"Concurrent access errors: {errors}"
    # Buffer should have entries (up to maxlen)
    assert len(buf.get_recent(limit=200)) > 0


# ------------------------------------------------------------------
# Wiring lifecycle
# ------------------------------------------------------------------


def test_set_loop_and_registry():
    """set_loop/set_registry/detach lifecycle.

    >>> buf = ServerLogBuffer(maxlen=5)
    >>> buf._loop is None
    True
    >>> buf._registry is None
    True
    """
    buf = ServerLogBuffer(maxlen=5)
    assert buf._loop is None
    assert buf._registry is None

    # Simulate wiring (use a mock loop)
    class FakeLoop:
        pass

    class FakeRegistry:
        pass

    buf.set_loop(FakeLoop())
    buf.set_registry(FakeRegistry())
    assert buf._loop is not None
    assert buf._registry is not None

    buf.detach()
    assert buf._loop is None
    assert buf._registry is None


# ------------------------------------------------------------------
# Buffer size property
# ------------------------------------------------------------------


def test_buffer_size_property():
    """buffer_size reflects the maxlen.

    >>> ServerLogBuffer(maxlen=500).buffer_size
    500
    """
    assert ServerLogBuffer(maxlen=500).buffer_size == 500
    assert ServerLogBuffer(maxlen=2000).buffer_size == 2000


# ------------------------------------------------------------------
# Detach clears pending
# ------------------------------------------------------------------


def test_detach_clears_pending():
    """detach() drains the pending list so stale entries aren't broadcast.

    >>> buf = ServerLogBuffer(maxlen=10)
    >>> buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, "m", (), None))
    >>> len(buf._pending) > 0
    True
    >>> buf.detach()
    >>> buf._pending
    []
    """
    buf = ServerLogBuffer(maxlen=10)
    buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, "m", (), None))
    assert len(buf._pending) > 0

    buf.detach()
    assert buf._pending == []
    assert buf._loop is None
    assert buf._registry is None
    # Buffer itself is still intact (entries not lost)
    assert len(buf.get_recent()) == 1


# ------------------------------------------------------------------
# Broadcast failure logging
# ------------------------------------------------------------------


def test_flush_async_logs_broadcast_failure(caplog):
    """_flush_async logs broadcast errors instead of swallowing silently.

    >>> # Verified via caplog integration test below
    """
    buf = ServerLogBuffer(maxlen=10)
    buf._handle_record(logging.LogRecord("t", logging.INFO, "", 0, "m", (), None))

    class FailingRegistry:
        async def broadcast(self, event_type, payload):
            raise RuntimeError("ws send failed")

    buf.set_registry(FailingRegistry())

    with caplog.at_level(logging.DEBUG, logger="jacked.api.log_capture"):
        asyncio.run(buf._flush_async())

    assert "Server log broadcast failed" in caplog.text
    # Broadcasting flag is cleared even on failure
    assert not buf._broadcasting


# ------------------------------------------------------------------
# Flush burst cap
# ------------------------------------------------------------------


def test_flush_caps_at_max_flush_entries():
    """Flush broadcasts at most MAX_FLUSH_ENTRIES entries (newest).

    >>> # Verified via async integration test below
    """
    buf = ServerLogBuffer(maxlen=MAX_FLUSH_ENTRIES + 200)
    for i in range(MAX_FLUSH_ENTRIES + 100):
        buf._handle_record(
            logging.LogRecord("t", logging.INFO, "", 0, f"m{i}", (), None)
        )

    broadcast_payloads = []

    class SpyRegistry:
        async def broadcast(self, event_type, payload):
            broadcast_payloads.append(payload)

    buf.set_registry(SpyRegistry())
    asyncio.run(buf._flush_async())

    assert len(broadcast_payloads) == 1
    entries = broadcast_payloads[0]["entries"]
    assert len(entries) == MAX_FLUSH_ENTRIES
    # Should be the newest entries (tail)
    assert entries[-1]["msg"] == f"m{MAX_FLUSH_ENTRIES + 99}"


# ------------------------------------------------------------------
# RuntimeError in _maybe_schedule_flush
# ------------------------------------------------------------------


def test_maybe_schedule_flush_runtime_error():
    """_flush_scheduled is reset when call_soon_threadsafe raises RuntimeError.

    >>> buf = ServerLogBuffer(maxlen=5)
    >>> buf._flush_scheduled
    False
    """
    buf = ServerLogBuffer(maxlen=5)

    class ClosedLoop:
        def call_soon_threadsafe(self, callback):
            raise RuntimeError("Event loop is closed")

    class FakeRegistry:
        pass

    buf.set_loop(ClosedLoop())
    buf.set_registry(FakeRegistry())

    buf._maybe_schedule_flush()
    # Flag must be reset to False despite RuntimeError
    assert not buf._flush_scheduled


# ------------------------------------------------------------------
# Detach resets flags
# ------------------------------------------------------------------


def test_detach_resets_flush_and_broadcasting_flags():
    """detach() resets _flush_scheduled and _broadcasting flags.

    >>> buf = ServerLogBuffer(maxlen=5)
    >>> buf._flush_scheduled = True
    >>> buf._broadcasting = True
    >>> buf.detach()
    >>> buf._flush_scheduled
    False
    >>> buf._broadcasting
    False
    """
    buf = ServerLogBuffer(maxlen=5)
    buf._flush_scheduled = True
    buf._broadcasting = True

    buf.detach()
    assert not buf._flush_scheduled
    assert not buf._broadcasting
