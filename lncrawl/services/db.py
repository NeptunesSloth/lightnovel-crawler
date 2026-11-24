from functools import cached_property
import logging
import os
from pathlib import Path
from threading import Condition, Event, Lock, Thread
import time
from typing import Any, Callable, Mapping, Optional, Sequence, Set

from sqlalchemy.engine import make_url
import sqlmodel as sa

from ..context import ctx
from ..dao import SQLModel

logger = logging.getLogger(__name__)

# Automatic copies of the database: the first one this long after a start (and
# only if the newest is older than SNAPSHOT_EVERY), then looked at this often.
SNAPSHOT_FIRST = 600.0
SNAPSHOT_EVERY = 12 * 3600.0
SNAPSHOT_CHECK = 3600.0


class _Gate:
    """Counts the database connections in use, and can hold new ones back.

    Nothing waits to be let in while it is open, so a thread that already has a
    connection and asks for another is never stuck behind a `close` waiting for
    it: `close` only succeeds once nothing at all is in use.
    """

    def __init__(self) -> None:
        self._state = Condition()
        self._in_use = 0
        self._closed = False

    @property
    def in_use(self) -> int:
        return self._in_use

    def enter(self) -> None:
        with self._state:
            while self._closed:
                self._state.wait()
            self._in_use += 1

    def leave(self) -> None:
        with self._state:
            self._in_use = max(0, self._in_use - 1)
            self._state.notify_all()

    def close(self, timeout: float) -> bool:
        """Hold new connections back once none is in use; False if that took too long."""
        with self._state:
            if not self._state.wait_for(lambda: self._in_use == 0, timeout):
                return False
            self._closed = True
            return True

    def open(self) -> None:
        with self._state:
            self._closed = False
            self._state.notify_all()


