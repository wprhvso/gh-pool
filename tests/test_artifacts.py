import asyncio
import contextlib
import hashlib

from tests.conftest import as_client, as_worker

from gh_pool import server


async def test_an_artifact_comes_back_byte_for_byte(client):
    body = b"a recording of something"

    put = await client.put(
        "/v1/artifacts/runs/one.bin", content=body, headers=as_worker()
    )
    got = await client.get("/v1/artifacts/runs/one.bin", headers=as_client())

    assert put.json()["size"] == len(body)
    assert put.json()["sha256"] == hashlib.sha256(body).hexdigest()
    assert got.content == body


async def test_writing_the_same_key_again_replaces_it(client):
    await client.put("/v1/artifacts/k", content=b"first", headers=as_worker())
    await client.put("/v1/artifacts/k", content=b"second", headers=as_worker())

    got = await client.get("/v1/artifacts/k", headers=as_client())

    assert got.content == b"second"


async def test_a_key_nobody_wrote_is_not_there(client):
    got = await client.get("/v1/artifacts/missing", headers=as_client())

    assert got.status_code == 404


async def test_artifacts_can_be_narrowed_by_prefix(client):
    await client.put("/v1/artifacts/logs/a", content=b"1", headers=as_worker())
    await client.put("/v1/artifacts/other/b", content=b"2", headers=as_worker())

    listed = await client.get(
        "/v1/artifacts", params={"prefix": "logs/"}, headers=as_client()
    )

    assert [row["key"] for row in listed.json()] == ["logs/a"]


async def test_a_deleted_artifact_leaves_nothing_behind(client):
    await client.put("/v1/artifacts/k", content=b"x", headers=as_worker())

    await client.delete("/v1/artifacts/k", headers=as_client())

    assert (await client.get("/v1/artifacts/k", headers=as_client())).status_code == 404
    assert "k" not in server.BLOBS


async def test_an_unfinished_upload_does_not_become_the_artifact(client, monkeypatch):
    def explode(*_, **__):
        raise OSError("disk went away")

    await client.put("/v1/artifacts/k", content=b"good", headers=as_worker())
    monkeypatch.setattr(server.os, "replace", explode)

    with contextlib.suppress(OSError):
        await client.put("/v1/artifacts/k", content=b"bad", headers=as_worker())

    assert (await client.get("/v1/artifacts/k", headers=as_client())).content == b"good"


async def test_two_uploads_of_the_same_key_do_not_mix(client):
    async def upload(body):
        return await client.put(
            "/v1/artifacts/shared", content=body, headers=as_worker()
        )

    answers = await asyncio.gather(upload(b"a" * 50000), upload(b"b" * 50000))
    got = (await client.get("/v1/artifacts/shared", headers=as_client())).content

    assert got in (b"a" * 50000, b"b" * 50000)
    assert {a.json()["sha256"] for a in answers} == {
        hashlib.sha256(b"a" * 50000).hexdigest(),
        hashlib.sha256(b"b" * 50000).hexdigest(),
    }


async def test_a_failed_upload_leaves_no_scraps_behind(client, monkeypatch):
    def explode(*_, **__):
        raise OSError("disk went away")

    monkeypatch.setattr(server.os, "replace", explode)
    with contextlib.suppress(OSError):
        await client.put("/v1/artifacts/k", content=b"bad", headers=as_worker())

    assert list(server.BLOB_DIR.rglob("*.part")) == []
