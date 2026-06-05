"""Audacy station catalog — loads stations.yaml and resolves voice queries.

The catalog is a static YAML shipped with the package. Resolution uses tiered
fuzzy match:
  1. Exact alias hit (case-insensitive)
  2. Exact callsign hit
  3. Substring alias hit
  4. Token-overlap score against aliases + display_name (≥60% wins)

The resolve() check is also used by the command's pre_route() to decide
whether to claim a "play X radio" utterance — so it must be fast and
deterministic. We load the YAML once at import time and keep it in memory.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Station:
    callsign: str
    band: str
    display_name: str
    aliases: tuple[str, ...]
    format: str
    market: str
    stream_url: str | None = None  # explicit override; resolver uses this if set


def _load_stations() -> list[Station]:
    catalog_path = Path(__file__).parent / "stations.yaml"
    with catalog_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("stations", [])
    stations: list[Station] = []
    for entry in raw:
        stations.append(
            Station(
                callsign=str(entry["callsign"]).upper(),
                band=str(entry.get("band", "fm")).lower(),
                display_name=str(entry["display_name"]),
                aliases=tuple(a.lower() for a in entry.get("aliases", [])),
                format=str(entry.get("format", "")).lower(),
                market=str(entry.get("market", "")),
                stream_url=entry.get("stream_url"),
            )
        )
    return stations


_STATIONS: list[Station] = _load_stations()


def all_stations() -> list[Station]:
    return list(_STATIONS)


def _tokens(s: str) -> set[str]:
    return {t for t in s.lower().replace("-", " ").replace(".", " ").split() if t}


def _token_overlap(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    common = query_tokens & candidate_tokens
    return len(common) / max(len(query_tokens), len(candidate_tokens))


def resolve(query: str) -> Station | None:
    """Best-effort station match for a voice query. Returns None if unsure.

    Conservative on purpose: the pre_route gate calls this to decide whether
    to claim a "play X radio" utterance. False positives steal commands meant
    for Spotify / Pandora / Music Assistant; false negatives just fall back
    to the LLM router.
    """
    if not query:
        return None
    q = query.strip().lower().rstrip(".!?,")
    if not q:
        return None

    # 1. exact alias
    for station in _STATIONS:
        if q in station.aliases:
            return station

    # 2. exact callsign
    for station in _STATIONS:
        if q == station.callsign.lower():
            return station

    # 3. substring against aliases (q in alias OR alias in q)
    for station in _STATIONS:
        for alias in station.aliases:
            if q in alias or alias in q:
                return station

    # 4. token overlap — must be ≥ 0.6 to claim, and ≥ 1 non-trivial token shared
    q_tokens = _tokens(q)
    trivial = {"the", "a", "an", "fm", "am", "radio", "station"}
    q_meaningful = q_tokens - trivial
    if not q_meaningful:
        return None

    best: tuple[float, Station] | None = None
    for station in _STATIONS:
        candidate_tokens = set()
        for alias in station.aliases:
            candidate_tokens |= _tokens(alias)
        candidate_tokens |= _tokens(station.display_name)
        candidate_tokens |= {station.callsign.lower()}

        score = _token_overlap(q_meaningful, candidate_tokens - trivial)
        if score >= 0.6 and (best is None or score > best[0]):
            best = (score, station)

    return best[1] if best else None
