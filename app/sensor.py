# UNIFIED SERVER CHANGE: sensor ESP now connects to WS /sensor.
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from .config import settings
from .database import engine
from .device_manager import ConnectedDevice, device_manager
from .models import Device, DeviceStatus, SensorData
from .services import get_or_create_device, pairing_secrets
from .telegram_bot import send_rain_alert, telegram_application

router = APIRouter()


def now() -> datetime:
    return datetime.now(UTC)


async def handle_sensor(device_id: str, message: dict):
    with Session(engine) as session:
        device = session.exec(
            select(Device).where(Device.device_id == device_id)
        ).first()

        if not device:
            return None, None

        timestamp = now()
        if message.get("timestamp"):
            try:
                timestamp = datetime.fromisoformat(
                    message["timestamp"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        row = SensorData(
            device_id=device.id,
            rain=bool(message.get("rain", False)),
            temperature=message.get("temperature"),
            pressure=message.get("pressure"),
            humidity=message.get("humidity"),
            timestamp=timestamp,
        )

        session.add(row)
        device.last_seen = now()
        session.add(device)
        session.commit()
        session.refresh(row)
        session.refresh(device)

        should_alert = row.rain and device.owner_id is not None
        return device, row if should_alert else None


@router.websocket("/sensor")
async def sensor_websocket(websocket: WebSocket):
    await websocket.accept()
    device_id = None

    try:
        hello = await websocket.receive_json()

        if hello.get("type") != "hello":
            await websocket.close(code=1002)
            return

        device_id = str(hello["device_id"])
        firmware = str(hello.get("firmware_version", "unknown"))

        with Session(engine) as session:
            device = get_or_create_device(session, device_id, firmware)
            paired = device.owner_id is not None

            device.status = (
                DeviceStatus.ONLINE
                if paired
                else DeviceStatus.WAITING_PAIR
            )
            device.last_seen = now()
            session.add(device)
            session.commit()

        connection = ConnectedDevice(
            websocket=websocket,
            device_id=device_id,
            firmware_version=firmware,
            paired=paired,
        )
        await device_manager.register(connection)

        await websocket.send_json({
            "type": "hello_ack",
            "paired": paired,
            "server_time": now().isoformat(),
        })

        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            if message_type == "heartbeat":
                with Session(engine) as session:
                    device = session.exec(
                        select(Device).where(Device.device_id == device_id)
                    ).first()

                    if device:
                        device.last_seen = now()
                        session.add(device)
                        session.commit()

                await websocket.send_json({
                    "type": "heartbeat_ack",
                    "timestamp": now().isoformat(),
                })

            elif message_type == "sensor":
                device, alert_sensor = await handle_sensor(device_id, message)

                await websocket.send_json({
                    "type": "sensor_ack",
                    "id": message.get("id"),
                })

                if device and alert_sensor and telegram_application:
                    await send_rain_alert(
                        telegram_application.bot,
                        device,
                        alert_sensor,
                    )

            elif message_type == "offline_batch":
                count = 0
                for item in message.get("items", []):
                    item["type"] = "sensor"
                    device, alert_sensor = await handle_sensor(device_id, item)
                    count += 1

                    if device and alert_sensor and telegram_application:
                        await send_rain_alert(
                            telegram_application.bot,
                            device,
                            alert_sensor,
                        )

                await websocket.send_json({
                    "type": "offline_batch_ack",
                    "count": count,
                })

            elif message_type == "pairing_secret":
                secret = str(message.get("secret", ""))
                pairing_secrets.set(device_id, secret)

                await websocket.send_json({
                    "type": "pairing_secret_ack",
                    "expires_seconds": settings.device_secret_ttl_seconds,
                })

            elif message_type == "status":
                with Session(engine) as session:
                    device = session.exec(
                        select(Device).where(Device.device_id == device_id)
                    ).first()
                    if device:
                        device.last_seen = now()
                        session.add(device)
                        session.commit()

            elif message_type == "motor_done":
                pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print("WebSocket error:", repr(exc))
    finally:
        if device_id:
            await device_manager.remove(device_id, websocket)

            with Session(engine) as session:
                device = session.exec(
                    select(Device).where(Device.device_id == device_id)
                ).first()

                if device:
                    device.status = DeviceStatus.OFFLINE
                    session.add(device)
                    session.commit()
