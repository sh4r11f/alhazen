"""The hub's relational schema and its version.

Hides: table layout, column types chosen to behave the same on SQLite
(local development) and PostgreSQL (deployment), and the migration steps
between schema versions.

Rules (docs/hub/server.md "Schema and migrations"):

- `METADATA` always describes the newest schema. A fresh database is created
  directly at `SCHEMA_VERSION` by `create_schema`.
- An existing database is upgraded only by `MIGRATIONS`, explicit steps from
  version N to N+1, run by an operator (`admin.migrate_database`). The
  service itself never creates, alters or drops anything on start; it checks
  the version and refuses to serve a database it does not match.
- Nothing here ever drops a table holding user data.

Times are integer milliseconds since the Unix epoch (UTC). JSON-valued
columns are TEXT holding canonical JSON, so both backends store them alike.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Connection,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    inspect,
    select,
)

SCHEMA_VERSION = 2
ID = 32  # opaque random identifiers: 32 lowercase hex characters

METADATA = MetaData()

schema_meta = Table(
    "hub_schema",
    METADATA,
    Column("key", String(64), primary_key=True),
    Column("value", String(200), nullable=False),
)

users = Table(
    "hub_users",
    METADATA,
    Column("id", String(ID), primary_key=True),
    Column("username", String(32), nullable=False, unique=True),
    Column("display_name", String(80), nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("password_changed_at", BigInteger, nullable=False),
    Column("disabled_at", BigInteger, nullable=True),
)

invites = Table(
    "hub_invites",
    METADATA,
    Column("id", String(ID), primary_key=True),
    Column("code_hash", String(64), nullable=False, unique=True),
    Column("note", String(200), nullable=False),
    Column("created_by", String(100), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    # Single use (review gate M1): consumed by one registration.
    Column("used_at", BigInteger, nullable=True),
    Column("used_by", String(ID), nullable=True),
    Column("revoked_at", BigInteger, nullable=True),
)

auth_sessions = Table(
    "hub_auth_sessions",
    METADATA,
    Column("id", String(ID), primary_key=True),
    Column("user_id", String(ID), ForeignKey("hub_users.id"), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("kind", String(10), nullable=False),  # "cookie" | "bearer"
    Column("created_at", BigInteger, nullable=False),
    Column("last_seen_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("revoked_at", BigInteger, nullable=True),
    Index("ix_hub_auth_sessions_user", "user_id"),
)

auth_attempts = Table(
    "hub_auth_attempts",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope", String(16), nullable=False),  # "user" | "address" | "register"
    Column("subject", String(128), nullable=False),
    Column("at", BigInteger, nullable=False),
    Column("success", Boolean, nullable=False),
    Index("ix_hub_auth_attempts_lookup", "scope", "subject", "at"),
)

audit_events = Table(
    "hub_audit",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", BigInteger, nullable=False),
    Column("actor", String(100), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target", String(200), nullable=False),
    Column("detail", Text, nullable=False),
)

experiments = Table(
    "hub_experiments",
    METADATA,
    Column("id", String(ID), primary_key=True),
    Column("owner_id", String(ID), ForeignKey("hub_users.id"), nullable=False),
    Column("title", String(120), nullable=False),
    Column("summary", String(280), nullable=False),
    Column("description", Text, nullable=False),
    Column("license", String(100), nullable=False),
    Column("citations", Text, nullable=False),
    Column("tags", Text, nullable=False),
    # Bound by the first uploaded version; every later version must match.
    Column("package_name", String(64), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Index("ix_hub_experiments_owner", "owner_id"),
)

versions = Table(
    "hub_versions",
    METADATA,
    Column("id", String(ID), primary_key=True),
    Column("experiment_id", String(ID), ForeignKey("hub_experiments.id"), nullable=False),
    Column("version", String(64), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("manifest", Text, nullable=False),
    Column("storage_key", String(300), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("experiment_id", "version", name="uq_hub_versions_version"),
)

# The public face of an experiment (review gate M2): every public field is
# frozen here at the explicit publish, with the one published version. The
# owner's later edits to hub_experiments and new versions change nothing a
# visitor sees until they publish again. One row per published experiment;
# unpublishing deletes it (the audit log keeps the history).
publications = Table(
    "hub_publications",
    METADATA,
    Column("experiment_id", String(ID), ForeignKey("hub_experiments.id"), primary_key=True),
    Column("version_id", String(ID), ForeignKey("hub_versions.id"), nullable=False),
    Column("title", String(120), nullable=False),
    Column("summary", String(280), nullable=False),
    Column("description", Text, nullable=False),
    Column("license", String(100), nullable=False),
    Column("citations", Text, nullable=False),
    Column("tags", Text, nullable=False),
    Column("owner_username", String(32), nullable=False),
    Column("owner_display_name", String(80), nullable=False),
    Column("published_at", BigInteger, nullable=False),
)

library = Table(
    "hub_library",
    METADATA,
    Column("user_id", String(ID), ForeignKey("hub_users.id"), nullable=False),
    Column("experiment_id", String(ID), ForeignKey("hub_experiments.id"), nullable=False),
    Column("version_id", String(ID), ForeignKey("hub_versions.id"), nullable=False),
    # The title the user saw when pinning, so an entry stays recognisable
    # after its experiment is unpublished.
    Column("title", String(120), nullable=False),
    Column("added_at", BigInteger, nullable=False),
    PrimaryKeyConstraint("user_id", "experiment_id"),
)

data_sessions = Table(
    "hub_sessions",
    METADATA,
    Column("id", String(ID), primary_key=True),
    Column("owner_id", String(ID), ForeignKey("hub_users.id"), nullable=False),
    Column("experiment_id", String(ID), ForeignKey("hub_experiments.id"), nullable=False),
    Column("version_id", String(ID), ForeignKey("hub_versions.id"), nullable=False),
    Column("client_session_id", String(128), nullable=False),
    # Raw upload state (review gate B1): "staging" -> "sealing" -> "committed";
    # "aborted" / "expired" release the reservation of an unfinished upload.
    Column("status", String(16), nullable=False),
    Column("metadata", Text, nullable=False),
    Column("subject_code", String(64), nullable=True),  # copy of metadata, for filtering
    Column("mode", String(32), nullable=True),
    Column("manifest_sha256", String(64), nullable=False),
    Column("total_bytes", BigInteger, nullable=False),
    Column("file_count", Integer, nullable=False),
    Column("storage_key", String(300), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("completed_at", BigInteger, nullable=True),
    # The current seal attempt (status sealing): a random token naming the
    # sealer and the time its lease ends. Every seal step after the claim is
    # conditional on the token, so a sealer whose lease was taken over can
    # neither commit, reset nor release anything (schema 2).
    Column("seal_lease_until", BigInteger, nullable=True),
    Column("seal_token", String(64), nullable=True),
    # A condition an operator or the owner must act on: "artifact_conflict"
    # (a stored final copy disagrees; the seal stopped) or "artifact_missing"
    # (a committed session's stored files are gone). Schema 2.
    Column("problem_code", String(32), nullable=True),
    Column("problem_at", BigInteger, nullable=True),
    # Retry of a closed (aborted/expired) upload: the new attempt names the
    # attempt it replaces; the closed row keeps its original client id here
    # and gives up the unique (owner, client_session_id) key. Schema 2.
    Column("previous_attempt_id", String(ID), nullable=True),
    Column("retired_client_id", String(128), nullable=True),
    # Derived trial index state, independent of the raw state:
    # "none" | "pending" | "indexing" | "indexed" | "partial" | "failed".
    Column("index_status", String(16), nullable=False),
    Column("index_claimed_until", BigInteger, nullable=True),
    # The current index job's token (schema 2): a rebuild requested while a
    # job runs, or a takeover after its claim lapsed, fences the old job.
    Column("index_token", String(64), nullable=True),
    Column("index_rows", Integer, nullable=False),
    Column("index_error", String(500), nullable=True),
    Column("index_columns", Text, nullable=True),
    UniqueConstraint("owner_id", "client_session_id", name="uq_hub_sessions_client"),
    Index("ix_hub_sessions_owner_status", "owner_id", "status"),
)

session_files = Table(
    "hub_session_files",
    METADATA,
    Column("session_id", String(ID), ForeignKey("hub_sessions.id"), nullable=False),
    Column("path", String(1024), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("received", BigInteger, nullable=False),
    Column("verified", Boolean, nullable=False),
    PrimaryKeyConstraint("session_id", "path"),
)

# Every acknowledged chunk (review gate B1): a retry of the same
# (offset, length, sha256) is a replay; anything else at a covered offset is
# a conflict. Rows go away with a file reset after a failed verification.
session_chunks = Table(
    "hub_session_chunks",
    METADATA,
    Column("session_id", String(ID), ForeignKey("hub_sessions.id"), nullable=False),
    Column("path", String(1024), nullable=False),
    Column("offset", BigInteger, nullable=False),
    Column("length", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    PrimaryKeyConstraint("session_id", "path", "offset"),
)

trial_rows = Table(
    "hub_trial_rows",
    METADATA,
    Column("session_id", String(ID), ForeignKey("hub_sessions.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("source_path", String(1024), nullable=False),
    Column("row_values", Text, nullable=False),
    PrimaryKeyConstraint("session_id", "ordinal"),
)


def _v1_to_v2(conn: Connection) -> None:
    """Schema 2: seal tokens, problem marks, retried attempts and index tokens.

    Only nullable columns are added (portable ADD COLUMN on SQLite and
    PostgreSQL); existing rows keep NULL, which every reader treats as "none".
    """
    for name, kind in (
        ("seal_token", "VARCHAR(64)"),
        ("problem_code", "VARCHAR(32)"),
        ("problem_at", "BIGINT"),
        ("previous_attempt_id", "VARCHAR(32)"),
        ("retired_client_id", "VARCHAR(128)"),
        ("index_token", "VARCHAR(64)"),
    ):
        conn.exec_driver_sql(f"ALTER TABLE hub_sessions ADD COLUMN {name} {kind}")


# Steps that upgrade an existing database from version N to N+1, keyed by N.
MIGRATIONS: dict[int, Callable[[Connection], None]] = {1: _v1_to_v2}


class SchemaError(RuntimeError):
    """The database is not at the schema this code serves."""


def read_version(conn: Connection) -> int | None:
    """The stored schema version, or None for a database with no hub schema."""
    if not inspect(conn).has_table(schema_meta.name):
        return None
    value = conn.execute(
        select(schema_meta.c.value).where(schema_meta.c.key == "schema_version")
    ).scalar_one_or_none()
    if value is None:
        raise SchemaError("hub_schema exists but records no schema_version; refusing to guess")
    return int(value)


def check_version(conn: Connection) -> None:
    """Refuse a database that is not exactly at `SCHEMA_VERSION`."""
    found = read_version(conn)
    if found is None:
        raise SchemaError(
            "the database has no hub schema: initialise it once with "
            "`alhazen.hub.admin.init_database` (never done automatically)"
        )
    if found < SCHEMA_VERSION:
        raise SchemaError(
            f"the database is at hub schema {found}, this code serves {SCHEMA_VERSION}: "
            "back it up, "
            "then run `alhazen.hub.admin.migrate_database`"
        )
    if found > SCHEMA_VERSION:
        raise SchemaError(
            f"the database is at hub schema {found}, newer than this code ({SCHEMA_VERSION}); "
            "run the alhazen version that wrote it"
        )


def create_schema(conn: Connection) -> None:
    """Create every table at `SCHEMA_VERSION` in an empty database."""
    existing = set(inspect(conn).get_table_names())
    ours = {table.name for table in METADATA.sorted_tables}
    if existing & ours:
        raise SchemaError(
            "some hub tables already exist without a complete schema record "
            f"({', '.join(sorted(existing & ours))}); refusing to create over them"
        )
    METADATA.create_all(conn)
    conn.execute(schema_meta.insert().values(key="schema_version", value=str(SCHEMA_VERSION)))


def migrate(conn: Connection) -> int:
    """Apply each pending step in order; return the version reached."""
    found = read_version(conn)
    if found is None:
        raise SchemaError("the database has no hub schema to migrate; initialise it instead")
    if found > SCHEMA_VERSION:
        check_version(conn)
    while found < SCHEMA_VERSION:
        step = MIGRATIONS.get(found)
        if step is None:
            raise SchemaError(f"no migration from hub schema {found}")
        step(conn)
        found += 1
        conn.execute(
            schema_meta.update()
            .where(schema_meta.c.key == "schema_version")
            .values(value=str(found))
        )
    return found
