"""Disposable PostgreSQL and isolated application imports for integration tests."""

import importlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, configure_mappers


class PostgresTestCase(unittest.IsolatedAsyncioTestCase):
    extra_modules = {}

    @classmethod
    def prepare_extra_imports(cls, database):
        """Optional hook for route tests to stub external-service imports."""

    @classmethod
    def setUpClass(cls):
        initdb = shutil.which("initdb")
        pg_ctl = shutil.which("pg_ctl")
        if not initdb or not pg_ctl:
            raise unittest.SkipTest("Local PostgreSQL tools initdb and pg_ctl are required")

        class IsolatedBase(DeclarativeBase):
            pass

        # Keep real model/service code, but isolate its Base from app.database,
        # whose configuration can load production credentials or AWS secrets.
        original_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }
        try:
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    del sys.modules[name]
            database = types.ModuleType("app.database")
            database.Base = IsolatedBase
            sys.modules["app.database"] = database
            cls.models = importlib.import_module("app.models")
            cls.jobs = importlib.import_module("app.services.report_jobs")
            cls.prepare_extra_imports(database)
            for attribute, module_name in cls.extra_modules.items():
                setattr(cls, attribute, importlib.import_module(module_name))
            configure_mappers()
            config = sys.modules.get("app.config")
            if config is not None and not getattr(config, "_offline_test_stub", False):
                raise AssertionError("Offline tests must not import application settings")
        finally:
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    del sys.modules[name]
            sys.modules.update(original_modules)
        cls.base = IsolatedBase

        temporary = tempfile.TemporaryDirectory(prefix="rr-jobs-", dir="/tmp")
        cls.addClassCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        data = directory / "data"
        log_path = directory / "postgres.log"

        def run_postgres(command):
            result = subprocess.run(command, capture_output=True, text=True, timeout=40)
            if result.returncode:
                log = log_path.read_text() if log_path.exists() else ""
                raise RuntimeError(f"Temporary PostgreSQL setup failed:\n{result.stdout}{result.stderr}{log}")

        run_postgres([
            initdb, "-D", str(data), "--auth=trust", "--no-locale", "-E", "UTF8", "-U", "report_jobs_test",
        ])
        cls.addClassCleanup(
            subprocess.run, [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            capture_output=True, text=True, timeout=40,
        )
        run_postgres([
            pg_ctl, "-D", str(data), "-l", str(log_path),
            "-o", f"-h '' -k {directory} -c unix_socket_permissions=0700", "-w", "start",
        ])
        cls.url = URL.create(
            "postgresql+asyncpg", username="report_jobs_test", database="postgres",
            query={"host": str(directory)},
        )

    async def asyncSetUp(self):
        self.engine = create_async_engine(self.url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(self.base.metadata.create_all)
        async with self.sessions.begin() as db:
            db.add(self.models.User(id="owner", email="owner@example.invalid", full_name="Owner"))
            db.add(self.models.User(id="other", email="other@example.invalid", full_name="Other"))

    async def asyncTearDown(self):
        try:
            async with self.engine.begin() as connection:
                await connection.run_sync(self.base.metadata.drop_all)
        finally:
            await self.engine.dispose()

    async def make_interview(self, **overrides):
        values = {
            "id": str(uuid.uuid4()), "user_id": "owner", "resume_text": "Test resume",
            "status": "completed", "interview_mode": "voice",
            "transcript": [{"role": "user", "text": "This is my original answer."}],
            "interview_plan": {"categories": [{"name": "Technical", "questions": []}]},
        }
        values.update(overrides)
        async with self.sessions.begin() as db:
            db.add(self.models.Interview(**values))
        return values["id"]

