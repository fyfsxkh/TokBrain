import pytest

import app.main as main


class Coordinator:
    def __init__(self, *, fail_start: bool = False):
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("start failed")

    async def stop(self):
        self.stopped += 1


async def test_lifespan_cleans_up_started_workers_when_later_start_fails(monkeypatch):
    imports = Coordinator()
    packages = Coordinator(fail_start=True)
    jobs = Coordinator()
    disposed = False
    clients_closed = False

    async def no_op():
        return None

    async def dispose():
        nonlocal disposed
        disposed = True

    async def close_clients():
        nonlocal clients_closed
        clients_closed = True

    monkeypatch.setattr(main, "ensure_directories", lambda: None)
    monkeypatch.setattr(main, "_remove_abandoned_media", no_op)
    monkeypatch.setattr(main, "init_db", no_op)
    monkeypatch.setattr(main, "import_coordinator", imports)
    monkeypatch.setattr(main, "package_import_coordinator", packages)
    monkeypatch.setattr(main, "job_coordinator", jobs)
    monkeypatch.setattr(main, "close_provider_clients", close_clients)
    monkeypatch.setattr(main.database, "dispose", dispose)

    with pytest.raises(RuntimeError, match="start failed"):
        async with main._application_lifetime(None):
            pass

    assert imports.started == 1
    assert imports.stopped == 1
    assert packages.started == 1
    assert packages.stopped == 0
    assert jobs.started == 0
    assert clients_closed is True
    assert disposed is True
