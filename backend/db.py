#!/usr/bin/env python3
"""Postgres 커넥션 풀과 요청 단위 커넥션 제공."""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger("backend.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool: ConnectionPool | None = None


def open_pool() -> None:
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    _pool.wait()
    logger.info("db pool opened host=%s", DATABASE_URL.rsplit("@", 1)[-1])


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("db pool closed")


def get_conn() -> Iterator[psycopg.Connection]:
    """요청 하나에 커넥션 하나. 정상 종료면 commit, 예외면 rollback."""
    if _pool is None:
        raise RuntimeError("db pool is not initialized")

    with _pool.connection() as conn:
        yield conn
