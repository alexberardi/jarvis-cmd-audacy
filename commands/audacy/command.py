"""Audacy live-radio voice command — play stations from the Audacy network.

Audio playback runs through `mpd` (Music Player Daemon), installed by the
package's apt_packages declaration and re-parented to the jarvis_user via
post_install configure_systemd_service so it shares the node's PulseAudio
session — same pattern the music-assistant package uses for shairport-sync.
This package itself never spawns a subprocess; all playback goes over mpd's
TCP control protocol.
"""

from __future__ import annotations

import re
from typing import Any

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw: str) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: object) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: object) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: object) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: object) -> None:
            self._log.debug(msg)


from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    IJarvisCommand,
    IJarvisSecret,
    JarvisPackage,
    JarvisParameter,
    PreRouteResult,
    RequestInformation,
)

from audacy_shared.audacy_service import AudacyService, PlaybackError, get_service

logger = JarvisLogger(service="cmd.audacy")


# ----------------------------------------------------------------------
# Pre-route patterns
# ----------------------------------------------------------------------

# Tier 1: utterance names "audacy" explicitly — always claim.
_EXPLICIT_ON_AUDACY = re.compile(
    r"^(?:please\s+)?(?:play|put\s+on|listen\s+to|start)\s+(?P<station>.+?)\s+on\s+audacy\s*$",
)
_AUDACY_ALONE = re.compile(
    r"^(?:please\s+)?(?:play|put\s+on|start|open|launch)\s+audacy(?:\s+radio)?\s*$",
)
_STOP_AUDACY_EXPLICIT = re.compile(
    r"^(?:please\s+)?(?:stop|turn\s+off|kill|exit|quit|close)\s+audacy(?:\s+radio)?\s*$",
)

# Tier 2: "play X radio" / "tune in to X" — strong claim, gated by catalog.
# Order matters: longer, more specific patterns first so "play X radio station"
# isn't gobbled by "play X" (handled in Tier 3 anyway, but cleaner to be explicit).
# "station" is accepted as a standalone suffix too — "play WFAN station" is
# every bit as common as "play WFAN radio".
_RADIO_SUFFIX = r"(?:radio(?:\s+station)?|station)"
_RADIO_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"^(?:please\s+)?play\s+(?:the\s+)?(?P<station>.+?)\s+{_RADIO_SUFFIX}\s*$"),
    re.compile(r"^(?:please\s+)?play\s+radio\s+(?P<station>.+?)\s*$"),
    re.compile(rf"^(?:please\s+)?(?:put|turn)\s+on\s+(?:the\s+)?(?P<station>.+?)\s+{_RADIO_SUFFIX}\s*$"),
    re.compile(rf"^(?:please\s+)?tune\s+(?:in\s+)?to\s+(?:the\s+)?(?P<station>.+?)(?:\s+{_RADIO_SUFFIX})?\s*$"),
    re.compile(rf"^(?:please\s+)?listen\s+to\s+(?:the\s+)?(?P<station>.+?)\s+{_RADIO_SUFFIX}\s*$"),
]

# Tier 3: bare "play X" / "put on X" / "turn on X" / "start X" — claim if X
# resolves against the catalog. The catalog's fuzzy match is conservative
# (token-overlap threshold 0.6, trivial words stripped) so generic queries
# like "play Taylor Swift" or "play classical music" don't accidentally
# resolve. "Play WFAN", "play the score", "put on 1010 WINS" all win here.
_BARE_PLAY = re.compile(
    r"^(?:please\s+)?(?:play|put\s+on|turn\s+on|start)\s+(?P<station>.+?)\s*$",
)

# Tier 4: bare stop, gated on Audacy being the active player.
_BARE_STOP_PHRASES = {
    "stop", "pause", "stop radio", "stop the radio",
    "kill the radio", "shut it off", "turn it off",
    "stop playing", "stop the music",
}

# Filler tokens that mean "no specific station" — used in post_process_tool_call
# when the LLM extracted a junk query.
_JUNK_QUERIES = {"", "radio", "audacy", "audacy radio", "music", "something"}


