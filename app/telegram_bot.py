from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request
from sqlmodel import Session, select
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import settings
from .database import engine
from .device_manager import device_manager
from .models import Device, DeviceStatus, SensorData, User
from .services import (
    csv_for_device,
    get_or_create_user,
    get_settings,
    get_user_devices,
    pair_device,
    pairing_secrets,
)

CONNECT_DEVICE_ID, CONNECT_SECRET = range(2)
DISCONNECT_SELECT = 10
SETTINGS_SELECT, SETTINGS_VALUE = range(11, 13)

BTN_CONNECT = "Connect"
BTN_DISCONNECT = "Disconnect"
BTN_RETRIEVE = "Retrieve Data"
BTN_SETTINGS = "Settings"
BTN_DRIVE = "Link Google Drive"
BTN_CANCEL = "Cancel"


def main_keyboard(registered: bool = True) -> ReplyKeyboardMarkup:
    if registered:
        buttons = [
            [BTN_CONNECT, BTN_DISCONNECT],
            [BTN_RETRIEVE, BTN_SETTINGS],
            [BTN_DRIVE],
        ]
    else:
        buttons = [
            [BTN_CONNECT],
            [BTN_SETTINGS, BTN_DRIVE],
        ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


def get_user_from_update(session: Session, update: Update):
    tg = update.effective_user
    return get_or_create_user(
        session,
        str(tg.id),
        tg.username or tg.full_name or "",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with Session(engine) as session:
        existing = session.exec(
            select(User).where(
                User.telegram_id == str(update.effective_user.id)
            )
        ).first()
        get_user_from_update(session, update)

    await update.message.reply_text(
        "Welcome to Smart Drying Rack."
        if not existing
        else "Welcome back to Smart Drying Rack.",
        reply_markup=main_keyboard(existing is not None),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Operation cancelled.",
        reply_markup=main_keyboard(True),
    )
    return ConversationHandler.END


async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Enter the ESP32 device ID.",
        reply_markup=cancel_keyboard(),
    )
    return CONNECT_DEVICE_ID


async def connect_device_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    device_id = update.message.text.strip()
    context.user_data["device_id"] = device_id

    with Session(engine) as session:
        device = session.exec(
            select(Device).where(Device.device_id == device_id)
        ).first()

    if not device:
        await update.message.reply_text(
            "Device is not known by the server yet. "
            "Make sure the ESP32 is connected to Wi-Fi and the WebSocket server."
        )
        return CONNECT_DEVICE_ID

    await update.message.reply_text(
        "Enter the one-time secret displayed by the ESP32."
    )
    return CONNECT_SECRET


async def connect_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    device_id = context.user_data["device_id"]
    secret = update.message.text.strip().upper()

    with Session(engine) as session:
        user = get_user_from_update(session, update)
        device = session.exec(
            select(Device).where(Device.device_id == device_id)
        ).first()

        if not device:
            await update.message.reply_text("Device was not found.")
            return ConversationHandler.END

        if not pairing_secrets.consume(device_id, secret):
            await update.message.reply_text(
                "Invalid or expired secret. Generate a new secret on the ESP32."
            )
            return CONNECT_SECRET

        pair_device(session, user, device)

    conn = await device_manager.get(device_id)
    if conn:
        conn.paired = True
        await device_manager.send(
            device_id,
            {"type": "pair_result", "success": True},
        )
        text = f"Connection established with {device_id}."
    else:
        text = (
            "Pairing accepted, but the ESP32 WebSocket is not currently connected. "
            "It will connect automatically when it is online."
        )

    context.user_data.clear()
    await update.message.reply_text(text, reply_markup=main_keyboard(True))
    return ConversationHandler.END


async def disconnect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with Session(engine) as session:
        user = get_user_from_update(session, update)
        devices = get_user_devices(session, user)

    if not devices:
        await update.message.reply_text(
            "You have no connected devices.",
            reply_markup=main_keyboard(True),
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(d.device_id, callback_data=f"disconnect:{d.id}")]
        for d in devices
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="disconnect:cancel")])

    await update.message.reply_text(
        "Select a device to disconnect:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DISCONNECT_SELECT


async def disconnect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    value = query.data.split(":", 1)[1]

    if value == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    with Session(engine) as session:
        user = get_user_from_update(session, update)
        device = session.get(Device, int(value))

        if not device or device.owner_id != user.id:
            await query.edit_message_text("Invalid device.")
            return ConversationHandler.END

        device.owner_id = None
        device.status = DeviceStatus.WAITING_PAIR
        session.add(device)
        session.commit()
        device_id = device.device_id

    conn = await device_manager.get(device_id)
    if conn:
        conn.paired = False
        await device_manager.send(device_id, {"type": "unpaired"})

    await query.edit_message_text(f"{device_id} disconnected.")
    return ConversationHandler.END


async def retrieve_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with Session(engine) as session:
        user = get_user_from_update(session, update)
        devices = get_user_devices(session, user)

        if not devices:
            await update.message.reply_text("You have no connected devices.")
            return

        if len(devices) == 1:
            device = devices[0]
            fileobj = csv_for_device(session, device)
            await update.message.reply_document(
                document=fileobj,
                filename=f"{device.device_id}_weather.csv",
                caption=f"Weather data for {device.device_id}",
            )
            return

        keyboard = [
            [InlineKeyboardButton(
                d.device_id,
                callback_data=f"csv:{d.id}"
            )]
            for d in devices
        ]

    await update.message.reply_text(
        "Select a device:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def csv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    with Session(engine) as session:
        user = get_user_from_update(session, update)
        device = session.get(Device, int(query.data.split(":", 1)[1]))

        if not device or device.owner_id != user.id:
            await query.edit_message_text("Invalid device.")
            return

        fileobj = csv_for_device(session, device)
        device_id = device.device_id

    await query.message.reply_document(
        document=fileobj,
        filename=f"{device_id}_weather.csv",
        caption=f"Weather data for {device_id}",
    )


async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup(
        [
            ["Update Frequency"],
            ["Rain Action Waiting Time"],
            ["Photo Update"],
            [BTN_CANCEL],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "Select a setting.",
        reply_markup=keyboard,
    )
    return SETTINGS_SELECT


async def settings_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()

    if value == BTN_CANCEL:
        return await cancel(update, context)

    mapping = {
        "Update Frequency": "update_frequency",
        "Rain Action Waiting Time": "action_waiting_time",
        "Photo Update": "get_photo_update",
    }

    if value not in mapping:
        await update.message.reply_text("Choose one of the displayed options.")
        return SETTINGS_SELECT

    context.user_data["setting_name"] = mapping[value]
    await update.message.reply_text(
        "Enter the new value.\n"
        "Update Frequency = minutes\n"
        "Rain Action Waiting Time = seconds\n"
        "Photo Update = true/false"
    )
    return SETTINGS_VALUE


async def settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setting = context.user_data["setting_name"]
    value = update.message.text.strip()

    with Session(engine) as session:
        user = get_user_from_update(session, update)
        current = get_settings(session, user)

        try:
            if setting in ("update_frequency", "action_waiting_time"):
                parsed = int(value)
                if parsed <= 0:
                    raise ValueError
                setattr(current, setting, parsed)
            else:
                setattr(
                    current,
                    setting,
                    value.lower() in ("true", "1", "yes", "on"),
                )

            session.add(current)
            session.commit()

        except ValueError:
            await update.message.reply_text("Invalid value. Try again.")
            return SETTINGS_VALUE

    context.user_data.clear()
    await update.message.reply_text(
        "Setting updated.",
        reply_markup=main_keyboard(True),
    )
    return ConversationHandler.END


async def link_drive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Google Drive linking is reserved for the next integration stage."
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please use the buttons provided.",
        reply_markup=main_keyboard(True),
    )


async def send_rain_alert(bot: Bot, device: Device, sensor: SensorData):
    if not device.owner_id:
        return

    with Session(engine) as session:
        owner = session.get(User, device.owner_id)
        if not owner:
            return

        user_settings = get_settings(session, owner)
        wait_seconds = user_settings.action_waiting_time

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Move to shelter",
                callback_data=f"rain:move:{device.id}",
            ),
            InlineKeyboardButton(
                "Ignore",
                callback_data=f"rain:ignore:{device.id}",
            ),
        ]
    ])

    await bot.send_message(
        chat_id=int(owner.telegram_id),
        text=(
            f"🌧 Rain detected on {device.device_id}.\n"
            f"Temperature: {sensor.temperature if sensor.temperature is not None else 'N/A'} °C\n"
            f"Humidity: {sensor.humidity if sensor.humidity is not None else 'N/A'} %\n\n"
            f"Choose an action. Automatic shelter movement will occur after "
            f"{wait_seconds} seconds if no action is taken."
        ),
        reply_markup=keyboard,
    )

    # The actual delayed action is scheduled here.
    # It is deliberately kept in the same application as FastAPI and Telegram.
    async def delayed_move(context: ContextTypes.DEFAULT_TYPE):
        with Session(engine) as session:
            current = session.get(Device, device.id)
            if not current or not current.owner_id:
                return

        await device_manager.send(
            device.device_id,
            {"type": "motor", "action": "move_to_shelter"},
        )

        await bot.send_message(
            chat_id=int(owner.telegram_id),
            text=f"No rain action was selected. Automatic shelter command sent to {device.device_id}.",
        )

    if wait_seconds > 0:
        # JobQueue uses seconds and is part of python-telegram-bot[job-queue].
        bot_app = telegram_application
        if bot_app:
            bot_app.job_queue.run_once(
                delayed_move,
                wait_seconds,
                name=f"rain:{device.device_id}:{sensor.id}",
            )


