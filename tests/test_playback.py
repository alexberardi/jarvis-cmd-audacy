"""The IJarvisMusicCommand side: what the node's display, the app and the built-in transport
commands see. Every state is read back from mpd, here a fake one speaking mpd's protocol."""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from audacy_shared import audacy_service
from audacy_shared.audacy_service import AudacyService
from audacy_shared.mpd_client import MPDClient
from audacy_shared.station_catalog import all_stations
from commands.audacy.command import AudacyCommand

STATION = all_stations()[0]


class FakeMPD:
    """Just enough of mpd: status, currentsong, pause and setvol, one command per connection."""

    def __init__(self) -> None:
        self.state, self.volume, self.title, self.file = "play", "40", "Artist - Song", "https://stream"
        self.sent: list[str] = []
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"OK MPD 0.23.5\n")
                data = b""
                while not (data.endswith(b"command_list_end\n") or (data.endswith(b"\n") and b"command_list_begin" not in data)):
                    data += conn.recv(4096)
                lines = [ln for ln in data.decode().splitlines() if not ln.startswith("command_list")]
                self.sent += lines
                out = ""
                for ln in lines:
                    verb, _, arg = ln.partition(" ")
                    arg = arg.strip('"')
                    if verb == "status":
                        out += f"volume: {self.volume}\nstate: {self.state}\n"
                    elif verb == "currentsong" and self.file:
                        out += f"file: {self.file}\n" + (f"Title: {self.title}\n" if self.title else "")
                    elif verb == "pause":
                        self.state = "pause" if arg == "1" else "play"
                    elif verb == "setvol":
                        self.volume = arg
                conn.sendall((out + "OK\n").encode())

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def mpd(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMPD]:
    fake = FakeMPD()
    service = AudacyService(MPDClient(port=fake.port))
    service._current = STATION  # what play_station leaves once the stream starts
    monkeypatch.setattr(audacy_service, "_service", service)
    yield fake
    fake.close()


def test_status_reads_mpd(mpd: FakeMPD) -> None:
    assert AudacyCommand().playback_status().to_dict() == {
        "state": "playing", "media_title": "Artist - Song", "media_artist": "", "media_album": "",
        "context": STATION.display_name, "actions": ["play", "pause", "volume"], "volume": 40,
    }


def test_no_title_or_mixer(mpd: FakeMPD) -> None:
    mpd.title, mpd.volume = "", "-1"
    st = AudacyCommand().playback_status()
    assert (st.media_title, st.volume, st.actions) == (STATION.display_name, None, ("play", "pause"))


def test_nothing_playing(mpd: FakeMPD) -> None:
    mpd.state = "stop"
    assert AudacyCommand().playback_status() is None
    mpd.state = "play"
    audacy_service._service._current = None  # mpd playing something Audacy didn't start
    assert AudacyCommand().playback_status() is None


def test_mpd_down_is_nothing_playing(mpd: FakeMPD) -> None:
    with socket.create_server(("127.0.0.1", 0)) as s:
        free = s.getsockname()[1]
    audacy_service._service._mpd = MPDClient(port=free)  # nothing listens there
    assert AudacyCommand().playback_status() is None


def test_controls_read_back(mpd: FakeMPD) -> None:
    cmd = AudacyCommand()
    assert cmd.playback_control("pause").state == "paused"
    assert cmd.playback_control("play").state == "playing"
    assert cmd.playback_control("volume", volume=140).volume == 100
    assert ["pause \"1\"", "pause \"0\"", "setvol \"100\""] == [s for s in mpd.sent if s.split()[0] in ("pause", "setvol")]
    with pytest.raises(ValueError):
        cmd.playback_control("next")


def test_binary_and_report_without_a_node() -> None:
    cmd = AudacyCommand()
    assert cmd.playback_binary == "mpd"
    assert cmd.report() is False  # no node backend in tests; never raises
