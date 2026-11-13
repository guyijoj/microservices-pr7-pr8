"""Одинаковый интерфейс репозитория для PostgreSQL и локального SQLite.

Каждый сервис имеет собственную БД. JSON позволяет оставить учебную
схему небольшой. Данные, inbox, outbox и ключи идемпотентности атомарны.
"""
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import (JSON, Column, MetaData, String, Table, create_engine,
                        delete, insert, select, text, update)


class Repository:
    def __init__(self, url):
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        self.lock = RLock()
        metadata = MetaData()
        self.table = Table("records", metadata,
                           Column("kind", String(40), primary_key=True),
                           Column("id", String(300), primary_key=True),
                           Column("payload", JSON, nullable=False))
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.lock, self.engine.begin() as connection:
            # Сериализуем изменения даже при нескольких процессах PostgreSQL.
            if self.engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(7007)"))
            yield UnitOfWork(connection, self.table)


class UnitOfWork:
    def __init__(self, connection, table):
        self.connection, self.table = connection, table

    def get(self, kind, key):
        return self.connection.execute(select(self.table.c.payload).where(
            self.table.c.kind == kind, self.table.c.id == key)).scalar_one_or_none()

    def all(self, kind):
        return list(self.connection.execute(select(self.table.c.payload).where(
            self.table.c.kind == kind)).scalars())

    def put(self, kind, key, payload):
        condition = (self.table.c.kind == kind) & (self.table.c.id == key)
        if self.get(kind, key) is None:
            self.connection.execute(insert(self.table).values(kind=kind, id=key, payload=payload))
        else:
            self.connection.execute(update(self.table).where(condition).values(payload=payload))

    def remove(self, kind, key):
        self.connection.execute(delete(self.table).where(
            self.table.c.kind == kind, self.table.c.id == key))
