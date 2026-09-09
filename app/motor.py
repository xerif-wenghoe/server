from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

router = APIRouter()


@dataclass
class MotorDeviceConnection:
    websocket: WebSocket
    device_id: str
    system_id: str
    firmware_version: str


class MotorConnectionManager:
    """Tracks the always-awake ESP #3 WebSocket by System ID."""

    def __init__(self):
        self._devices: dict[str, MotorDeviceConnection] = {}
        self._lock = asyncio.Lock()
        self._controllers: set[WebSocket] = set()

    async def register_device(self, conn: MotorDeviceConnection):
        async with self._lock:
            old = self._devices.get(conn.system_id)
            if old and old.websocket is not conn.websocket:
                try:
                    await old.websocket.close(code=1012)
                except Exception:
                    pass
            self._devices[conn.system_id] = conn

    async def remove_device(self, system_id: str, websocket: WebSocket):
        async with self._lock:
            current = self._devices.get(system_id)
            if current and current.websocket is websocket:
                self._devices.pop(system_id, None)

    async def add_controller(self, websocket: WebSocket):
        async with self._lock:
            self._controllers.add(websocket)

    async def remove_controller(self, websocket: WebSocket):
        async with self._lock:
            self._controllers.discard(websocket)

    async def send_for_system(self, system_id: str, command: str) -> bool:
        async with self._lock:
            conn = self._devices.get(system_id)
        if not conn:
            return False
        try:
            await conn.websocket.send_text(command)
            return True
        except Exception:
            return False

    async def send_any(self, command: str) -> bool:
        """Prototype fallback for the current one-system deployment."""
        async with self._lock:
            conn = next(iter(self._devices.values()), None)
        if not conn:
            return False
        try:
            await conn.websocket.send_text(command)
            return True
        except Exception:
            return False

    async def broadcast_status(self, payload: dict):
        message = json.dumps(payload)
        async with self._lock:
            clients = list(self._controllers)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._controllers.discard(ws)


motor_manager = MotorConnectionManager()


@router.websocket("/motor")
async def motor_websocket(websocket: WebSocket):
    """
    One Render WebSocket endpoint for ESP #3 and the optional browser controller.

    ESP #3 connects to:  wss://<host>/motor
    Browser UI connects: wss://<host>/motor?client=control&system_id=<system id>
    """
    await websocket.accept()
    client_type = websocket.query_params.get("client", "device")

    if client_type == "control":
        system_id = websocket.query_params.get("system_id", "")
        await motor_manager.add_controller(websocket)
        try:
            await websocket.send_json({"type": "control_ack", "system_id": system_id})
            while True:
                command = await websocket.receive_text()
                # All movement/control operations remain WebSocket commands.
                sent = (
                    await motor_manager.send_for_system(system_id, command)
                    if system_id
                    else await motor_manager.send_any(command)
                )
                await websocket.send_json({
                    "type": "command_ack",
                    "command": command,
                    "delivered": sent,
                })
        except WebSocketDisconnect:
            pass
        finally:
            await motor_manager.remove_controller(websocket)
        return

    device_id: Optional[str] = None
    system_id: Optional[str] = None
    try:
        hello = await websocket.receive_json()
        if hello.get("type") != "hello" or hello.get("role") != "motor":
            await websocket.close(code=1002)
            return

        device_id = str(hello.get("device_id", ""))
        system_id = str(hello.get("system_id", ""))
        firmware = str(hello.get("firmware_version", "unknown"))
        if not device_id or not system_id:
            await websocket.close(code=1008)
            return

        await motor_manager.register_device(MotorDeviceConnection(
            websocket=websocket,
            device_id=device_id,
            system_id=system_id,
            firmware_version=firmware,
        ))
        await websocket.send_json({
            "type": "hello_ack",
            "role": "motor",
            "device_id": device_id,
            "system_id": system_id,
        })
        await motor_manager.broadcast_status({
            "type": "motor_online",
            "device_id": device_id,
            "system_id": system_id,
        })

        while True:
            message = await websocket.receive_text()
            # ESP #3 returns JSON status/ack messages; forward them to UI clients.
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                payload = {"type": "motor_message", "message": message}
            payload.setdefault("system_id", system_id)
            payload.setdefault("device_id", device_id)
            await motor_manager.broadcast_status(payload)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print("Motor WebSocket error:", repr(exc))
    finally:
        if system_id:
            await motor_manager.remove_device(system_id, websocket)
            await motor_manager.broadcast_status({
                "type": "motor_offline",
                "device_id": device_id,
                "system_id": system_id,
            })


@router.get("/motor/control", response_class=HTMLResponse)
async def motor_control_page(system_id: str = ""):
    # This is the original motor browser concept moved to the central server.
    # Every control action below is sent through WS /motor, not HTTP.
    return HTMLResponse(f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Smart Rack Motor</title>
<style>
body{{font-family:Arial;text-align:center;background:#121212;color:white;user-select:none}}
#status{{color:#ffca28;margin:12px}} .grid{{display:grid;grid-template-columns:90px 90px 90px;gap:12px;justify-content:center}}
.btn{{background:#008CBA;color:#fff;border:0;border-radius:12px;padding:20px 0;font-size:18px;font-weight:bold}}
.estop{{background:red;color:white;border:3px solid white;border-radius:12px;padding:15px;width:80%;font-size:20px;font-weight:bold;margin:10px}}
.path button{{margin:5px;padding:12px;border:0;border-radius:8px;font-weight:bold}}
</style></head><body>
<h2>Motor Controller</h2><div id="status">Connecting...</div>
<button class="estop" onclick="send('ESTOP')">EMERGENCY STOP</button>
<div class="grid"><span></span><button class="btn" data-k="1">1<br>FRONT</button><span></span>
<button class="btn" data-k="2">2<br>LEFT</button><span></span><button class="btn" data-k="4">4<br>RIGHT</button>
<span></span><button class="btn" data-k="3">3<br>BACK</button><span></span></div>
<div class="path"><button onclick="send('START_REC')">START REC</button><button onclick="send('STOP_REC')">STOP REC</button>
<button onclick="send('RUN_PATH')">RUN PATH</button><button onclick="send('REVERSE_PATH')">REVERSE PATH</button></div>
<script>
const sys={system_id!r}; const proto=location.protocol==='https:'?'wss':'ws';
let ws=new WebSocket(`${{proto}}://${{location.host}}/motor?client=control&system_id=${{encodeURIComponent(sys)}}`);
let timer=null,key=null; const status=document.getElementById('status');
ws.onopen=()=>status.textContent='Connected'; ws.onclose=()=>status.textContent='Disconnected'; ws.onmessage=e=>status.textContent=e.data;
function send(x){{if(ws.readyState===1)ws.send(x)}}
function press(k){{release();key=k;send('P:'+k);timer=setInterval(()=>send('P:'+k),50)}}
function release(){{if(timer)clearInterval(timer);timer=null;if(key)send('R:'+key);key=null}}
document.querySelectorAll('[data-k]').forEach(b=>{{let k=b.dataset.k;b.onmousedown=()=>press(k);b.onmouseup=release;b.ontouchstart=e=>{{e.preventDefault();press(k)}};b.ontouchend=e=>{{e.preventDefault();release()}}}});
window.onmouseup=release;
</script></body></html>""")
