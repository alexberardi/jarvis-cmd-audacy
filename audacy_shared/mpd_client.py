"""Minimal MPD client — pure stdlib, blocking sockets.

We only need the verbs Audacy uses: clear queue, add stream URL, play, stop,
status. No password support (mpd runs on localhost), no idle subscriptions,
no playlist management.

Why not python-mpd2? It's a fine library but it adds a runtime dep for what
amounts to ~80 lines of stdlib. Keeping this self-contained keeps the
Pantry-validated dependency list short.

Connection lifecycle:
  - Each operation opens a fresh connection, sends commands, reads the
    response, and closes. mpd handles many short connections fine and we
    don't have to manage broken-pipe recovery across voice turns.
  - Timeouts are short (2s connect, 5s read) — if mpd isn't responsive on
    localhost, we want to fail fast rather than block the voice loop.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass


class MPDError(RuntimeError):
    """Raised when mpd returns an ACK (error response) or the socket fails."""


@dataclass
class MPDStatus:
    state: str  # "play" | "stop" | "pause"
    volume: int | None
    current_url: str | None


class MPDClient:
    def __init__(self, host: str = "localhost", port: int = 6600,
                 connect_timeout: float = 2.0, read_timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    # ------------------------------------------------------------------
    # Public verbs
    # ------------------------------------------------------------------

    def play_stream(self, url: str) -> None:
        """Replace the queue with a single stream URL and start playback.

        mpd treats internet radio streams the same as files — once added,
        `play` begins streaming. We `clear` first so we never accidentally
        append to a stale queue.
        """
        self._exchange([
            ("clear", []),
            ("add", [url]),
            ("play", []),
        ])

    def stop(self) -> None:
        self._exchange([("stop", [])])

    def status(self) -> MPDStatus:
        """Snapshot of mpd's playback state."""
        lines = self._exchange([("status", []), ("currentsong", [])])
        kv: dict[str, str] = {}
        for line in lines:
            if ":" in line:
                k, _, v = line.partition(":")
                kv[k.strip()] = v.strip()
        state = kv.get("state", "stop")
        volume_raw = kv.get("volume")
        volume = int(volume_raw) if volume_raw and volume_raw.lstrip("-").isdigit() else None
        # mpd reports the playing source under "file:" (for streams, that's the URL)
        current_url = kv.get("file")
        return MPDStatus(state=state, volume=volume, current_url=current_url)

    def ping(self) -> bool:
        """True iff mpd is reachable and responsive."""
        try:
            self._exchange([("ping", [])])
            return True
        except (MPDError, OSError):
            return False

    # ------------------------------------------------------------------
    # Wire protocol
    # ------------------------------------------------------------------

    def _exchange(self, commands: list[tuple[str, list[str]]]) -> list[str]:
        """Open connection, send commands, return aggregated response lines.

        Single-command requests use plain mode; multi-command requests use
        command_list mode so mpd evaluates them atomically.
        """
        sock = socket.create_connection(
            (self._host, self._port), timeout=self._connect_timeout,
        )
        try:
            sock.settimeout(self._read_timeout)
            self._read_greeting(sock)

            payload = self._build_payload(commands)
            sock.sendall(payload)
            return self._read_until_terminator(sock)
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    @staticmethod
    def _build_payload(commands: list[tuple[str, list[str]]]) -> bytes:
        def _quote(arg: str) -> str:
            # mpd protocol: quote arguments with backslash escapes for " and \
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

        def _line(verb: str, args: list[str]) -> str:
            if not args:
                return verb
            return verb + " " + " ".join(_quote(a) for a in args)

        if len(commands) == 1:
            return (_line(*commands[0]) + "\n").encode("utf-8")

        body = "command_list_begin\n"
        for verb, args in commands:
            body += _line(verb, args) + "\n"
        body += "command_list_end\n"
        return body.encode("utf-8")

    @staticmethod
    def _read_greeting(sock: socket.socket) -> None:
        # mpd greets with "OK MPD <version>\n" on connect; consume it.
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise MPDError("mpd closed connection during greeting")
            buf += chunk
        first_line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        if not first_line.startswith("OK MPD"):
            raise MPDError(f"unexpected mpd greeting: {first_line!r}")

    @staticmethod
    def _read_until_terminator(sock: socket.socket) -> list[str]:
        """Read response lines until 'OK' or 'ACK ...' terminator."""
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                raise MPDError("mpd closed connection mid-response")
            buf += chunk
            if b"\nOK\n" in buf or buf.endswith(b"OK\n"):
                break
            if b"\nACK " in buf or buf.startswith(b"ACK "):
                break

        text = buf.decode("utf-8", errors="replace").rstrip("\n")
        lines = text.split("\n")
        # Strip terminator + surface ACK errors
        if lines and lines[-1] == "OK":
            return lines[:-1]
        if lines and lines[-1].startswith("ACK "):
            raise MPDError(lines[-1])
        return lines
