"""Guard standalone container startup ordering."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_container_uses_startup_script():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY start.sh ./" in dockerfile
    assert 'CMD ["sh", "/app/start.sh"]' in dockerfile


def test_migrations_finish_before_services_start():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "set -e" in script
    assert "alembic upgrade head\n" in script
    assert script.index("alembic upgrade head") < script.index("arq ") < script.index("exec uvicorn ")
