"""AudacyService — orchestrates catalog lookup, URL resolution, and mpd playback.

A module-level singleton instance is held so that state (current station,
last-played) survives across MQTT tool_call re-invocations within the same
node process. The Pandora package uses the same pattern; without it, each
voice turn loses context.

This module does not import anything from `commands.audacy.command`; the
command imports the service. That dependency direction lets us keep
`is_playing()` queryable from `pre_route()` without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from .mpd_client import MPDClient, MPDError
from .station_catalog import Station, resolve as _catalog_resolve
from .stream_resolver import stream_urls


@dataclass
class PlaybackError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


class AudacyService:
    def __init__(self, mpd: MPDClient | None = None) -> None:
        self._mpd = mpd or MPDClient()
        self._current: Station | None = None

    # ------------------------------------------------------------------
    # Catalog passthrough (so command.py only depends on the service)
    # ------------------------------------------------------------------

    @staticmethod
    def catalog_resolves(query: str) -> bool:
        """Cheap yes/no for the pre_route gate. Doesn't hit the network."""
        return _catalog_resolve(query) is not None

    @staticmethod
    def resolve_station(query: str) -> Station | None:
        return _catalog_resolve(query)

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def is_playing(self) -> bool:
        """True iff mpd reports it's actively playing an Audacy stream.

        Used by pre_route to gate ambiguous "stop"/"pause" utterances —
        we only claim them when Audacy is what the user means to stop.
        Falls back to False on any error so a broken mpd never causes us
        to silently steal commands.
        """
        if self._current is None:
            return False
        try:
            status = self._mpd.status()
        except (MPDError, OSError):
            return False
        return status.state == "play" and status.current_url is not None

    def mpd_alive(self) -> bool:
        """True iff mpd is reachable on its control socket.

        Used by ``_handle_play`` to fail fast with a clear voice response
        when the local mpd daemon isn't running — otherwise the user hears
        a confident "Playing X" TTS confirmation, then silence (the
        deferred-play callback hits ECONNREFUSED and only logs). See
        prds/audacy-mpd-install-gap.md for the original incident.
        """
        return self._mpd.ping()

    def now_playing(self) -> Station | None:
        return self._current if self.is_playing() else None

    # ------------------------------------------------------------------
    # Playback verbs
    # ------------------------------------------------------------------

    def play_station(self, query: str) -> Station:
        """Resolve `query` against the catalog and start playback.

        Walks the URL candidate list — AmperWave AAC → MP3 → StreamTheWorld —
        and stops at the first one mpd accepts and actually starts. We treat
        a "play" command that doesn't transition mpd to state=play within a
        short window as a failure (the URL silently 404'd or codec was wrong)
        and move to the next candidate.

        Raises PlaybackError if no URL worked — caller turns this into a
        CommandResponse.error_response so the user hears why.
        """
        station = _catalog_resolve(query)
        if station is None:
            raise PlaybackError(
                f"I don't have '{query}' in my Audacy station list",
            )

        last_error: str | None = None
        for url in stream_urls(station):
            try:
                self._mpd.play_stream(url)
            except (MPDError, OSError) as e:
                last_error = f"mpd refused {url}: {e}"
                continue

            # Verify mpd actually started — a 404'd stream returns OK to `play`
            # but state stays "stop". One short status read is enough; we don't
            # block in a poll loop since the voice loop is on the hot path.
            try:
                status = self._mpd.status()
            except (MPDError, OSError) as e:
                last_error = f"mpd status check failed: {e}"
                continue

            if status.state == "play":
                self._current = station
                return station

            last_error = f"stream did not start (state={status.state}): {url}"

        raise PlaybackError(
            f"I couldn't reach the stream for {station.display_name}. "
            f"({last_error or 'all candidates failed'})"
        )

    def stop(self) -> None:
        """Stop playback. Idempotent — safe to call when nothing's playing."""
        try:
            self._mpd.stop()
        except (MPDError, OSError):
            pass
        self._current = None

    def healthcheck(self) -> bool:
        return self._mpd.ping()


# Module-level singleton — see module docstring for rationale.
_service: AudacyService | None = None


def get_service() -> AudacyService:
    global _service
    if _service is None:
        _service = AudacyService()
    return _service
