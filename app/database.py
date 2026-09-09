from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


# RENDER/DB CHANGE:
# Use the same Pydantic settings object as the rest of the application instead
# of reading os.getenv() separately. This supports both local .env files and
# Render environment variables.
DATABASE_URL = settings.database_url

# RENDER/DB CHANGE:
# requirements.txt uses psycopg v3 ("psycopg[binary]"). Render supplies a URL
# such as "postgresql://...", while SQLAlchemy needs "+psycopg" to explicitly
# select the psycopg v3 driver rather than trying to import psycopg2.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1,
    )
elif DATABASE_URL.startswith("postgres://"):
    # Compatibility with providers that still return the older postgres:// form.
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql+psycopg://",
        1,
    )

# SQLite needs this option; PostgreSQL does not.
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def init_db() -> None:
    # Existing behavior retained: creates any missing SQLModel/SQLAlchemy tables
    # when the application starts. This works for the current prototype.
    SQLModel.metadata.create_all(bind=engine)

    from sqlalchemy import inspect

    inspector = inspect(engine)

    print(
        "Database tables:",
        inspector.get_table_names()
    )

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
