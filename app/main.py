from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .config import settings
from .database import init_db
from .telegram_bot import shutdown_bot, startup_bot, telegram_webhook
from .websocket import router as websocket_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await startup_bot()
    yield
    await shutdown_bot()


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

app.include_router(websocket_router)


@app.get("/")
async def root():
    return {
        "application": settings.app_name,
        "status": "ok",
        "service": "FastAPI + Telegram Bot",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook_endpoint(secret: str, request: Request):
    return await telegram_webhook(request, secret)
