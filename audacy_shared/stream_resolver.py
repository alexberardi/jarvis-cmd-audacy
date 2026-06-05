"""Build stream URLs for an Audacy station.

Audacy moved its public streams from StreamTheWorld to its in-house AmperWave
platform in 2021. AmperWave is the current primary; StreamTheWorld URLs still
work but Audacy has signaled intent to retire them, so we try AmperWave first
and fall back to StreamTheWorld.

URL formats observed in the wild (2026):
  AmperWave AAC  → https://live.amperwave.net/direct/audacy-{callsign}{band}aac-imc
  AmperWave MP3  → https://live.amperwave.net/direct/audacy-{callsign}{band}mp3-imc
  StreamTheWorld → https://provisioning.streamtheworld.com/pls/{CALLSIGN}AAC.pls

A station entry may declare `stream_url` to override both — useful when a
station doesn't follow the slug convention.
"""

from .station_catalog import Station


def stream_urls(station: Station) -> list[str]:
    """Return ordered URL candidates for a station, most-preferred first.

    mpd can chew through this list with `add` calls; the first URL it can
    successfully open wins. A station with an explicit `stream_url` overrides
    everything — we trust the catalog author.
    """
    if station.stream_url:
        return [station.stream_url]

    cs = station.callsign.lower()
    band = station.band.lower()

    return [
        f"https://live.amperwave.net/direct/audacy-{cs}{band}aac-imc",
        f"https://live.amperwave.net/direct/audacy-{cs}{band}mp3-imc",
        f"https://provisioning.streamtheworld.com/pls/{station.callsign.upper()}AAC.pls",
    ]