async def rain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, action, device_db_id = query.data.split(":")
    device_db_id = int(device_db_id)

    with Session(engine) as session:
        user = get_user_from_update(session, update)
        device = session.get(Device, device_db_id)

        if not device or device.owner_id != user.id:
            await query.edit_message_text("Invalid device.")
            return

        device_id = device.device_id

    if action == "move":
        await device_manager.send(
            device_id,
            {"type": "motor", "action": "move_to_shelter"},
        )
        await query.edit_message_text(f"Shelter command sent to {device_id}.")
    else:
        await query.edit_message_text("Rain action ignored.")


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()

    connect_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_CONNECT}$"), connect_start)
        ],
        states={
            CONNECT_DEVICE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_device_id)
            ],
            CONNECT_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_secret)
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel),
            CommandHandler("cancel", cancel),
        ],
    )

    disconnect_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_DISCONNECT}$"), disconnect_start)
        ],
        states={
            DISCONNECT_SELECT: [
                CallbackQueryHandler(
                    disconnect_callback,
                    pattern=r"^disconnect:",
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    settings_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_SETTINGS}$"), settings_start)
        ],
        states={
            SETTINGS_SELECT: [MessageHandler(filters.TEXT, settings_select)],
            SETTINGS_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_value)
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel),
            CommandHandler("cancel", cancel),
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(connect_conv)
    application.add_handler(disconnect_conv)
    application.add_handler(settings_conv)
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_RETRIEVE}$"), retrieve_data)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_DRIVE}$"), link_drive)
    )
    application.add_handler(
        CallbackQueryHandler(csv_callback, pattern=r"^csv:")
    )
    application.add_handler(
        CallbackQueryHandler(rain_callback, pattern=r"^rain:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT, unknown_message)
    )

    return application


telegram_application: Optional[Application] = None


async def startup_bot():
    global telegram_application

    if not settings.telegram_bot_token:
        print("Telegram bot disabled: TELEGRAM_BOT_TOKEN is empty.")
        return

    telegram_application = build_application()
    await telegram_application.initialize()
    await telegram_application.start()

    if settings.telegram_mode == "webhook":
        if not settings.public_base_url or not settings.telegram_webhook_secret:
            raise RuntimeError(
                "PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET are required for webhook mode."
            )

        url = (
            settings.public_base_url.rstrip("/")
            + "/telegram/webhook/"
            + settings.telegram_webhook_secret
        )
        await telegram_application.bot.set_webhook(url=url)
        print("Telegram webhook configured:", url)
    else:
        await telegram_application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES
        )
        print("Telegram polling started.")


async def shutdown_bot():
    global telegram_application

    if not telegram_application:
        return

    if telegram_application.updater and telegram_application.updater.running:
        await telegram_application.updater.stop()

    await telegram_application.stop()
    await telegram_application.shutdown()
    telegram_application = None


async def telegram_webhook(request: Request, secret: str):
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if not telegram_application:
        raise HTTPException(status_code=503, detail="Telegram bot not initialized")

    data = await request.json()
    update = Update.de_json(data, telegram_application.bot)
    await telegram_application.process_update(update)
    return {"ok": True}
