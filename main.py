import logging
import warnings
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, ApplicationHandlerStop, MessageHandler, TypeHandler,
    filters, ContextTypes,
)
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.warnings import PTBUserWarning

# Filter PTBUserWarning for mixed text and callback queries where per_message=False is intended
warnings.filterwarnings(
    "ignore",
    message=r".*CallbackQueryHandler.*will not be tracked.*",
    category=PTBUserWarning,
)

from config import BOT_TOKEN, ADMIN_IDS
from database import (
    async_record_business_admin_activity,
    async_record_business_welcome_sent,
    async_can_send_business_welcome,
)
from user import setup_user_conversation
from user.base_handlers import fallback_unknown
from manager import setup_manager_handler
from driver.handlers import setup_driver_handler

BUSINESS_SILENCE_SECONDS = 5 * 3600  # 5 hours (18,000 seconds)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BUSINESS_ALLOWED_UPDATES = (
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)


async def log_business_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Business events without consuming them before the bot flow runs."""
    if update.business_connection:
        connection = update.business_connection
        if connection.user:
            context.bot_data.setdefault("business_owner_ids", set()).add(connection.user.id)
        logger.info(
            "Business connection %s is %s (can_reply=%s, can_read_messages=%s)",
            connection.id,
            "enabled" if connection.is_enabled else "disabled",
            connection.rights.can_reply if connection.rights else None,
            connection.rights.can_read_messages if connection.rights else None,
        )
    elif update.business_message:
        message = update.business_message
        logger.info(
            "Business message received: connection=%s chat=%s user=%s",
            message.business_connection_id,
            message.chat_id,
            message.from_user.id if message.from_user else None,
        )


async def business_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the Business chat as an entry point to the regular bot chat with human takeover & cooldown."""
    message = update.business_message
    if not message:
        return

    from_user_id = message.from_user.id if message.from_user else None
    business_owners = context.bot_data.get("business_owner_ids", set())

    # 1. Check if the message is sent by an admin or the business account owner
    is_admin_or_owner = (
        (from_user_id in ADMIN_IDS) or
        (from_user_id in business_owners)
    )

    if is_admin_or_owner:
        # Admin is active in this chat -> silence the bot for 5 hours (Human Takeover)
        await async_record_business_admin_activity(message.chat_id)
        logger.info(
            "Admin/Owner %s active in business chat %s. Bot silenced for 5 hours.",
            from_user_id, message.chat_id
        )
        raise ApplicationHandlerStop

    # 2. Check if the chat is on cooldown or silenced by recent admin activity (5 hours)
    can_send = await async_can_send_business_welcome(
        message.chat_id,
        silence_seconds=BUSINESS_SILENCE_SECONDS
    )
    if not can_send:
        logger.info(
            "Business welcome skipped for chat %s (admin active or welcome sent within 5 hours).",
            message.chat_id
        )
        raise ApplicationHandlerStop

    bot_profile = await context.bot.get_me()
    if not bot_profile.username:
        logger.error("The bot has no username, so a booking link cannot be created.")
        raise ApplicationHandlerStop

    booking_url = f"https://t.me/{bot_profile.username}?start=business_order"
    welcome_text = (
        "Բարև ձեզ 👋\n"
        "Մենք կատարում ենք Ջերմուկ–Երևան, Երևան–Ջերմուկ "
        "ուղևորափոխադրումներ և զբոսաշրջային էքսկուրսիաներ։\n\n"
        "Здравствуйте 👋\n"
        "Мы выполняем перевозки Джермук–Ереван, Ереван–Джермук "
        "и туристические экскурсии.\n\n"
        "Hello 👋\n"
        "We provide Jermuk–Yerevan, Yerevan–Jermuk transportation "
        "and tourist excursions.\n\n"
        "Պատվեր կատարելու կամ գրանցվելու համար սեղմեք ստորև։\n"
        "Чтобы оформить заказ, нажмите кнопку ниже.\n"
        "To make a booking, tap the button below."
    )
    await message.reply_text(
        welcome_text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Պատվիրել / Заказать / Book", url=booking_url)
        ]]),
    )
    await async_record_business_welcome_sent(message.chat_id)
    logger.info("Sent Business welcome to chat %s (cooldown started: 5 hours)", message.chat_id)
    raise ApplicationHandlerStop


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Async error handler for PTB v20+ runtime exceptions."""
    if isinstance(context.error, Conflict):
        logger.error("API Conflict: Another bot instance is running with this token.")
    elif isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Transient network disruption handled: {context.error}. Bot remains active.")
    else:
        logger.error(f"Unhandled exception: {context.error}", exc_info=context.error)


def main():
    request = HTTPXRequest(
        connection_pool_size=100,
        connect_timeout=15.0,
        read_timeout=15.0,
        write_timeout=15.0,
        pool_timeout=10.0,
    )
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

    # Diagnostic logging and business welcome interceptors
    app.add_handler(TypeHandler(Update, log_business_updates), group=-2)
    app.add_handler(
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, business_welcome),
        group=-1,
    )

    # 1. Manager/Admin Handler (modular package entry point)
    app.add_handler(setup_manager_handler())

    # 2. Driver Conversation Handler
    app.add_handler(setup_driver_handler())

    # 3. Customer Conversation Handler (imported via user package)
    app.add_handler(setup_user_conversation())

    # 4. Global Out-of-Conversation Fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_unknown))

    # Global Error Handler
    app.add_error_handler(error_handler)

    logger.info("🚀 Bot started listening via Polling (including Business updates)...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=(
            "message",
            "edited_message",
            "callback_query",
            *BUSINESS_ALLOWED_UPDATES,
        ),
    )


if __name__ == '__main__':
    main()