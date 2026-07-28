from types import SimpleNamespace

import app.services.health as health


class ResultSession:
    def __init__(self, cleanup=None):
        self.cleanup = cleanup
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement))

    async def get(self, _model, key):
        return self.cleanup if key == "security_cleanup" else None


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
