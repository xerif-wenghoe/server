from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .camera.router import router as camera_router
from .config import settings
from .database import init_db
from .motor import router as motor_router
from .sensor import router as sensor_router
from .telegram_bot import shutdown_bot, startup_bot, telegram_webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await startup_bot()
    yield
    await shutdown_bot()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(sensor_router)
app.include_router(camera_router)
app.include_router(motor_router)


@app.get("/")
async def root():
    return {
        "application": settings.app_name,
        "status": "ok",
        "service": "Unified FastAPI + Telegram + PostgreSQL",
        "endpoints": {
            "sensor": "WS /sensor",
            "camera": "POST /camera",
            "motor": "WS /motor",
        },
    }


@app.get("/health")
async def health(): return {"status": "healthy"}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook_endpoint(secret: str, request: Request):
    return await telegram_webhook(request, secret)
