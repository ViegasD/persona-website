from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text

from app.db.models import AccountToken, OutboxJob
from app.services.outbox import active_account_mail


def test_arq_receives_explicit_account_worker_redis_settings():
    from arq.worker import get_kwargs
    from app.workers.batch_runner import AccountWorkerSettings
    from app.workers.queue import redis_settings

    options = get_kwargs(AccountWorkerSettings)
    assert options['redis_settings'] == redis_settings()
    assert options['queue_name'] == 'persona:account:queue'
    assert options['functions'] == AccountWorkerSettings.functions


def test_only_live_unused_matching_account_links_are_eligible():
    now = datetime.now(UTC)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS web"))
        conn.execute(text("CREATE TABLE web.account_token (id INTEGER, user_id INTEGER, used_at DATETIME, expires_at DATETIME)"))
        conn.execute(text("CREATE TABLE web.outbox_job (id INTEGER, kind TEXT, dedup_key TEXT, user_id INTEGER)"))
        conn.execute(AccountToken.__table__.insert(), [
            dict(id=1, user_id=10, used_at=None, expires_at=now + timedelta(hours=1)),
            dict(id=2, user_id=10, used_at=None, expires_at=now - timedelta(seconds=1)),
            dict(id=3, user_id=10, used_at=now, expires_at=now + timedelta(hours=1)),
            dict(id=4, user_id=10, used_at=None, expires_at=now),
        ])
        conn.execute(text("INSERT INTO web.outbox_job VALUES "
                          "(1,'account_email','account_email:1',10),"
                          "(2,'account_email','account_email:2',10),"
                          "(3,'account_email','account_email:3',10),"
                          "(4,'account_email','account_email:4',10),"
                          "(5,'account_email','account_email:99',10),"
                          "(6,'account_email','account_email:1',11),"
                          "(7,'process_item','account_email:1',10)"))
        query = select(OutboxJob.id).where(active_account_mail(now))
        assert conn.execute(query).scalars().all() == [1]
        assert conn.execute(text("SELECT count(*) FROM web.outbox_job")).scalar_one() == 7
    engine.dispose()
