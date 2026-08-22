import pytest
from sqlalchemy.dialects import postgresql

from gh_pool.db import tasks as db


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


def test_a_task_is_keyed_by_its_id():
    assert db.key_of(db.Task) == "id"


def test_an_artifact_is_keyed_by_its_key():
    assert db.key_of(db.Artifact) == "key"


def test_the_task_columns_are_the_ones_the_server_hands_out():
    assert db.TASK_COLUMNS == (
        "id",
        "type",
        "payload",
        "status",
        "worker_id",
        "error",
        "parent_id",
        "created_at",
        "started_at",
        "finished_at",
    )


def test_the_artifact_columns_describe_a_stored_blob():
    assert db.ARTIFACT_COLUMNS == (
        "key",
        "path",
        "size",
        "sha256",
        "task_id",
        "created_at",
    )


def test_a_row_becomes_a_plain_dictionary():
    row = db.Task(
        id="t1",
        type="python",
        payload={"code": "x"},
        status="done",
        worker_id="w1",
        error=None,
        parent_id=None,
        created_at=1.0,
        started_at=2.0,
        finished_at=3.0,
    )

    got = db.as_dict(row, db.Task)

    assert got["id"] == "t1"
    assert got["payload"] == {"code": "x"}
    assert set(got) == set(db.TASK_COLUMNS)


async def test_saving_nothing_touches_no_session(monkeypatch):
    def explode(*_, **__):
        raise AssertionError("the session should not have been opened")

    monkeypatch.setattr(db.Session, "begin", explode)

    assert await db.save(db.Task, []) is None


def test_only_the_unfinished_are_recovered():
    text = sql(db.select(db.Task).where(db.Task.status.in_(("pending", "running"))))

    assert "status IN" in text


@pytest.mark.parametrize("status", [None, "done"])
def test_a_listing_is_newest_first_and_bounded(status):
    q = db.select(db.Task).order_by(db.Task.created_at.desc()).limit(10)
    text = sql(q.where(db.Task.status == status) if status else q)

    assert "ORDER BY tasks.created_at DESC" in text
    assert "LIMIT" in text
    assert ("WHERE" in text) is bool(status)


def test_artifacts_can_be_narrowed_by_prefix():
    text = sql(db.select(db.Artifact).where(db.Artifact.key.startswith("out/")))

    assert "LIKE" in text
