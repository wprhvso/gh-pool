from collections.abc import AsyncIterator

import pytest

from pool.protocol.sse import SseMessage, parse_sse


async def _chunks(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


async def _read(*pieces: bytes) -> list[SseMessage]:
    return [message async for message in parse_sse(_chunks(*pieces))]


def _bytewise(payload: bytes) -> tuple[bytes, ...]:
    return tuple(payload[index : index + 1] for index in range(len(payload)))


async def test_a_frame_carries_its_name_its_data_and_its_id():
    read = await _read(b"event: command\nid: 7\ndata: {}\n\n")

    assert read == [SseMessage(event="command", data="{}", id="7")]


async def test_a_frame_without_a_name_is_a_message():
    read = await _read(b"data: bare\n\n")

    assert read == [SseMessage(event="message", data="bare", id=None)]


async def test_the_space_after_the_colon_is_optional():
    read = await _read(b"event:command\nid:7\ndata:{}\n\n")

    assert read == [SseMessage(event="command", data="{}", id="7")]


async def test_data_lines_are_joined_with_newlines():
    read = await _read(b"data: first\ndata: second\n\n")

    assert read[0].data == "first\nsecond"


async def test_carriage_returns_are_not_part_of_the_data():
    read = await _read(b"event: command\r\ndata: {}\r\n\r\n")

    assert read == [SseMessage(event="command", data="{}", id=None)]


async def test_a_comment_keeps_the_stream_alive_without_producing_a_frame():
    read = await _read(b": ping\n\n", b"data: real\n\n")

    assert read == [SseMessage(event="message", data="real", id=None)]


async def test_a_frame_with_no_data_at_all_is_not_delivered():
    read = await _read(b"event: empty\n\n", b"data: real\n\n")

    assert [message.event for message in read] == ["message"]


async def test_the_name_of_one_frame_does_not_leak_into_the_next():
    read = await _read(b"event: first\ndata: a\n\nid: 2\ndata: b\n\n")

    assert [(message.event, message.id) for message in read] == [
        ("first", None),
        ("message", "2"),
    ]


async def test_a_frame_split_across_reads_is_still_one_frame():
    payload = b'event: command\nid: 12\ndata: {"seq": 12}\n\n'

    read = await _read(*_bytewise(payload))

    assert read == [SseMessage(event="command", data='{"seq": 12}', id="12")]


async def test_a_frame_the_sender_never_finished_is_not_delivered():
    read = await _read(b"event: command\ndata: half")

    assert read == []


async def test_a_multi_byte_character_split_across_reads_survives():
    payload = 'data: {"text": "café 中"}\n\n'.encode()
    middle = payload.index(b"caf") + 4

    read = await _read(payload[:middle], payload[middle:])

    assert read[0].data == '{"text": "café 中"}'


async def test_every_split_of_a_frame_full_of_text_reads_the_same_way():
    payload = 'data: {"said": "приветствие"}\n\n'.encode()

    for cut in range(1, len(payload)):
        read = await _read(payload[:cut], payload[cut:])

        assert read[0].data == '{"said": "приветствие"}'


async def test_a_byte_that_is_not_text_becomes_a_replacement_rather_than_an_error():
    read = await _read(b"data: \xff\n\n")

    assert read[0].data == "�"


@pytest.mark.parametrize("field", [b"retry: 1000", b"unknown: value"])
async def test_a_field_this_reader_has_no_use_for_is_ignored(field: bytes):
    read = await _read(field + b"\ndata: real\n\n")

    assert read[0].data == "real"
