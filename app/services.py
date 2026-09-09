from __future__ import annotations

import csv
import io
import secrets
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from .config import settings
from .models import Device, DeviceStatus, PairingHistory, SensorData, Settings, User


def get_or_create_user(session: Session, telegram_id: str, username: str) -> User:
    user = session.exec(
        select(User).where(User.telegram_id == telegram_id)
    ).first()

    if user:
        user.username = username or user.username
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    user = User(telegram_id=telegram_id, username=username or "")
    session.add(user)
    session.commit()
    session.refresh(user)

    session.add(Settings(user_id=user.id))
    session.commit()
    return user


def get_user_devices(session: Session, user: User) -> list[Device]:
    return list(session.exec(
        select(Device)
        .where(Device.owner_id == user.id)
        .order_by(Device.device_id)
    ).all())


def get_or_create_device(session: Session, device_id: str, firmware_version: str) -> Device:
    device = session.exec(
        select(Device).where(Device.device_id == device_id)
    ).first()

    if not device:
        device = Device(
            device_id=device_id,
            firmware_version=firmware_version,
            status=DeviceStatus.WAITING_PAIR,
        )
        session.add(device)
    else:
        device.firmware_version = firmware_version

    device.last_seen = datetime.now(UTC)
    session.add(device)
    session.commit()
    session.refresh(device)
    return device


def pair_device(session: Session, user: User, device: Device) -> None:
    device.owner_id = user.id
    device.status = DeviceStatus.ONLINE
    device.last_seen = datetime.now(UTC)
    session.add(device)
    session.add(PairingHistory(
        device_id=device.id,
        telegram_user=user.telegram_id,
    ))
    session.commit()


def get_settings(session: Session, user: User) -> Settings:
    value = session.exec(
        select(Settings).where(Settings.user_id == user.id)
    ).first()

    if value:
        return value

    value = Settings(user_id=user.id)
    session.add(value)
    session.commit()
    session.refresh(value)
    return value


def csv_for_device(session: Session, device: Device, limit: int = 5000) -> io.BytesIO:
    rows = session.exec(
        select(SensorData)
        .where(SensorData.device_id == device.id)
        .order_by(SensorData.timestamp.desc())
        .limit(limit)
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "device_id",
        "rain",
        "temperature_C",
        "pressure_hPa",
        "humidity_percent",
        "timestamp_UTC",
    ])

    for row in reversed(rows):
        writer.writerow([
            row.id,
            device.device_id,
            row.rain,
            row.temperature,
            row.pressure,
            row.humidity,
            row.timestamp.isoformat(),
        ])

    return io.BytesIO(output.getvalue().encode("utf-8"))


def generate_pair_secret() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


class PairingSecrets:
    # In-memory, short-lived, one-time pairing secrets.
    def __init__(self):
        self._values: dict[str, tuple[str, datetime]] = {}

    def set(self, device_id: str, secret: str):
        self._values[device_id] = (
            secret,
            datetime.now(UTC) + timedelta(seconds=settings.device_secret_ttl_seconds),
        )

    def consume(self, device_id: str, secret: str) -> bool:
        value = self._values.get(device_id)
        if not value:
            return False

        expected, expires = value

        if datetime.now(UTC) > expires:
            self._values.pop(device_id, None)
            return False

        if not secrets.compare_digest(expected, secret):
            return False

        self._values.pop(device_id, None)
        return True


pairing_secrets = PairingSecrets()
