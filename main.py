import os
import sqlite3
import secrets
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB = os.getenv("DB_PATH", "sixseven.sqlite3")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
OWNER_IDS = {
    x.strip()
    for x in os.getenv("OWNER_DEVICE_IDS", "").split(",")
    if x.strip() and not x.startswith("REPLACE_")
}

app = FastAPI(title="SIX SEVEN License API", version="1.2")
bot_app = None


def db():
    connection = sqlite3.connect(DB, timeout=10)
    connection.row_factory = sqlite3.Row
    # Включаем WAL режим для надежности SQLite на сервере
    connection.execute("PRAGMA journal_mode=WAL;")
    return connection


def init_db():
    with db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                expires TEXT,
                max_devices INTEGER NOT NULL DEFAULT 3,
                revoked INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS activations (
                key_hash TEXT NOT NULL,
                device_id TEXT NOT NULL,
                created TEXT NOT NULL,
                UNIQUE(key_hash, device_id)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS trial_claims (
                device_id TEXT PRIMARY KEY,
                started TEXT NOT NULL,
                expires TEXT NOT NULL
            )"""
        )
    logger.info("Database initialized successfully.")


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)


def make_key():
    return "S7-" + "-".join(
        secrets.token_hex(3).upper() for _ in range(4)
    )


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_ID and user.id == ADMIN_ID)


async def deny_non_admin(update: Update) -> bool:
    if is_admin(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Нет доступа.")
    return True


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Статистика", callback_data="panel:stats"),
                InlineKeyboardButton("📨 Заявки", callback_data="panel:requests"),
            ],
            [
                InlineKeyboardButton("🔑 Ключи", callback_data="panel:licenses"),
            ],
        ]
    )


class RequestIn(BaseModel):
    device_id: str = Field(min_length=2, max_length=128)
    note: str = Field(
        default="Запрос доступа из приложения",
        max_length=300,
    )


class ActivateIn(BaseModel):
    key: str = Field(min_length=4, max_length=100)
    device_id: str = Field(min_length=2, max_length=128)


class TrialIn(BaseModel):
    device_id: str = Field(min_length=2, max_length=128)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"Incoming request: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"Response status: {response.status_code} for {request.method} {request.url.path}")
    return response


@app.on_event("startup")
async def startup():
    global bot_app
    init_db()

    if BOT_TOKEN:
        try:
            bot_app = Application.builder().token(BOT_TOKEN).build()

            bot_app.add_handler(CommandHandler("start", start_command))
            bot_app.add_handler(CommandHandler("whoami", whoami))
            bot_app.add_handler(CommandHandler("admin", admin_command))
            bot_app.add_handler(CommandHandler("stats", stats_command))
            bot_app.add_handler(CommandHandler("users", users_command))
            bot_app.add_handler(CommandHandler("requests", requests_command))
            bot_app.add_handler(
                CallbackQueryHandler(approve_callback, pattern=r"^approve:")
            )
            bot_app.add_handler(
                CallbackQueryHandler(panel_callback, pattern=r"^panel:")
            )

            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling()
            logger.info("Telegram bot started successfully.")
        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")


@app.on_event("shutdown")
async def shutdown():
    global bot_app
    if bot_app:
        try:
            if bot_app.updater and bot_app.updater.running:
                await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
            logger.info("Telegram bot stopped successfully.")
        except Exception as e:
            logger.error(f"Error during bot shutdown: {e}")


@app.get("/health")
def health():
    return {"ok": True, "service": "sixseven-license", "version": "1.2"}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! Это бот SIX SEVEN VPN.
"
        "Команда /whoami покажет твой Telegram ID."
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(
            f"Ваш Telegram ID: {user.id}"
        )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_non_admin(update):
        return

    await update.effective_message.reply_text(
        "🛠 Админ-панель SIX SEVEN

"
        "Выбери раздел или используй команды:
"
        "/stats — статистика
"
        "/requests — последние заявки
"
        "/users — выданные лицензии
"
        "/whoami — твой Telegram ID",
        reply_markup=admin_keyboard(),
    )


async def stats_text():
    with db() as connection:
        requests_total = connection.execute(
            "SELECT COUNT(*) AS n FROM requests"
        ).fetchone()["n"]
        pending = connection.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE status='pending'"
        ).fetchone()["n"]
        licenses_total = connection.execute(
            "SELECT COUNT(*) AS n FROM licenses"
        ).fetchone()["n"]
        active_licenses = connection.execute(
            "SELECT COUNT(*) AS n FROM licenses WHERE revoked=0"
        ).fetchone()["n"]
        activations = connection.execute(
            "SELECT COUNT(*) AS n FROM activations"
        ).fetchone()["n"]

    return (
        "📊 Статистика SIX SEVEN

"
        f"Всего заявок: {requests_total}
"
        f"Ожидают решения: {pending}
"
        f"Всего лицензий: {licenses_total}
"
        f"Не отозвано лицензий: {active_licenses}
"
        f"Активаций устройств: {activations}"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_non_admin(update):
        return
    await update.effective_message.reply_text(await stats_text())


async def requests_text():
    with db() as connection:
        rows = connection.execute(
            """SELECT id, device_id, note, status, created
               FROM requests ORDER BY id DESC LIMIT 10"""
        ).fetchall()

    if not rows:
        return "📨 Заявок пока нет."

    lines = ["📨 Последние заявки:"]
    for row in rows:
        lines.append(
            f"\n#{row['id']} — {row['status']}\n"
            f"Устройство: {row['device_id']}\n"
            f"Комментарий: {row['note'] or '—'}\n"
            f"Дата: {row['created']}"
        )
    return "\n".join(lines)


async def requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_non_admin(update):
        return
    await update.effective_message.reply_text(await requests_text())


async def users_text():
    with db() as connection:
        rows = connection.execute(
            """SELECT l.id, l.expires, l.max_devices, l.revoked, l.created,
                      COUNT(a.device_id) AS devices
               FROM licenses l
               LEFT JOIN activations a ON a.key_hash=l.key_hash
               GROUP BY l.id
               ORDER BY l.id DESC LIMIT 10"""
        ).fetchall()

    if not rows:
        return "🔑 Выданных лицензий пока нет."

    lines = [
        "🔑 Последние лицензии:"
    ]
    for row in rows:
        status = "отозвана" if row["revoked"] else "активна"
        lines.append(
            f"\nЛицензия #{row['id']} — {status}\n"
            f"Устройств: {row['devices']}/{row['max_devices']}\n"
            f"Истекает: {row['expires'] or 'без срока'}\n"
            f"Создана: {row['created']}"
        )
    return "\n".join(lines)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_non_admin(update):
        return
    await update.effective_message.reply_text(await users_text())


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if query.from_user.id != ADMIN_ID or not ADMIN_ID:
        await query.answer("Нет доступа", show_alert=True)
        return

    await query.answer()
    section = query.data.split(":", 1)[1]

    if section == "stats":
        message = await stats_text()
    elif section == "requests":
        message = await requests_text()
    elif section == "licenses":
        message = await users_text()
    else:
        message = "Неизвестный раздел."

    await query.message.reply_text(message)


@app.post("/request-access")
async def request_access(data: RequestIn):
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO requests(device_id,note,created) VALUES(?,?,?)",
            (data.device_id, data.note, utcnow().isoformat()),
        )
        request_id = cursor.lastrowid

    if bot_app and ADMIN_ID:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Одобрить на 30 дн (150₽)",
                        callback_data=f"approve:{request_id}:30",
                    ),
                    InlineKeyboardButton(
                        "90 дн (350₽)",
                        callback_data=f"approve:{request_id}:90",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "180 дн (650₽)",
                        callback_data=f"approve:{request_id}:180",
                    ),
                    InlineKeyboardButton(
                        "365 дн (1200₽)",
                        callback_data=f"approve:{request_id}:365",
                    )
                ],
            ]
        )
        try:
            await bot_app.bot.send_message(
                ADMIN_ID,
                f"🔥 Новая заявка SIX SEVEN #{request_id}\n"
                f"Устройство: {data.device_id}\n"
                f"Комментарий: {data.note}",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")

    return {
        "ok": True,
        "request_id": request_id,
        "message": "Заявка отправлена администратору в Telegram",
    }


async def approve_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query or not query.data:
        return

    if query.from_user.id != ADMIN_ID or not ADMIN_ID:
        await query.answer("Нет доступа", show_alert=True)
        return

    _, request_id, days = query.data.split(":")
    with db() as connection:
        request = connection.execute(
            "SELECT * FROM requests WHERE id=?",
            (int(request_id),),
        ).fetchone()

        if not request or request["status"] != "pending":
            await query.answer("Заявка уже обработана")
            return

        key = make_key()
        expires = (
            utcnow() + timedelta(days=int(days))
        ).isoformat()

        connection.execute(
            """INSERT INTO licenses
               (key_hash,expires,max_devices,created)
               VALUES(?,?,3,?)""",
            (hash_value(key), expires, utcnow().isoformat()),
        )
        connection.execute(
            "UPDATE requests SET status='approved' WHERE id=?",
            (int(request_id),),
        )

    await query.edit_message_text(
        f"✅ Заявка #{request_id} одобрена.\n"
        f"Ключ на {days} дней:\n`{key}`\n"
        "Лимит устройств: 3",
        parse_mode="Markdown"
    )
    await query.answer("Ключ создан")


# Дублируем endpoint с дефисом и подчеркиванием для полной совместимости с любыми клиентами
@app.post("/start-trial")
@app.post("/start_trial")
def start_trial(data: TrialIn):
    """Grant one 7-day trial per device ID."""
    if data.device_id in OWNER_IDS:
        return {
            "ok": True,
            "plan": "developer",
            "expires": None,
            "message": "Доступ разработчика активен навсегда",
        }

    with db() as connection:
        existing = connection.execute(
            "SELECT started, expires FROM trial_claims WHERE device_id=?",
            (data.device_id,),
        ).fetchone()

        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Бесплатный пробный период уже использован на этом устройстве",
                    "expires": existing["expires"],
                },
            )

        started = utcnow()
        expires = started + timedelta(days=7)
        connection.execute(
            "INSERT INTO trial_claims(device_id, started, expires) VALUES(?,?,?)",
            (data.device_id, started.isoformat(), expires.isoformat()),
        )

    return {
        "ok": True,
        "plan": "trial",
        "started": started.isoformat(),
        "expires": expires.isoformat(),
        "days": 7,
        "message": "Бесплатный доступ активирован на 7 дней",
    }


@app.post("/activate")
def activate(data: ActivateIn):
    if data.device_id in OWNER_IDS:
        return {
            "ok": True,
            "plan": "developer",
            "expires": None,
            "max_devices": 999,
            "message": "Доступ разработчика активен навсегда",
        }

    key_clean = data.key.strip().upper()
    key_hash = hash_value(key_clean)
    
    with db() as connection:
        license_row = connection.execute(
            "SELECT * FROM licenses WHERE key_hash=?",
            (key_hash,),
        ).fetchone()

        if not license_row or license_row["revoked"]:
            raise HTTPException(status_code=403, detail="Ключ недействителен или отозван")

        if datetime.fromisoformat(license_row["expires"]) <= utcnow():
            raise HTTPException(status_code=403, detail="Срок действия ключа истёк")

        existing = connection.execute(
            """SELECT 1 FROM activations
               WHERE key_hash=? AND device_id=?""",
            (key_hash, data.device_id),
        ).fetchone()

        count = connection.execute(
            "SELECT COUNT(*) AS n FROM activations WHERE key_hash=?",
            (key_hash,),
        ).fetchone()["n"]

        if not existing and count >= license_row["max_devices"]:
            raise HTTPException(status_code=403, detail=f"Достигнут лимит устройств: {license_row['max_devices']}")

        connection.execute(
            """INSERT OR IGNORE INTO activations
               (key_hash,device_id,created) VALUES(?,?,?)""",
            (key_hash, data.device_id, utcnow().isoformat()),
        )

    return {
        "ok": True,
        "plan": "user",
        "expires": license_row["expires"],
        "max_devices": license_row["max_devices"],
        "message": "Ключ успешно активирован",
    }


@app.get("/validate")
@app.post("/validate")
async def validate(request: Request, device_id: str = None):
    # Поддерживаем как GET параметры, так и POST JSON (на случай разного поведения Android клиента)
    if not device_id:
        try:
            body = await request.json()
            device_id = body.get("device_id")
        except Exception:
            pass

    if not device_id:
        raise HTTPException(status_code=422, detail="device_id is required")

    if device_id in OWNER_IDS:
        return {"ok": True, "plan": "developer", "expires": None}

    now = utcnow()
    with db() as connection:
        row = connection.execute(
            """SELECT l.expires, l.revoked
               FROM activations a
               JOIN licenses l ON l.key_hash=a.key_hash
               WHERE a.device_id=?
               ORDER BY a.rowid DESC LIMIT 1""",
            (device_id,),
        ).fetchone()

        if row and not row["revoked"]:
            expiry = datetime.fromisoformat(row["expires"])
            if expiry > now:
                return {
                    "ok": True,
                    "plan": "user",
                    "expires": row["expires"],
                }

        trial = connection.execute(
            "SELECT started, expires FROM trial_claims WHERE device_id=?",
            (device_id,),
        ).fetchone()

    if trial:
        trial_expiry = datetime.fromisoformat(trial["expires"])
        if trial_expiry > now:
            return {
                "ok": True,
                "plan": "trial",
                "started": trial["started"],
                "expires": trial["expires"],
                "message": "Пробный доступ активен",
            }

    return {"ok": False, "message": "Нет активной подписки или пробный период закончился"}
