"""Relays OpenTuner's "Jetson stream" to the RX page - no decoding here.

OpenTuner (Windows PC) re-encodes tuner 1 to browser-ready fragmented MP4
(H.264 with an IDR at every fragment start, optional AAC) and pushes it to
us as a TCP client on RX_TCP_PORT. One TCP connection = one station
("session"): it starts with ftyp+moov (the init segment), then one moof+mdat
fragment per second. OpenTuner closes it on lost lock / retune and opens a
new one on the next lock; a new connection always replaces an old one.

We cache the current session's init segment and hand every viewer the init
followed by whole fragments. When the session ends (or a new one starts),
every viewer's HTTP stream is ended so the page reconnects and resets its
player with the new init - a new moov must never go into an old
SourceBuffer. The TCP reader never waits for viewers: a viewer that falls
behind loses its oldest queued fragments instead.
"""

import queue
import socket
import struct
import threading
import time

RX_TCP_PORT = 5001
VIEWER_QUEUE_FRAGMENTS = 8   # ~8 s at one fragment per second
VIEWER_IDLE_TIMEOUT_S = 15
_END = object()              # queue sentinel: session over

_lock = threading.Lock()
_session = {"id": 0, "init": None, "peer": None, "since": None}
_viewers = set()             # of (session_id, queue.Queue)
_current_conn = None


def _end_viewers():
    """Caller holds _lock."""
    for _session_id, viewer_queue in list(_viewers):
        _put_dropping_oldest(viewer_queue, _END)
    _viewers.clear()


def _put_dropping_oldest(viewer_queue, item):
    while True:
        try:
            viewer_queue.put_nowait(item)
            return
        except queue.Full:
            try:
                viewer_queue.get_nowait()
            except queue.Empty:
                pass


def _read_boxes(conn):
    """Yields (type, bytes) for each top-level MP4 box until EOF."""
    buffer = b""
    while True:
        while len(buffer) >= 8:
            size, box_type = struct.unpack(">I4s", buffer[:8])
            if size == 1:  # 64-bit largesize follows
                if len(buffer) < 16:
                    break
                size = struct.unpack(">Q", buffer[8:16])[0]
            if size < 8:
                raise ValueError("bad MP4 box size {}".format(size))
            if len(buffer) < size:
                break
            yield box_type, buffer[:size]
            buffer = buffer[size:]
        chunk = conn.recv(65536)
        if not chunk:
            return
        buffer += chunk


def _handle_connection(conn, peer):
    global _current_conn
    ftyp = None
    fragment = []
    my_session = None
    try:
        for box_type, box in _read_boxes(conn):
            if box_type == b"ftyp":
                ftyp = box
            elif box_type == b"moov":
                with _lock:
                    if _current_conn is not conn:
                        return
                    _end_viewers()
                    _session["id"] += 1
                    _session.update(init=(ftyp or b"") + box, peer=peer[0], since=time.time())
                    my_session = _session["id"]
            elif my_session is not None:
                fragment.append(box)
                if box_type == b"mdat":
                    data = b"".join(fragment)
                    fragment = []
                    with _lock:
                        for session_id, viewer_queue in list(_viewers):
                            if session_id == my_session:
                                _put_dropping_oldest(viewer_queue, data)
    except (OSError, ValueError):
        pass
    finally:
        conn.close()
        with _lock:
            if _current_conn is conn:
                _current_conn = None
                _session.update(init=None, peer=None, since=None)
                _end_viewers()


def _accept_loop(listener):
    global _current_conn
    while True:
        conn, peer = listener.accept()
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        with _lock:
            old, _current_conn = _current_conn, conn
        if old is not None:
            # New connection wins - unblocks the old reader thread.
            try:
                old.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        threading.Thread(target=_handle_connection, args=(conn, peer), daemon=True).start()


def start():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", RX_TCP_PORT))
    listener.listen(2)
    threading.Thread(target=_accept_loop, args=(listener,), daemon=True).start()


def status():
    with _lock:
        live = _session["init"] is not None
        return {
            "receiving": live,
            "peer": _session["peer"],
            "seconds": round(time.time() - _session["since"]) if live else None,
        }


def stream():
    """Generator for one viewer, or None when no station is being received.
    Yields the init segment, then whole fragments, and ends when the
    session ends (the page then reconnects for the next one)."""
    viewer_queue = queue.Queue(maxsize=VIEWER_QUEUE_FRAGMENTS)
    with _lock:
        if _session["init"] is None:
            return None
        entry = (_session["id"], viewer_queue)
        init = _session["init"]
        _viewers.add(entry)

    def generate():
        try:
            yield init
            while True:
                try:
                    item = viewer_queue.get(timeout=VIEWER_IDLE_TIMEOUT_S)
                except queue.Empty:
                    return
                if item is _END:
                    return
                yield item
        finally:
            with _lock:
                _viewers.discard(entry)

    return generate()
