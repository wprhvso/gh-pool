"""The DASH manifest the player polls while the session is being recorded."""

from datetime import UTC, datetime
from pathlib import Path

TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     profiles="urn:mpeg:dash:profile:isoff-live:2011"
     minBufferTime="{min_buffer}" maxSegmentDuration="{max_segment}" {timing}>
  <Period id="0" start="PT0.0S">
    <AdaptationSet contentType="video" mimeType="video/mp4" segmentAlignment="true"
                   startWithSAP="1" par="{width}:{height}">
      <SegmentTemplate media="$Number$.m4s" initialization="init.m4s"
                       duration="{segment_ms}" timescale="1000" startNumber="1"/>
      <Representation id="0" codecs="avc1.42c01f" width="{width}" height="{height}"
                      frameRate="{fps}" sar="1:1" bandwidth="2000000"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


def count_segments(directory: Path) -> int:
    """The highest segment number on disk, which is also how many there are."""
    if not directory.exists():
        return 0
    numbers = [int(path.stem) for path in directory.glob("*.m4s") if path.stem.isdigit()]
    return max(numbers, default=0)


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
    if live:
        timing = (
            f'type="dynamic" availabilityStartTime="{_stamp(available_at)}" '
            f'publishTime="{_stamp(datetime.now(UTC))}" '
            f'minimumUpdatePeriod="{_seconds(segment_seconds)}" '
            f'timeShiftBufferDepth="{_seconds(max(duration, segment_seconds))}" '
            f'suggestedPresentationDelay="{_seconds(segment_seconds * 3)}"'
        )
    else:
        timing = f'type="static" mediaPresentationDuration="{_seconds(duration)}"'
    return TEMPLATE.format(
        min_buffer=_seconds(segment_seconds * 2),
        max_segment=_seconds(segment_seconds),
        timing=timing,
        width=width,
        height=height,
        fps=fps,
        segment_ms=int(segment_seconds * 1000),
    )


def _seconds(value: float) -> str:
    return f"PT{value:.3f}S"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")
