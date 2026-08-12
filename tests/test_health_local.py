from types import SimpleNamespace

import pytest

import app.services.health as health


class ResultSession:
    def __init__(self, cleanup=None):
        self.cleanup = cleanup
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement))

    async def get(self, _model, key):
        return self.cleanup if key == "security_cleanup" else None


@pytest.fixture(autouse=True)
def healthy_coordinators(monkeypatch):
    monkeypatch.setattr(
        health,
        "coordinator_snapshots",
        lambda: [
            {
                "name": name,
                "alive": True,
                "workers_alive": workers,
                "workers_expected": workers,
                "last_error": None,
            }
            for name, workers in (
                ("link_preview", 3),
                ("package_import", 1),
                ("processing", 1),
            )
        ],
    )


async def test_health_check_is_local_and_reports_cleanup_block(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda _name: "ffmpeg")
    session = ResultSession(
        SimpleNamespace(value={"required": True, "message": "请清理旧敏感目录"})
    )
    result = await health.run_system_checks(session)
    assert session.statements == ["SELECT 1"]
    assert result["overall"] == "down"
    probes = {item["probe"]: item for item in result["probes"]}
    assert probes["database"]["status"] == "healthy"
    assert probes["media_runtime"]["status"] == "healthy"
    assert probes["security_cleanup"]["status"] == "down"
    assert probes["security_cleanup"]["details"]["required"] is True


async def test_missing_media_runtime_is_degraded_not_network_failure(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda _name: None)
    result = await health.run_system_checks(ResultSession())
    assert result["overall"] == "degraded"
    assert all(item["probe"] != "network" for item in result["probes"])


async def test_local_probes_can_run_one_at_a_time_for_live_progress(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda _name: "ffmpeg")
    session = ResultSession()
    probes = [
        await health.run_system_probe(session, probe)
        for probe in health.SYSTEM_PROBES
    ]
    assert [probe["probe"] for probe in probes] == list(health.SYSTEM_PROBES)
    assert all(probe["status"] == "healthy" for probe in probes)


async def test_dead_coordinator_exposes_last_error(monkeypatch):
    monkeypatch.setattr(
        health,
        "coordinator_snapshots",
        lambda: [
            {
                "name": "link_preview",
                "alive": False,
                "workers_alive": 2,
                "workers_expected": 3,
                "last_error": "RuntimeError: worker stopped",
            },
            {
                "name": "package_import",
                "alive": True,
                "workers_alive": 1,
                "workers_expected": 1,
                "last_error": None,
            },
            {
                "name": "processing",
                "alive": True,
                "workers_alive": 1,
                "workers_expected": 1,
                "last_error": None,
            },
        ],
    )

    result = await health.run_system_probe(ResultSession(), "coordinators")

    assert result["status"] == "down"
    first = result["details"]["coordinators"][0]
    assert first["last_error"] == "RuntimeError: worker stopped"


async def test_alive_coordinator_with_last_error_is_degraded(monkeypatch):
    monkeypatch.setattr(
        health,
        "coordinator_snapshots",
        lambda: [
            {
                "name": name,
                "alive": True,
                "workers_alive": workers,
                "workers_expected": workers,
                "last_error": "transient database failure" if name == "processing" else None,
            }
            for name, workers in (
                ("link_preview", 3),
                ("package_import", 1),
                ("processing", 1),
            )
        ],
    )

    result = await health.run_system_probe(ResultSession(), "coordinators")

    assert result["status"] == "degraded"
