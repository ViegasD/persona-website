"""Guard standalone container startup ordering."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_async_database_dependencies_are_explicit():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(dependency.startswith("sqlalchemy[asyncio]") for dependency in project["project"]["dependencies"])


def test_container_uses_startup_script():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY start.sh ./" in dockerfile
    assert 'CMD ["sh", "/app/start.sh"]' in dockerfile


def test_migrations_finish_before_services_start():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "set -e" in script
    assert "alembic upgrade head\n" in script
    assert script.index("alembic upgrade head") < script.index("arq ") < script.index("exec uvicorn ")
