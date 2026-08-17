from __future__ import annotations

import queue
import socket
import threading


class SyncClient:
    """
    Background connection to a patched SuperTuxKart's --sync-port
    listener (patches/PROTOCOL.md).

    Owns a thread that does the actual socket I/O and nothing else -
    outgoing commands arrive through a Queue fed by the GUI thread,
    incoming ones are handed to `post`, which the caller wires to the
    App's existing self.q/_poll() channel.  That channel is the only
    thing this ever touches from the GUI's side: nothing here calls into
    Tk directly, which is not safe off the main thread.

    Retries the connection while running, since STK may not be up yet
    (or may restart) - the protocol is explicit that both sides must
    tolerate that.
    """
    RETRY_SECONDS = 2.0

    def __init__(self, host: str, port: int, post):
        self.host = host
        self.port = port
        self.post = post           # post(kind: str, payload) -> queued
        self._out: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._out.put(None)        # unstick a thread waiting to send

    def send(self, line: str):
        self._out.put(line)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._connect_once()
            except OSError as exc:
                self.post("sync-status", ("error", str(exc)))
            if self._stop.is_set():
                return
            self.post("sync-status", ("retrying", None))
            if self._stop.wait(self.RETRY_SECONDS):
                return

    def _connect_once(self):
        self.post("sync-status", ("connecting", None))
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            sock.settimeout(0.2)
            self.post("sync-status", ("connected", None))
            buf = b""
            while not self._stop.is_set():
                try:
                    while True:
                        line = self._out.get_nowait()
                        if line is None:
                            return
                        sock.sendall((line + "\n").encode("ascii"))
                except queue.Empty:
                    pass
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return          # STK closed the connection
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._handle(raw.decode("ascii", "replace").strip())
        finally:
            sock.close()

    def _handle(self, line: str):
        # Unknown verbs are ignored, not an error - patches/PROTOCOL.md,
        # so a mismatched version on either side degrades gracefully.
        if line.startswith("STATE "):
            parts = line.split()
            if len(parts) != 4:
                return
            try:
                t, playing, rate = (float(parts[1]), parts[2] == "1",
                                    float(parts[3]))
            except ValueError:
                return
            self.post("sync-state", (t, playing, rate))
        elif line.startswith("REPLAY "):
            self.post("sync-replay", line[len("REPLAY "):])
        elif line.startswith("DURATION "):
            try:
                self.post("sync-duration", float(line[len("DURATION "):]))
            except ValueError:
                pass
        elif line == "BYE":
            self.post("sync-bye", None)
