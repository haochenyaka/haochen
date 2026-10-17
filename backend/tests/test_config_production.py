"""生产配置守卫：默认弱口令、通配 CORS、开发态身份头必须在启动期失败关闭。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "production",
        "database_url": "postgresql+asyncpg://haochen:strong-db-pass@db:5432/haochen",
        "neo4j_password": "strong-neo4j-pass",
        "minio_secret_key": "strong-minio-pass",
        "lock_master_key": "0123456789abcdef",
        "device_sim_signing_secret": "strong-device-sim-pass",
        "cors_origins": ["https://admin.example.com"],
        "dev_identity_enabled": False,
        "rate_limit_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_local_defaults_skip_production_checks() -> None:
    Settings(env="local").validate_production()


def test_production_valid_settings_pass() -> None:
    settings = _production_settings()
    assert settings.production_issues() == []
    settings.validate_production()


def test_production_rejects_dev_defaults() -> None:
    issues = Settings(env="production").production_issues()
    joined = "\n".join(issues)
    assert "DATABASE_URL" in joined
    assert "NEO4J_PASSWORD" in joined
    assert "MINIO_SECRET_KEY" in joined
    assert "LOCK_MASTER_KEY" in joined
    assert "DEVICE_SIM_SIGNING_SECRET" in joined
    assert "CORS_ORIGINS" in joined


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"database_url": "postgresql+asyncpg://haochen:haochen@db:5432/haochen"},
            "DATABASE_URL",
        ),
        ({"neo4j_password": "neo4j"}, "NEO4J_PASSWORD"),
        ({"minio_secret_key": "haochen-dev-secret"}, "MINIO_SECRET_KEY"),
        ({"lock_master_key": "short-key"}, "LOCK_MASTER_KEY"),
        ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"cors_origins": []}, "CORS_ORIGINS"),
        ({"dev_identity_enabled": True}, "DEV_IDENTITY_ENABLED"),
        ({"rate_limit_enabled": False}, "RATE_LIMIT_ENABLED"),
    ],
)
def test_production_rejects_unsafe_value(overrides: dict[str, object], expected: str) -> None:
    issues = _production_settings(**overrides).production_issues()
    assert any(expected in issue for issue in issues), issues


def test_production_langfuse_external_requires_explicit_consent() -> None:
    """配置了密钥但未声明 self-host 时必须显式允许脱敏外发，否则拒绝启动。"""
    settings = _production_settings(
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    assert any("LANGFUSE" in issue for issue in settings.production_issues())

    allowed = _production_settings(
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_allow_external_export=True,
    )
    assert allowed.production_issues() == []

    self_hosted = _production_settings(
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_self_hosted=True,
    )
    assert self_hosted.production_issues() == []


def test_create_app_fails_closed_with_unsafe_production_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(env="production"))
    with pytest.raises(RuntimeError, match="生产配置不安全"):
        create_app()


def test_cors_wildcard_disables_credentials() -> None:
    """通配符来源与凭据不能同时生效，否则浏览器会拒绝跨域响应。"""
    app = create_app()
    with TestClient(app) as client:
        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://any.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in response.headers
