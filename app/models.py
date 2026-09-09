from datetime import UTC, datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    """
    PostgreSQL-compatible timezone-aware UTC timestamp.

    CHANGE:
    Use one helper for all timestamp defaults so every datetime inserted
    by the application is timezone-aware.
    """
    return datetime.now(UTC)


class DeviceStatus(str, Enum):
    OFFLINE = "offline"
    ONLINE = "online"
    WAITING_PAIR = "waiting_pair"


class User(SQLModel, table=True):
    # Existing table name is made explicit so PostgreSQL and SQLModel use
    # exactly the same name expected by the existing foreign keys.
    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)

    # CHANGE:
    # Explicit SQL column types are used for PostgreSQL instead of relying
    # completely on SQLite/SQLModel type inference.
    telegram_id: str = Field(
        sa_type=String,
        index=True,
        unique=True,
        nullable=False,
    )
    username: str = Field(default="", sa_type=String, nullable=False)
    g_drive: Optional[str] = Field(default=None, sa_type=String)

    # CHANGE:
    # DateTime(timezone=True) maps to PostgreSQL TIMESTAMP WITH TIME ZONE.
    created_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )

    devices: list["Device"] = Relationship(back_populates="owner")
    settings: Optional["Settings"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"uselist": False},
    )


class Device(SQLModel, table=True):
    __tablename__ = "device"

    id: Optional[int] = Field(default=None, primary_key=True)

    device_id: str = Field(
        sa_type=String,
        index=True,
        unique=True,
        nullable=False,
    )
    device_type: str = Field(
        default="ESP32",
        sa_type=String,
        nullable=False,
    )
    firmware_version: str = Field(
        default="0.1.0",
        sa_type=String,
        index=True,
        nullable=False,
    )

    owner_id: Optional[int] = Field(
        default=None,
        foreign_key="user.id",
        index=True,
    )

    last_seen: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )

    # CHANGE:
    # Store DeviceStatus as VARCHAR instead of a PostgreSQL native ENUM.
    # This keeps the existing string values ("offline", "online",
    # "waiting_pair") and avoids needing a separate PostgreSQL ENUM type.
    status: DeviceStatus = Field(
        default=DeviceStatus.WAITING_PAIR,
        sa_type=String,
        index=True,
        nullable=False,
    )

    created_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )

    owner: Optional[User] = Relationship(back_populates="devices")
    sensor_data: list["SensorData"] = Relationship(back_populates="device")
    pairing_history: list["PairingHistory"] = Relationship(back_populates="device")


class SensorData(SQLModel, table=True):
    __tablename__ = "sensordata"

    # Existing application behavior is preserved: UUID is generated in
    # Python and stored as a string rather than changing your API/schema
    # to PostgreSQL's native UUID type.
    id: str = Field(
        default_factory=lambda: str(uuid4()),
        sa_type=String,
        primary_key=True,
    )

    device_id: int = Field(
        foreign_key="device.id",
        index=True,
        nullable=False,
    )

    rain: bool = Field(sa_type=Boolean, nullable=False)
    temperature: Optional[float] = Field(default=None, sa_type=Float)
    pressure: Optional[float] = Field(default=None, sa_type=Float)
    humidity: Optional[float] = Field(default=None, sa_type=Float)

    timestamp: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )

    device: Optional[Device] = Relationship(back_populates="sensor_data")


class Settings(SQLModel, table=True):
    __tablename__ = "settings"

    id: Optional[int] = Field(default=None, primary_key=True)

    user_id: int = Field(
        foreign_key="user.id",
        unique=True,
        index=True,
        nullable=False,
    )

    update_frequency: int = Field(
        default=15,
        sa_type=Integer,
        nullable=False,
    )
    get_photo_update: bool = Field(
        default=False,
        sa_type=Boolean,
        nullable=False,
    )
    action_waiting_time: int = Field(
        default=300,
        sa_type=Integer,
        nullable=False,
    )

    user: Optional[User] = Relationship(back_populates="settings")


class PairingHistory(SQLModel, table=True):
    __tablename__ = "pairinghistory"

    id: Optional[int] = Field(default=None, primary_key=True)

    device_id: int = Field(
        foreign_key="device.id",
        index=True,
        nullable=False,
    )

    telegram_user: str = Field(
        sa_type=String,
        index=True,
        nullable=False,
    )

    timestamp: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )

    device: Optional[Device] = Relationship(back_populates="pairing_history")
