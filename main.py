import os, sqlite3, secrets, hashlib, asyncio
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

DB = os.getenv('DB_PATH', 'sixseven.sqlite3')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_TELEGRAM_ID', '0'))
OWNER_IDS = {x.strip() for x in os.getenv('OWNER_DEVICE_IDS', '').split(',') if x.strip() and not x.startswith('REPLACE_')}
app = FastAPI(title='SIX SEVEN License API', version='1.0')
bot_app = None

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT \'pending\', created TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS licenses(id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT UNIQUE NOT NULL, expires TEXT, max_devices INTEGER NOT NULL DEFAULT 3, revoked INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS activations(key_hash TEXT NOT NULL, device_id TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(key_hash,device_id))')

def h(s): return hashlib.sha256(s.encode()).hexdigest()
def utcnow(): return datetime.now(timezone.utc)
def make_key(): return 'S7-' + '-'.join(secrets.token_hex(3).upper() for _ in range(4))

class RequestIn(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)
    note: str = Field(default='Запрос доступа из приложения', max_length=300)
class ActivateIn(BaseModel):
    key: str = Field(min_length=8, max_length=100)
    device_id: str = Field(min_length=8, max_length=128)

@app.on_event('startup')
async def startup():
    global bot_app
    init_db()
    if BOT_TOKEN:
        bot_app = Application.builder().token(BOT_TOKEN).build()
        bot_app.add_handler(CallbackQueryHandler(approve_callback, pattern=r'^approve:'))
        bot_app.add_handler(CommandHandler('whoami', whoami))
        await bot_app.initialize(); await bot_app.start(); await bot_app.updater.start_polling()

@app.on_event('shutdown')
async def shutdown():
    if bot_app:
        await bot_app.updater.stop(); await bot_app.stop(); await bot_app.shutdown()

@app.get('/health')
def health(): return {'ok': True, 'service': 'sixseven-license'}

@app.post('/request-access')
async def request_access(data: RequestIn):
    with db() as c:
        cur = c.execute('INSERT INTO requests(device_id,note,created) VALUES(?,?,?)', (data.device_id,data.note,utcnow().isoformat()))
        rid = cur.lastrowid
    if bot_app and ADMIN_ID:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Одобрить и выдать ключ на 30 дней', callback_data=f'approve:{rid}:30'), InlineKeyboardButton('90 дней', callback_data=f'approve:{rid}:90')], [InlineKeyboardButton('365 дней', callback_data=f'approve:{rid}:365')]])
        await bot_app.bot.send_message(ADMIN_ID, f'Новая заявка SIX SEVEN #{rid}\nУстройство: {data.device_id}\nКомментарий: {data.note}', reply_markup=keyboard)
    return {'ok': True, 'request_id': rid, 'message': 'Заявка отправлена администратору в Telegram'}

async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data: return
    if q.from_user.id != ADMIN_ID:
        await q.answer('Нет доступа', show_alert=True); return
    _, rid, days = q.data.split(':')
    with db() as c:
        req = c.execute('SELECT * FROM requests WHERE id=?', (int(rid),)).fetchone()
        if not req or req['status'] != 'pending':
            await q.answer('Заявка уже обработана'); return
        key = make_key(); expires = (utcnow() + timedelta(days=int(days))).isoformat()
        c.execute('INSERT INTO licenses(key_hash,expires,max_devices,created) VALUES(?,?,3,?)', (h(key),expires,utcnow().isoformat()))
        c.execute("UPDATE requests SET status='approved' WHERE id=?", (int(rid),))
    await q.edit_message_text(f'Заявка #{rid} одобрена. Ключ на {days} дней:\n`{key}`\nУстройств: 3', parse_mode='Markdown')
    await q.answer('Ключ создан')

@app.post('/activate')
def activate(data: ActivateIn):
    if data.device_id in OWNER_IDS:
        return {'ok': True, 'plan': 'developer', 'expires': None, 'max_devices': 999, 'message': 'Доступ разработчика активен навсегда'}
    kh = h(data.key.strip().upper())
    with db() as c:
        lic = c.execute('SELECT * FROM licenses WHERE key_hash=?', (kh,)).fetchone()
        if not lic or lic['revoked']: raise HTTPException(403, 'Ключ недействителен или отозван')
        if datetime.fromisoformat(lic['expires']) <= utcnow(): raise HTTPException(403, 'Срок действия ключа истёк')
        existing = c.execute('SELECT 1 FROM activations WHERE key_hash=? AND device_id=?', (kh,data.device_id)).fetchone()
        count = c.execute('SELECT COUNT(*) n FROM activations WHERE key_hash=?', (kh,)).fetchone()['n']
        if not existing and count >= lic['max_devices']: raise HTTPException(403, 'Достигнут лимит устройств: 3')
        c.execute('INSERT OR IGNORE INTO activations(key_hash,device_id,created) VALUES(?,?,?)', (kh,data.device_id,utcnow().isoformat()))
    return {'ok': True, 'plan': 'user', 'expires': lic['expires'], 'max_devices': 3, 'message': 'Ключ активирован'}

@app.get('/validate')
def validate(device_id: str):
    if device_id in OWNER_IDS: return {'ok': True, 'plan': 'developer', 'expires': None}
    with db() as c:
        row = c.execute('SELECT l.expires,l.revoked FROM activations a JOIN licenses l ON l.key_hash=a.key_hash WHERE a.device_id=? ORDER BY a.id DESC LIMIT 1', (device_id,)).fetchone()
    if not row or row['revoked'] or datetime.fromisoformat(row['expires']) <= utcnow(): return {'ok': False, 'message': 'Нет активной подписки'}
    return {'ok': True, 'plan': 'user', 'expires': row['expires']}

# Admin helper command in Telegram: /whoami returns your numeric ID.
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'Ваш Telegram ID: {update.effective_user.id}')
