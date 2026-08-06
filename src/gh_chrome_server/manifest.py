from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "urn:mpeg:dash:schema:mpd:2011"
PROFILES = "urn:mpeg:dash:profile:isoff-live:2011"


def count_segments(directory: Path) -> int:
    if not directory.exists():
        return 0
    numbers = [int(p.stem) for p in directory.glob("*.m4s") if p.stem.isdigit()]
    return max(numbers) if numbers else 0


def _duration(seconds: float) -> str:
    return f"PT{seconds:.3f}S"


def build(
    *,
    width: int,
    height: int,
    fps: int,
    segment_seconds: float,
    segments: int,
    available_at: datetime,
    live: bool,
) -> str:
    duration = segments * segment_seconds
    attrs = {
        "xmlns": NS,
        "profiles": PROFILES,
        "minBufferTime": _duration(segment_seconds * 2),
        "maxSegmentDuration": _duration(segment_seconds),
    }
    if live:
        attrs |= {
            "type": "dynamic",
            "availabilityStartTime": available_at.astimezone(UTC).isoformat(
                timespec="milliseconds"
            ),
            "publishTime": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "minimumUpdatePeriod": _duration(segment_seconds),
            "timeShiftBufferDepth": _duration(max(duration, segment_seconds)),
            "suggestedPresentationDelay": _duration(segment_seconds * 3),
        }
    else:
        attrs |= {"type": "static", "mediaPresentationDuration": _duration(duration)}

    mpd = ET.Element("MPD", attrs)
    period = ET.SubElement(mpd, "Period", {"id": "0", "start": "PT0.0S"})
    adaptation = ET.SubElement(
        period,
        "AdaptationSet",
        {
            "contentType": "video",
            "mimeType": "video/mp4",
            "segmentAlignment": "true",
            "startWithSAP": "1",
            "par": f"{width}:{height}",
        },
    )
    ET.SubElement(
        adaptation,
        "SegmentTemplate",
        {
            "media": "$Number$.m4s",
            "initialization": "init.m4s",
            "duration": str(int(segment_seconds * 1000)),
            "timescale": "1000",
            "startNumber": "1",
        },
    )
    ET.SubElement(
        adaptation,
        "Representation",
        {
            "id": "0",
            "codecs": "avc1.42c01f",
            "width": str(width),
            "height": str(height),
            "frameRate": str(fps),
            "sar": "1:1",
            "bandwidth": "2000000",
        },
    )
    return ET.tostring(mpd, encoding="unicode", xml_declaration=True)
