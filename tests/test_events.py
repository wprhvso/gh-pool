from conftest import as_client, as_worker, submit, take

from pool import server


async def append(client, tid, token, data, offset):
    return await client.post(
        f"/v1/tasks/{tid}/events",
        params={"offset": offset},
        content=data,
        headers=as_worker({"X-Lease-Token": token}),
    )


async def test_events_are_appended_at_the_offset_the_worker_was_given(client):
    tid = await submit(client)
    leased = await take(client)
    token = leased["lease_token"]

    first = await append(client, tid, token, b"one\n", leased["event_offset"])
    second = await append(client, tid, token, b"two\n", first.json()["offset"])

    assert second.json()["offset"] == 8
    read = await client.get(f"/v1/tasks/{tid}/events", headers=as_client())
    assert read.text == "one\ntwo\n"


async def test_an_offset_that_does_not_match_is_refused_with_the_real_one(client):
    tid = await submit(client)
    leased = await take(client)
    token = leased["lease_token"]
    await append(client, tid, token, b"one\n", 0)

    stale = await append(client, tid, token, b"again\n", 0)

    assert stale.status_code == 409
    assert stale.json() == {"offset": 4, "accepting": True}


async def test_a_resent_chunk_does_not_duplicate_the_stream(client):
    tid = await submit(client)
    leased = await take(client)
    token = leased["lease_token"]
    await append(client, tid, token, b"one\n", 0)

    await append(client, tid, token, b"one\n", 0)

    read = await client.get(f"/v1/tasks/{tid}/events", headers=as_client())
    assert read.text == "one\n"


async def test_the_stream_stops_accepting_once_it_is_full(client, monkeypatch):
    monkeypatch.setattr(server, "EVENT_CAP", 4)
    tid = await submit(client)
    leased = await take(client)
    token = leased["lease_token"]

    filled = await append(client, tid, token, b"1234", 0)
    refused = await append(client, tid, token, b"more", filled.json()["offset"])

    assert filled.json()["accepting"] is False
    assert refused.json() == {"offset": 4, "accepting": False}


async def test_a_reader_can_start_from_where_it_left_off(client):
    tid = await submit(client)
    leased = await take(client)
    await append(client, tid, leased["lease_token"], b"one\ntwo\n", 0)

    read = await client.get(
        f"/v1/tasks/{tid}/events", params={"offset": 4}, headers=as_client()
    )

    assert read.text == "two\n"


async def test_events_need_the_lease_that_owns_the_task(client):
    tid = await submit(client)
    await take(client)

    answer = await append(client, tid, "wrong", b"x", 0)

    assert answer.status_code == 409
