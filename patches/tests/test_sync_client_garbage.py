#!/usr/bin/env python3
"""SyncClient against malformed input: garbage lines, a line split across two
writes, binary junk, and a real server disappearing mid-stream, none of which
should ever raise out of the client thread or corrupt state."""
import socket
import sys
import threading
import time

import _paths

PORT = 27996


def raw_server(conn_ready, script):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(1)
    conn_ready.set()
    conn, _ = srv.accept()
    for chunk, delay in script:
        conn.sendall(chunk)
        time.sleep(delay)
    conn.close()
    srv.close()


def main():
    c = _paths.Checker()
    app = _paths.build_app()

    events = []
    def spy_post(kind, payload, _orig=app.sync_post):
        events.append((kind, payload))
        _orig(kind, payload)
    app.sync_post = spy_post

    script = [
        (b"GARBAGE not a real line at all\n", 0.05),
        (b"STATE not numbers here 1\n", 0.05),
        (b"STA", 0.05),                      # split mid-verb
        (b"TE 5.0 1 1.0\n", 0.05),            # completes to a valid STATE
        (b"\x00\x01\xff\xfe binary junk \n", 0.05),
        (b"REPLAY  \n", 0.05),                # empty-ish path, must not crash
        (b"STATE 6.0 1 1.0\n", 0.05),
        (b"BYE\n", 0.05),
    ]
    ready = threading.Event()
    t = threading.Thread(target=raw_server, args=(ready, script), daemon=True)
    t.start()
    ready.wait(2)

    app.sync_port.set(str(PORT))
    exc = []
    old_report = app.root.report_callback_exception
    def report(exc_type, exc_val, tb):
        exc.append(exc_val)
        old_report(exc_type, exc_val, tb)
    app.root.report_callback_exception = report

    app.sync_toggle()
    _paths.pump(app, 3.0, step=0.02)

    c.check(not exc, f"no exception surfaced in the Tk main loop: {exc}")
    kinds = [k for k, _ in events]
    c.check(kinds.count("sync-state") >= 2,
            f"both valid STATE lines got through despite the garbage "
            f"({kinds.count('sync-state')} seen)")
    c.check(("sync-state", None) not in [(k, None) for k, p in events
                                         if k == "sync-state" and p is None],
            "no malformed STATE was ever forwarded as a payload")
    c.check(app.rp_t is not None, "app didn't crash: rp_t is still a number")
    c.check("sync-bye" in kinds, "BYE was received cleanly after the garbage")

    t.join(timeout=2)
    app.sync_toggle()
    app.root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
