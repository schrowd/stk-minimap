#!/usr/bin/env python3
"""
A fake SuperTuxKart sync listener, speaking the exact wire protocol the real
ReplaySyncServer implements (patches/replay_sync_server.cpp, verified against
the real running binary in the previous session).  Used to test
stk_minimap's SyncClient/App integration without the full game.

Usage: fake_stk.py <port> <replay_basename> <duration> [--no-replay-line]
"""
import socket
import sys
import threading
import time

HOST = "127.0.0.1"


class FakeSTK:
    def __init__(self, port, replay_name, duration, send_replay=True):
        self.port = port
        self.replay_name = replay_name
        self.send_replay = send_replay
        self.t = 0.0
        self.playing = True
        self.rate = 1.0
        self.duration = duration
        self.lock = threading.Lock()
        self.clients = []
        self.stop = threading.Event()

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, self.port))
        srv.listen(4)
        srv.settimeout(0.2)
        threading.Thread(target=self._clock, daemon=True).start()
        print(f"fake_stk: listening on {HOST}:{self.port}", flush=True)
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(0.2)
            self._greet(conn)
            threading.Thread(target=self._client_loop, args=(conn,),
                             daemon=True).start()
        srv.close()

    def _clock(self):
        last = time.monotonic()
        while not self.stop.is_set():
            time.sleep(1.0 / 60)
            now = time.monotonic()
            dt = now - last
            last = now
            with self.lock:
                if self.playing:
                    self.t += dt * self.rate
                    if self.t >= self.duration:
                        self.t = self.duration
                        self.playing = False

    def _greet(self, conn):
        lines = [f"HELLO stk 1.5 1"]
        if self.send_replay:
            lines.append(f"REPLAY {self.replay_name}")
        lines.append(f"DURATION {self.duration:.3f}")
        with self.lock:
            lines.append(f"STATE {self.t:.3f} {1 if self.playing else 0} "
                         f"{self.rate:.3f}")
        for l in lines:
            self._send(conn, l)

    def _send(self, conn, line):
        try:
            conn.sendall((line + "\n").encode("ascii"))
        except OSError:
            pass

    def _state_line(self):
        with self.lock:
            return f"STATE {self.t:.3f} {1 if self.playing else 0} " \
                   f"{self.rate:.3f}"

    def load_replay(self, name, duration, at_time=0.0):
        """Mimics the real server spotting a new replay in World::reset() and
        announcing it to everyone already connected."""
        with self.lock:
            self.replay_name = name
            self.duration = duration
            self.t = at_time
            self.playing = True
            self.send_replay = True
        for c in list(self.clients):
            self._send(c, f"REPLAY {name}\nDURATION {duration:.3f}")

    def _client_loop(self, conn):
        self.clients.append(conn)
        buf = b""
        last_beat = time.monotonic()
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now - last_beat >= 0.1:
                    self._send(conn, self._state_line())
                    last_beat = now
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._handle(conn, raw.decode().strip())
        finally:
            conn.close()
            if conn in self.clients:
                self.clients.remove(conn)

    def _handle(self, conn, line):
        print(f"fake_stk: recv {line!r}", flush=True)
        with self.lock:
            if line == "PLAY":
                if self.t >= self.duration:
                    self.t = 0.0
                self.playing = True
            elif line == "PAUSE":
                self.playing = False
            elif line == "PING":
                pass
            elif line.startswith("SEEK "):
                try:
                    t = float(line[5:])
                except ValueError:
                    return
                self.t = max(0.0, min(t, self.duration))
            elif line.startswith("RATE "):
                try:
                    r = float(line[5:])
                except ValueError:
                    return
                self.rate = max(0.01, min(r, 16.0))
        self._send(conn, self._state_line())


def _stdin_control(srv):
    """Lines on stdin: "LOAD <name> <duration> [at_time]"."""
    for line in sys.stdin:
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "LOAD":
            at = float(parts[3]) if len(parts) > 3 else 0.0
            srv.load_replay(parts[1], float(parts[2]), at)
            print(f"fake_stk: loaded {parts[1]}", flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1])
    name = sys.argv[2]
    dur = float(sys.argv[3])
    send_replay = "--no-replay-line" not in sys.argv
    srv = FakeSTK(port, name, dur, send_replay)
    threading.Thread(target=_stdin_control, args=(srv,), daemon=True).start()
    srv.serve()
