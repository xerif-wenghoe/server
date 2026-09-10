from datetime import UTC, datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, Relationship, SQLModel


class DeviceStatus(str, Enum):
    OFFLINE = "offline"
    ONLINE = "online"
    WAITING_PAIR = "waiting_pair"


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: str = Field(index=True, unique=True)
    username: str = Field(default="")
    g_drive: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    devices: list["Device"] = Relationship(back_populates="owner")
    settings: Optional["Settings"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"uselist": False},
    )


class Device(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: str = Field(index=True, unique=True)
    device_type: str = "ESP32"
    firmware_version: str = Field(default="0.1.0", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: DeviceStatus = Field(default=DeviceStatus.WAITING_PAIR, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    owner: Optional[User] = Relationship(back_populates="devices")
    sensor_data: list["SensorData"] = Relationship(back_populates="device")
    pairing_history: list["PairingHistory"] = Relationship(back_populates="device")


class SensorData(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    rain: bool
    temperature: Optional[float] = None
    pressure: Optional[float] = None
    humidity: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    device: Optional[Device] = Relationship(back_populates="sensor_data")


class Settings(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)
    update_frequency: int = 15
    get_photo_update: bool = False
    action_waiting_time: int = 300

    user: Optional[User] = Relationship(back_populates="settings")


class PairingHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: int = Field(foreign_key="device.id", index=True)
    telegram_user: str = Field(index=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    device: Optional[Device] = Relationship(back_populates="pairing_history")