class DB:
    def __init__(self) -> None:
        self.rescued: Optional[str] = None  # what was done to get an unreadable database back
        self._snapshotting = Event()
        self._closing = Event()
        self._gate = _Gate()
        self._held: Set[int] = set()  # connections handed out, by their pool record
        self._held_lock = Lock()
        self._generation = 0  # how many times the database file has moved
        self._sqlite_file: Optional[str] = None  # where it is now, once it has moved

    @cached_property
    def engine(self):
        engine = self._create_engine(ctx.config.db.url)
        if engine.dialect.name == "sqlite":
            self._watch(engine)
        return engine

    def _watch(self, engine: Any) -> None:
        """Count the connections in use, and open new ones where the database is now."""
        from sqlalchemy import event, exc

        @event.listens_for(engine, "do_connect")
        def _where(_dialect: Any, _record: Any, cargs: list, cparams: dict) -> None:
            if self._sqlite_file:
                if cargs:
                    cargs[0] = self._sqlite_file
                else:
                    cparams["database"] = self._sqlite_file

        @event.listens_for(engine, "connect")
        def _made(_dbapi_conn: Any, record: Any) -> None:
            record.info["lncrawl_generation"] = self._generation

        @event.listens_for(engine, "checkout")
        def _out(_dbapi_conn: Any, record: Any, _proxy: Any) -> None:
            self._gate.enter()
            if record.info.get("lncrawl_generation") != self._generation:
                # opened before the database moved: the pool makes a new one
                self._gate.leave()
                raise exc.DisconnectionError("the database has moved")
            with self._held_lock:
                self._held.add(id(record))

        @event.listens_for(engine, "checkin")
        def _in(_dbapi_conn: Any, record: Any) -> None:
            with self._held_lock:
                held = id(record) in self._held
                self._held.discard(id(record))
            if held:
                self._gate.leave()

    def move_sqlite(self, new_file: str, finish: Callable[[], None], timeout: float) -> bool:
        """Use the database file at ``new_file`` from now on, without a restart.

        Waits up to ``timeout`` seconds for no connection to be in use, then
        holds new ones back while ``finish`` runs: it makes ``new_file`` a whole
        copy of the database. After that every connection, including those of
        sessions made earlier, is to ``new_file``. False, with nothing changed,
        when the database could not be had to itself in time; an error from
        ``finish`` also changes nothing, and is raised.
        """
        engine = self.engine
        if engine.dialect.name != "sqlite":
            raise ValueError("Only a database file can be moved")
        if not self._gate.close(timeout):
            return False
        try:
            finish()
            self._sqlite_file = new_file
            self._generation += 1
            engine.dispose()  # the connections to the old file, all idle now
        finally:
            self._gate.open()
        return True

    def close(self):
        self._closing.set()
        if "engine" in self.__dict__:
            # Fold the journal into the database before letting go. Left to the
            # next start, a drive unplugged after the app "closed" still had
            # the last changes only in the journal.
            if self._database_file() is not None:
                try:
                    with self.engine.connect() as conn:
                        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as error:
                    logger.debug(f"Could not fold the journal in on closing: {error}")
            self.engine.dispose()
            self.__dict__.pop("engine")

    def session(
        self,
        *,
        autoflush: bool = True,
        expire_on_commit: bool = False,
        enable_baked_queries: bool = True,
    ):
        return sa.Session(
            self.engine,
            autoflush=autoflush,
            expire_on_commit=expire_on_commit,
            enable_baked_queries=enable_baked_queries,
        )

    def exec(
        self,
        raw_sql: str,
        parameters: Optional[Sequence] = None,
        execution_options: Optional[Mapping] = None,
    ):
        r"""Executes a string SQL statement on the DBAPI cursor directly,
        without any SQL compilation steps.

         Multiple dictionaries::

             conn.exec_driver_sql(
                 "INSERT INTO table (id, value) VALUES (%(id)s, %(value)s)",
                 [{"id": 1, "value": "v1"}, {"id": 2, "value": "v2"}],
             )

         Single dictionary::

             conn.exec_driver_sql(
                 "INSERT INTO table (id, value) VALUES (%(id)s, %(value)s)",
                 dict(id=1, value="v1"),
             )

         Single tuple::

             conn.exec_driver_sql("INSERT INTO table (id, value) VALUES (?, ?)", (1, "v1"))

        """
        with self.engine.begin() as conn:
            return conn.exec_driver_sql(raw_sql, parameters, execution_options)

    # ------------------------------------------------------------------ #
    #                          Prepare Database                          #
    # ------------------------------------------------------------------ #

    def bootstrap(self, reset_on_failure: bool = False):
        self._rescue_if_unreadable()
        self._ensure_database()
        try:
            from alembic import command

            base = self.base_revision()
            if base and self.has_any_tables() and not self.current_revision():
                command.stamp(self.alembic_config, base)
            command.upgrade(self.alembic_config, "head")
            logger.info("Database bootstrap successful.")
            self._verify_schema()
        except Exception:
            # Starting over empty is for a database with nothing in it. One that
            # holds a library, or cannot be read to tell, is never wiped: the
            # desktop app asked for this on every start, and an unreadable
            # library was only spared because it could not even be opened.
            if not reset_on_failure or self._holds_library():
                raise
            self._reset_database()
            self.bootstrap()

    def _database_file(self) -> Optional[Path]:
        url = make_url(ctx.config.db.url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            return None
        return Path(url.database)

    def _holds_library(self) -> bool:
        """Whether the database has novels in it, or cannot be read to tell (then: assume so)."""
        try:
            with self.engine.connect() as conn:
                if "novels" not in sa.inspect(conn).get_table_names():
                    return False
                return bool(conn.exec_driver_sql("SELECT 1 FROM novels LIMIT 1").first())
        except Exception:
            return True

    def _rescue_if_unreadable(self) -> None:
        """Get a database that cannot be read back before anything opens it (see db_rescue)."""
        from ..utils import db_rescue
        from ..utils.db_rescue import rescue, unreadable

        database = self._database_file()
        if database is None:
            return
        # the copies kept where the app starts from count too
        base = getattr(ctx.config.app, "base_dir", None)
        db_rescue.also_look_in = [Path(base)] if base else []
        error = unreadable(database, ctx.config.app.app_dir)
        if error is None:
            return
        logger.error(f"The database {database} cannot be read: {error}; trying to get it back")
        self.rescued = rescue(database, ctx.config.app.app_dir, error)
        logger.warning(self.rescued)

    # ------------------------------------------------------------------ #
    #                         Automatic copies                           #
    # ------------------------------------------------------------------ #

    def start_snapshots(self) -> None:
        """Keep a copy of the database from the last 12 hours, and the one before, in the library.

        So that a database file damaged by a drive that lost power is never the
        only copy of a library's list of novels.
        """
        if self._database_file() is None or self._snapshotting.is_set():
            return
        self._snapshotting.set()
        Thread(target=self._snapshot_loop, daemon=True, name="db-snapshots").start()

    def snapshot(self) -> Optional[Path]:
        from ..utils.db_rescue import take_snapshot

        database = self._database_file()
        if database is None or not database.is_file():
            return None
        made = take_snapshot(database, ctx.config.app.app_dir)
        if made is not None:
            logger.info(f"Copied the database to {made}")
            self._snapshot_elsewhere(made)
        else:
            logger.warning("Not enough room on the library's drive for a copy of the database")
        return made

    def _snapshot_elsewhere(self, made: Path) -> None:
        """A second copy where the app starts from, when the library is on another drive."""
        from ..utils.db_rescue import keep_elsewhere

        library, base = Path(ctx.config.app.app_dir), Path(ctx.config.app.base_dir)
        if (
            os.path.splitdrive(os.path.abspath(library))[0].lower()
            == (os.path.splitdrive(os.path.abspath(base))[0].lower())
            and os.name == "nt"
        ):
            return  # the same drive: nothing gained
        if os.path.abspath(library) == os.path.abspath(base):
            return
        try:
            kept = keep_elsewhere(made, base)
        except OSError as error:
            logger.warning(f"Could not keep a copy of the database in {base}: {error}")
            return
        if kept is not None:
            logger.info(f"Also kept in {kept}")

    def _snapshot_loop(self) -> None:
        from ..utils.db_rescue import snapshots

        # not straight away: the first minutes of a start are busy enough
        while not self._closing.wait(SNAPSHOT_FIRST):
            try:
                newest = snapshots(ctx.config.app.app_dir)
                age = time.time() - newest[0].stat().st_mtime if newest else None
                if age is None or age >= SNAPSHOT_EVERY:
                    self.snapshot()
            except Exception as error:
                logger.warning(f"Could not copy the database: {error}")
            if self._closing.wait(SNAPSHOT_CHECK):
                return

    @cached_property
    def alembic_config(self):
        from alembic.config import Config

        cfg = Config()
        migration_path = Path(__file__).parent.parent / "migrations"
        cfg.set_main_option("sqlalchemy.url", ctx.config.db.url)
        cfg.set_main_option("dialect", self.engine.dialect.name)
        cfg.set_main_option("script_location", migration_path.as_posix())
        cfg.set_main_option(
            "file_template",
            r"%%(year)d_%%(month).2d_%%(day).2d_%%(hour).2d_%%(minute).2d_%%(second).2d_%%(slug)s",
        )
        cfg.set_section_option("post_write_hooks", "hooks", "black")
        cfg.set_section_option("post_write_hooks", "black.type", "console_scripts")
        cfg.set_section_option("post_write_hooks", "black.entrypoint", "black")
        cfg.set_section_option("post_write_hooks", "black.options", "REVISION_SCRIPT_FILENAME")
        return cfg

    @cached_property
    def alembic_script(self):
        from alembic.script import ScriptDirectory

        return ScriptDirectory.from_config(self.alembic_config)

    def base_revision(self):
        return self.alembic_script.get_base()

    def latest_revision(self):
        return self.alembic_script.get_current_head()

    def has_any_tables(self):
        with self.engine.connect() as conn:
            return bool(sa.inspect(conn).get_table_names())

    def current_revision(self):
        from alembic.runtime.migration import MigrationContext

        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            return context.get_current_revision()

    def _create_engine(self, db_url: str, **kwargs):
        kwargs.setdefault("echo", ctx.logger.is_debug)

        # Connection arguments for database-specific settings
        connect_args: dict = kwargs.setdefault("connect_args", {})
        if "postgres" in db_url or "mysql" in db_url:
            connect_args.setdefault("connect_timeout", ctx.config.db.connect_timeout)

        # Pool configuration for connection management
        kwargs.setdefault("pool_size", ctx.config.db.pool_size)
        kwargs.setdefault("pool_timeout", ctx.config.db.pool_timeout)
        kwargs.setdefault("pool_recycle", ctx.config.db.pool_recycle)

        # Maximum overflow connections allowed
        kwargs.setdefault("max_overflow", ctx.config.db.pool_size * 3)

        # Test connections before using them (handles disconnects gracefully)
        kwargs.setdefault("pool_pre_ping", True)

        # Create the engine
        engine = sa.create_engine(db_url, **kwargs)
        if ctx.logger.is_debug:
            engine.logger = logger

        # SQLite: enable WAL so readers (the Reader UI, library lists, status
        # polls) never block the single writer (a running export) and vice versa,
        # and wait out brief lock contention instead of failing with "database is
        # locked". Without this, actively reading while an export runs makes the
        # export's chapter writes stall behind read transactions.
        if engine.dialect.name == "sqlite":
            from sqlalchemy import event

            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.close()

        return engine

    def _reset_database(self):
        logger.debug("Resetting database...")
        with self.engine.begin() as conn:
            metadata = sa.MetaData()
            metadata.reflect(bind=conn)
            metadata.drop_all(bind=conn)
        self.close()
        logger.info("Database reset.")

    def _ensure_database(self, max_retries=10) -> None:
        """Create the database if it doesn't exist (MySQL and PostgreSQL only)."""
        url = make_url(ctx.config.db.url)
        logger.info(f"Database: '{url}'")

        scheme = url.drivername
        database = url.database

        if "sqlite" in scheme:
            return

        if not database:
            raise ValueError("No database name found in the URL")

        if "mysql" in scheme:
            server_url = url.set(database=None)
            check_query = sa.text(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :db_name"
            )
            create_query = sa.text(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        elif "postgres" in scheme:
            server_url = url.set(database="postgres")
            check_query = sa.text("SELECT 1 FROM pg_database WHERE datname = :db_name")
            create_query = sa.text(f'CREATE DATABASE "{database}"')
        else:
            raise ValueError(f"Unsupported database scheme: {scheme}")

        engine = self._create_engine(server_url.render_as_string(hide_password=False))
        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(f"Ensuring database '{database}' exists...")
                # AUTOCOMMIT is required for PostgreSQL; it is harmless for MySQL.
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    result = conn.execute(check_query, {"db_name": database})
                    if result.fetchone() is None:
                        logger.info(f"Creating database '{database}'.")
                        conn.execute(create_query)
                        logger.info(f"Database '{database}' created.")
                engine.dispose()
                return
            except BaseException as e:
                if attempt == max_retries:
                    engine.dispose()
                    raise RuntimeError("Could not create database") from e
                logger.critical(
                    f"Could not connect to database. Retrying... ({attempt}/{max_retries})",
                    exc_info=ctx.logger.is_debug,
                )
                time.sleep(1)

    def _verify_schema(self):
        logger.debug("Verifying database schema...")
        from alembic.autogenerate import compare_metadata
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import Enum as SAEnum

        dialect = self.engine.dialect.name

        def _compare_type(*args):
            # SQLite has no native enum type — enums are stored as VARCHAR
            metadata_type = args[4]
            if dialect == "sqlite" and isinstance(metadata_type, SAEnum):
                return False
            return None

        with self.engine.connect() as conn:
            mc = MigrationContext.configure(
                conn,
                opts={
                    "compare_type": _compare_type,
                    # "compare_server_default": True,
                },
            )

            drift = list(compare_metadata(mc, SQLModel.metadata))
            if drift:
                logger.warning(f"Detected {len(drift)} schema drift(s) against models:")
                for op in drift:
                    logger.warning(f"  - {self._format_drift(op)}")
                logger.warning("Either drop the database, or manually fix these drifts.")
                raise ValueError("Database schema is not valid.")
            else:
                logger.info("Database schema is valid.")

    @staticmethod
    def _format_drift(op: Any) -> str:
        # alembic groups index/fk diffs inside a single-element list
        if isinstance(op, list):
            return ", ".join(DB._format_drift(x) for x in op)
        if not isinstance(op, tuple) or len(op) < 2:
            return repr(op)

        name, payload = op[0], op[1]
        if name in ("add_table", "remove_table"):
            return f"{name}: {getattr(payload, 'name', payload)}"
        if name in ("add_column", "remove_column") and len(op) >= 4:
            col = op[3]
            return f"{name}: {op[2]}.{col.name} ({col.type})"
        if name.startswith("modify_") and len(op) >= 7:
            _, _schema, table, column, _kwargs, old, new = op[:7]
            return f"{name}: {table}.{column}: {old!r} -> {new!r}"
        if name in ("add_index", "remove_index", "add_fk", "remove_fk"):
            return f"{name}: {getattr(payload, 'name', payload)}"
        return repr(op)
