from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from gh_chrome_server.manifest import build, count_segments

NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def test_live_manifest_is_dynamic() -> None:
    root = parse(
        build(
            width=1920,
            height=1080,
            fps=15,
            segment_seconds=1.0,
            segments=10,
            available_at=datetime.now(UTC),
            live=True,
        )
    )
    assert root.get("type") == "dynamic"
    assert root.get("availabilityStartTime")
    assert root.get("mediaPresentationDuration") is None


def test_closed_manifest_is_static_with_duration() -> None:
    root = parse(
        build(
            width=1280,
            height=720,
            fps=30,
            segment_seconds=1.0,
            segments=7,
            available_at=datetime.now(UTC),
            live=False,
        )
    )
    assert root.get("type") == "static"
    assert root.get("mediaPresentationDuration") == "PT7.000S"


def test_representation_matches_params() -> None:
    root = parse(
        build(
            width=1280,
            height=720,
            fps=30,
            segment_seconds=1.0,
            segments=1,
            available_at=datetime.now(UTC),
            live=False,
        )
    )
    representation = root.find(".//mpd:Representation", NS)
    assert representation is not None
    assert representation.get("width") == "1280"
    assert representation.get("frameRate") == "30"


def test_count_segments_ignores_non_numeric(tmp_path: Path) -> None:
    for name in ("1.m4s", "2.m4s", "3.m4s", "init.m4s"):
        (tmp_path / name).touch()
    assert count_segments(tmp_path) == 3


def test_count_segments_on_missing_dir(tmp_path: Path) -> None:
    assert count_segments(tmp_path / "nope") == 0
