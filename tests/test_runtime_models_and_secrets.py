from app.main import app
from app.models import AppSetting
import app.services.bss_bill as bss_bill
from app.services.runtime_settings import update_runtime_settings
from app.services.secrets import SecretUnavailableError


class SettingSession:
    def __init__(self):
        self.records: dict[str, AppSetting] = {}

    async def get(self, _model, key):
        return self.records.get(key)

    def add(self, record):
        self.records[record.key] = record

    async def flush(self):
        return None


async def test_runtime_models_are_selected_by_workload_and_reject_wrong_api_types():
    session = SettingSession()
    selected = await update_runtime_settings(
        session,
        {
            "processing_model": "qwen-max",
            "chat_fast_model": "qwen-math-turbo",
            "chat_deep_model": "glm-5",
        },
    )
    assert selected["processing_model"] == "qwen-max"
    assert selected["chat_fast_model"] == "qwen-math-turbo"
    assert selected["chat_deep_model"] == "glm-5"

    rejected = await update_runtime_settings(
        session,
        {
            "processing_model": "qwen3.7-text-embedding",
            "chat_fast_model": "gte-rerank-v2",
        },
    )
    assert rejected["processing_model"] == "qwen3.6-flash"
    assert rejected["chat_fast_model"] == "qwen3.6-flash"


async def test_bill_refresh_reports_unreadable_dpapi_credentials(monkeypatch):
    session = SettingSession()

    async def unreadable(_session, _name):
        raise SecretUnavailableError("different Windows security context")

    monkeypatch.setattr(bss_bill, "get_secret", unreadable)
    result = await bss_bill.refresh_official_bill(session)
    assert result["status"] == "credentials_unreadable"
    assert "重新输入" in result["message"]
    assert session.records["official_bill"].value == result


def test_settings_exposes_destructive_key_cleanup_endpoint():
    operations = app.openapi()["paths"]["/api/settings/secrets"]
    assert set(operations) == {"delete"}
