"""
Connection pool.

Endpoints are declared `async def` but every database call goes through
run_in_threadpool, because psycopg's sync driver is used here. Mixing a
sync driver into an async endpoint without that would block the event
loop for the duration of every query.
"""

from contextlib import contextmanager

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from . import config


_pool: ConnectionPool | None = None


def open_pool() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.DSN, min_size=1, max_size=8,
            kwargs={"row_factory": dict_row}, open=True,
        )
        _pool.wait(timeout=15)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def cursor():
    if _pool is None:
        raise RuntimeError("connection pool is not open")
    with _pool.connection() as conn, conn.cursor() as cur:
        yield cur


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
