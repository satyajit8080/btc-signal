from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base

log = logging.getLogger("btcsignal.db")

TIMESCALE_HYPERTABLES = (("metric_observations", "ts"), ("liquidation_events", "ts"), ("candles_1m", "ts"))


class Database:
    def __init__(self, url: str):
        kw: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
                kw["poolclass"] = StaticPool
        self.url = url
        self.engine: Engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragma(dbapi_conn, _):  # pragma: no cover - sqlite tuning
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "postgresql":
            self._timescale()

    def _timescale(self) -> None:  # pragma: no cover - requires TimescaleDB
        with self.engine.begin() as conn:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            except Exception as e:
                log.warning("TimescaleDB extension unavailable, using plain PostgreSQL: %s", e)
                return
        for table, col in TIMESCALE_HYPERTABLES:
            with self.engine.begin() as conn:
                try:
                    is_ht = conn.execute(text(
                        "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = :t"), {"t": table}).first()
                    if is_ht:
                        continue
                    if table != "candles_1m":  # hypertables need the time column in the primary key
                        conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey"))
                        conn.execute(text(f"ALTER TABLE {table} ADD PRIMARY KEY (id, {col})"))
                    conn.execute(text(f"SELECT create_hypertable('{table}', '{col}', migrate_data => TRUE)"))
                except Exception as e:
                    log.warning("hypertable %s not created: %s", table, e)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def as_utc(dt):
    """SQLite drops tzinfo; normalize anything read back to aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
