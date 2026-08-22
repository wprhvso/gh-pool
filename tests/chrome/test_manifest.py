from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pool.server import manifest

DASH = "{urn:mpeg:dash:schema:mpd:2011}"
STARTED = datetime(2026, 8, 19, 10, 30, 0, tzinfo=UTC)


def _build(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "width": 1280,
        "height": 720,
        "fps": 15,
        "segment_seconds": 1.0,
        "segments": 4,
        "available_at": STARTED,
        "live": True,
    }
    arguments.update(overrides)
    return manifest.build(**arguments)  # pyright: ignore[reportArgumentType]


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)  # noqa: S314


def _representation(xml: str) -> ET.Element:
    found = _root(xml).find(f".//{DASH}Representation")
    assert found is not None
    return found


def test_a_directory_that_was_never_written_holds_no_segments(tmp_path: Path):
    assert manifest.count_segments(tmp_path / "nothing-here") == 0
    assert manifest.count_segments(tmp_path) == 0


def test_the_count_is_the_highest_number_the_recorder_reached(tmp_path: Path):
    for number in (1, 2, 3):
        (tmp_path / f"{number}.m4s").write_bytes(b"segment")

    assert manifest.count_segments(tmp_path) == 3


def test_a_gap_left_by_a_failed_upload_does_not_shorten_the_recording(
    tmp_path: Path,
):
    for number in (1, 2, 9):
        (tmp_path / f"{number}.m4s").write_bytes(b"segment")

    assert manifest.count_segments(tmp_path) == 9


def test_whatever_is_not_a_numbered_segment_is_not_counted(tmp_path: Path):
    (tmp_path / "init.m4s").write_bytes(b"header")
    (tmp_path / "notes.txt").write_bytes(b"nothing")
    (tmp_path / "2.m4s").write_bytes(b"segment")

    assert manifest.count_segments(tmp_path) == 2


def test_the_manifest_describes_the_screen_that_was_recorded():
    representation = _representation(_build(width=800, height=600, fps=5))

    assert representation.get("width") == "800"
    assert representation.get("height") == "600"
    assert representation.get("frameRate") == "5"


def test_a_running_session_is_offered_as_a_live_stream():
    xml = _build(live=True)

    assert _root(xml).get("type") == "dynamic"
    assert _root(xml).get("availabilityStartTime", "").startswith("2026-08-19T10:30")
    assert _root(xml).get("minimumUpdatePeriod") == "PT1.000S"
    assert "mediaPresentationDuration" not in xml


def test_a_session_that_is_over_is_offered_as_a_recording_of_known_length():
    xml = _build(live=False, segments=4, segment_seconds=1.5)

    assert _root(xml).get("type") == "static"
    assert _root(xml).get("mediaPresentationDuration") == "PT6.000S"
    assert "availabilityStartTime" not in xml


def test_a_live_stream_always_offers_a_window_to_seek_in():
    xml = _build(live=True, segments=0)

    assert _root(xml).get("timeShiftBufferDepth") == "PT1.000S"


def test_the_segments_are_named_the_way_the_player_asks_for_them():
    template = _root(_build()).find(f".//{DASH}SegmentTemplate")

    assert template is not None
    assert template.get("media") == "$Number$.m4s"
    assert template.get("initialization") == "init.m4s"
    assert template.get("startNumber") == "1"


@pytest.mark.parametrize(
    ("segment_seconds", "expected"), [(1.0, "1000"), (0.5, "500"), (2.0, "2000")]
)
def test_the_segment_length_reaches_the_player_in_milliseconds(
    segment_seconds: float, expected: str
):
    template = _root(_build(segment_seconds=segment_seconds)).find(
        f".//{DASH}SegmentTemplate"
    )

    assert template is not None
    assert template.get("duration") == expected
    assert template.get("timescale") == "1000"


def test_the_manifest_is_xml_a_player_can_read():
    root = _root(_build())

    assert root.tag == f"{DASH}MPD"
    assert root.get("profiles") == "urn:mpeg:dash:profile:isoff-live:2011"
