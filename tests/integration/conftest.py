

import os
import subprocess
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config.settings import get_settings


@pytest_asyncio.fixture
async def isolated_maker():
    base_url = make_url(get_settings().DATABASE_URL)
    if base_url.host not in ("localhost", "127.0.0.1"):
        pytest.skip("These tests create databases and only run against a local PostgreSQL")
    if not (
        base_url.database == "slothpay" or str(base_url.database).startswith("slothpay_task8_test_")
    ):
        pytest.skip("Unexpected source database; refusing setup")

    name = f"slothpay_task8_case_{uuid.uuid4().hex[:12]}"
    isolated_url = base_url.set(database=name).render_as_string(hide_password=False)
    admin = create_async_engine(
        base_url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    engine = None
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        env = os.environ.copy()
        env["DATABASE_URL"] = isolated_url
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        engine = create_async_engine(isolated_url, pool_size=8)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()