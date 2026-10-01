"""Every Database.connect in a test leaks an aiosqlite worker thread —
non-daemon, so the interpreter can't exit after results print and pytest
processes accumulate as zombies. Track opened connections and close them
during teardown; the worker thread dies with the connection."""

import asyncio

import pytest

from devinmobile.db import Database


@pytest.fixture(autouse=True)
def _close_dbs(monkeypatch):
    opened: list[Database] = []
    orig = Database.connect.__func__

    async def connect(cls, path: str) -> Database:
        db = await orig(cls, path)
        opened.append(db)
        return db

    monkeypatch.setattr(Database, "connect", classmethod(connect))
    yield
    for db in opened:
        asyncio.run(db.close())