class AudacyCommand(IJarvisCommand):
    """Stream live Audacy stations with voice control."""

    def __init__(self) -> None:
        # No JarvisStorage needed — Audacy v1 has no secrets and no
        # per-user state beyond the in-memory service singleton.
        pass

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def command_name(self) -> str:
        return "audacy"

    @property
    def description(self) -> str:
        return (
            "Stream live radio from the Audacy network. Sports talk, news, "
            "music, and talk stations. Use action='play' with a station name "
            "(e.g. 'WFAN', '670 The Score', 'KROQ'); action='stop' to stop."
        )

    @property
    def associated_service(self) -> str | None:
        return "Audacy"

    @property
    def keywords(self) -> list[str]:
        return [
            "audacy", "radio", "live radio", "station", "tune in",
            "wfan", "kroq", "1010 wins", "the score", "sports radio",
            "news radio", "talk radio",
        ]

    # ------------------------------------------------------------------
    # Parameters & secrets
    # ------------------------------------------------------------------

    @property
    def parameters(self) -> list[JarvisParameter]:
        return [
            JarvisParameter(
                "action",
                "string",
                required=True,
                enum_values=["play", "stop", "now_playing", "stations"],
                description=(
                    "play=tune to a station, stop=stop playback, "
                    "now_playing=what station is on, stations=list available."
                ),
            ),
            JarvisParameter(
                "station",
                "string",
                required=False,
                description=(
                    "Station name, call letters, frequency, or nickname "
                    "(e.g. 'WFAN', 'the score', 'KROQ 106.7'). Only used "
                    "with action='play'."
                ),
            ),
        ]

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return []

    @property
    def required_packages(self) -> list[JarvisPackage]:
        return [JarvisPackage("PyYAML", ">=6.0")]

    # ------------------------------------------------------------------
    # Rules — surface non-obvious phrasing to the LLM router
    # ------------------------------------------------------------------

    @property
    def rules(self) -> list[str]:
        return [
            "Audacy plays LIVE radio stations only — not albums, artists, or playlists.",
            "If user says 'play X radio', 'tune in to X', 'put on X', or 'play X' "
            "where X is a station name or call letters, use action='play' with station=X.",
            "Strip trailing 'radio' or 'station' from the station name "
            "('WFAN radio' → station='WFAN', not 'WFAN radio').",
            "If user says 'stop Audacy', 'turn off the radio', or bare 'stop' "
            "while Audacy is playing, use action='stop'.",
        ]

    @property
    def critical_rules(self) -> list[str]:
        return [
            "Audacy is for LIVE radio. If the user names an artist, album, or "
            "song, do NOT route here — let Spotify/Music Assistant/Pandora handle it.",
        ]

    # ------------------------------------------------------------------
    # Pre-route — deterministic regex bypass of LLM
    # ------------------------------------------------------------------

    def pre_route(self, voice_command: str) -> PreRouteResult | None:
        text = voice_command.strip().lower().rstrip(".!?,;:")
        if not text:
            return None

        service = get_service()

        # Tier 1 — explicit "audacy" in the utterance
        if m := _EXPLICIT_ON_AUDACY.match(text):
            station = m.group("station").strip()
            return PreRouteResult(arguments={"action": "play", "station": station})

        if _AUDACY_ALONE.match(text):
            return PreRouteResult(arguments={"action": "play"})

        if _STOP_AUDACY_EXPLICIT.match(text):
            return PreRouteResult(arguments={"action": "stop"})

        # Tier 2 — "play X radio" / "tune in to X" / "listen to X radio"
        # Catalog gate: only claim if the captured station resolves. Otherwise
        # something like "play classical radio" or "tune in to my morning
        # podcast" reaches the LLM router instead of being silently stolen.
        for pattern in _RADIO_PATTERNS:
            m = pattern.match(text)
            if not m:
                continue
            station = m.group("station").strip()
            if station in _JUNK_QUERIES:
                return PreRouteResult(arguments={"action": "play"})
            if service.catalog_resolves(station):
                return PreRouteResult(
                    arguments={"action": "play", "station": station},
                )
            # No catalog hit — defer (don't claim).
            break  # don't try shorter patterns; we already matched a structural one

        # Tier 3 — bare "play X" / "put on X" — claim if X resolves against the
        # catalog. The catalog's token-overlap threshold (0.6, trivial words
        # stripped) is what stops "play Taylor Swift" / "play classical music"
        # from matching; meanwhile "play WFAN", "play the score", "put on
        # 1010 WINS" all hit because those tokens are in the catalog.
        if m := _BARE_PLAY.match(text):
            candidate = m.group("station").strip()
            if candidate not in _JUNK_QUERIES and service.catalog_resolves(candidate):
                return PreRouteResult(
                    arguments={"action": "play", "station": candidate},
                )

        # Tier 4 — bare stop, gated on Audacy being the active player.
        if text in _BARE_STOP_PHRASES and service.is_playing():
            return PreRouteResult(arguments={"action": "stop"})

        return None

    # ------------------------------------------------------------------
    # Post-process — fix up LLM-extracted args before run()
    # ------------------------------------------------------------------

    def post_process_tool_call(
        self, args: dict[str, Any], voice_command: str,
    ) -> dict[str, Any]:
        # Strip trailing " radio" / " station" from the station arg — the LLM
        # sometimes leaves them in even when the description tells it not to.
        station = args.get("station")
        if isinstance(station, str):
            cleaned = re.sub(
                r"\s+(?:radio(?:\s+station)?|station)\s*$",
                "",
                station,
                flags=re.IGNORECASE,
            ).strip()
            if cleaned.lower() in _JUNK_QUERIES:
                args.pop("station", None)
            elif cleaned and cleaned != station:
                args["station"] = cleaned
        return args

    # ------------------------------------------------------------------
    # Examples for LLM prompt + LoRA adapter
    # ------------------------------------------------------------------

    def generate_prompt_examples(self) -> list[CommandExample]:
        return [
            CommandExample(
                voice_command="Play WFAN radio",
                expected_parameters={"action": "play", "station": "WFAN"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Put on 670 The Score",
                expected_parameters={"action": "play", "station": "670 The Score"},
            ),
            CommandExample(
                voice_command="Tune in to KROQ",
                expected_parameters={"action": "play", "station": "KROQ"},
            ),
            CommandExample(
                voice_command="Play 1010 WINS",
                expected_parameters={"action": "play", "station": "1010 WINS"},
            ),
            CommandExample(
                voice_command="Stop the radio",
                expected_parameters={"action": "stop"},
            ),
            CommandExample(
                voice_command="What station is this",
                expected_parameters={"action": "now_playing"},
            ),
            CommandExample(
                voice_command="What Audacy stations do you have",
                expected_parameters={"action": "stations"},
            ),
        ]

    def generate_adapter_examples(self) -> list[CommandExample]:
        items: list[tuple[str, dict[str, Any]]] = [
            ("Play WFAN radio", {"action": "play", "station": "WFAN"}),
            ("Play WFAN", {"action": "play", "station": "WFAN"}),
            ("Put on WFAN", {"action": "play", "station": "WFAN"}),
            ("Turn on WFAN", {"action": "play", "station": "WFAN"}),
            ("Listen to WFAN radio", {"action": "play", "station": "WFAN"}),
            ("Play the FAN", {"action": "play", "station": "the FAN"}),
            ("Play 670 The Score", {"action": "play", "station": "670 The Score"}),
            ("Put on the Score", {"action": "play", "station": "the Score"}),
            ("Play 94 WIP", {"action": "play", "station": "94 WIP"}),
            ("Play 98.5 The Sports Hub", {"action": "play", "station": "98.5 The Sports Hub"}),
            ("Play KROQ", {"action": "play", "station": "KROQ"}),
            ("Play K-Rock", {"action": "play", "station": "K-Rock"}),
            ("Play Q104.3", {"action": "play", "station": "Q104.3"}),
            ("Play 1010 WINS", {"action": "play", "station": "1010 WINS"}),
            ("Play WBBM Newsradio", {"action": "play", "station": "WBBM Newsradio"}),
            ("Play KCBS", {"action": "play", "station": "KCBS"}),
            ("Tune in to KNX News", {"action": "play", "station": "KNX News"}),
            ("Play 105.3 The Fan", {"action": "play", "station": "105.3 The Fan"}),
            ("Play sports radio 610", {"action": "play", "station": "sports radio 610"}),
            ("Play the Game", {"action": "play", "station": "the Game"}),
            ("Play US99", {"action": "play", "station": "US99"}),
            ("Play WMMR", {"action": "play", "station": "WMMR"}),
            ("Play Audacy", {"action": "play"}),
            ("Open Audacy", {"action": "play"}),
            ("Stop", {"action": "stop"}),
            ("Stop Audacy", {"action": "stop"}),
            ("Stop the radio", {"action": "stop"}),
            ("Turn off Audacy", {"action": "stop"}),
            ("Kill the radio", {"action": "stop"}),
            ("What's playing on the radio", {"action": "now_playing"}),
            ("What station is this", {"action": "now_playing"}),
            ("Which Audacy station am I on", {"action": "now_playing"}),
            ("What Audacy stations do you have", {"action": "stations"}),
            ("List Audacy stations", {"action": "stations"}),
            ("Show me Audacy stations", {"action": "stations"}),
        ]
        return [
            CommandExample(
                voice_command=vc,
                expected_parameters=params,
                is_primary=(i == 0),
            )
            for i, (vc, params) in enumerate(items)
        ]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str | None = kwargs.get("action")
        if not action:
            return CommandResponse.error_response(
                error_details="What would you like to do with Audacy?",
                context_data={"error": "missing_action"},
            )

        service = get_service()

        handler = {
            "play": self._handle_play,
            "stop": self._handle_stop,
            "now_playing": self._handle_now_playing,
            "stations": self._handle_stations,
        }.get(action)

        if handler is None:
            return CommandResponse.error_response(
                error_details=f"I don't know how to '{action}' on Audacy.",
                context_data={"error": "unknown_action", "action": action},
            )

        return handler(service, kwargs.get("station"))

    def _handle_play(
        self, service: AudacyService, station_query: str | None,
    ) -> CommandResponse:
        # No query → resume last station if any, else nudge the user.
        if not station_query:
            current = service.now_playing()
            if current is not None:
                return CommandResponse.success_response(
                    context_data={
                        "action": "play",
                        "message": f"Already playing {current.display_name}",
                        "station": current.display_name,
                    },
                )
            return CommandResponse.error_response(
                error_details=(
                    "Which Audacy station? Try 'play WFAN radio' or "
                    "'play the score'."
                ),
                context_data={"error": "no_station"},
            )

        # Resolve before deferring playback so the spoken response can name
        # the actual station (not the raw query). Unknown station = fast
        # error path.
        station = service.resolve_station(station_query)
        if station is None:
            return CommandResponse.error_response(
                error_details=(
                    f"I don't have '{station_query}' in my Audacy station list. "
                    "Try a different call sign or nickname."
                ),
                context_data={"error": "unknown_station", "query": station_query},
            )

        # Defer the actual mpd play until after TTS finishes, so the first
        # seconds of the stream don't get ducked under the spoken response.
        # Same pattern Pandora and music-assistant use.
        def _start_playback() -> None:
            try:
                service.play_station(station_query)
            except PlaybackError as e:
                logger.error(
                    "Deferred Audacy play failed",
                    error=str(e), query=station_query,
                )
            except Exception as e:
                logger.error(
                    "Deferred Audacy play crashed",
                    error=str(e), query=station_query,
                )

        return CommandResponse.success_response(
            context_data={
                "action": "play",
                "message": f"Tuning in to {station.display_name}",
                "station": station.display_name,
                "callsign": station.callsign,
                "format": station.format,
            },
            on_response_complete=_start_playback,
        )

    def _handle_stop(
        self, service: AudacyService, _station: str | None,
    ) -> CommandResponse:
        service.stop()
        return CommandResponse.success_response(
            context_data={"action": "stop", "message": "Audacy stopped."},
        )

    def _handle_now_playing(
        self, service: AudacyService, _station: str | None,
    ) -> CommandResponse:
        current = service.now_playing()
        if current is None:
            return CommandResponse.error_response(
                error_details="Nothing is playing on Audacy right now.",
                context_data={"error": "nothing_playing"},
            )
        return CommandResponse.success_response(
            context_data={
                "action": "now_playing",
                "message": f"You're listening to {current.display_name}.",
                "station": current.display_name,
                "callsign": current.callsign,
                "format": current.format,
            },
        )

    def _handle_stations(
        self, service: AudacyService, _station: str | None,
    ) -> CommandResponse:
        from audacy_shared.station_catalog import all_stations

        stations = all_stations()
        if not stations:
            return CommandResponse.error_response(
                error_details="No Audacy stations are in my catalog yet.",
                context_data={"error": "empty_catalog"},
            )
        # Don't read 50+ station names out loud — give a sample.
        sample = [s.display_name for s in stations[:6]]
        return CommandResponse.success_response(
            context_data={
                "action": "stations",
                "message": (
                    f"I have {len(stations)} Audacy stations. "
                    f"Examples: {', '.join(sample)}. "
                    "Just ask for one by name."
                ),
                "stations": [s.display_name for s in stations],
            },
        )
