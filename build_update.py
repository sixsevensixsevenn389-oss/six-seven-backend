#!/usr/bin/env python3
from urllib.request import urlopen
from pathlib import Path
import shutil

SOURCE = "https://raw.githubusercontent.com/sixsevensixsevenn389-oss/six-seven-backend/main/main.py"
target = Path("main.py")
print("Downloading current SIX SEVEN main.py...")
with urlopen(SOURCE, timeout=30) as response:
    source = response.read().decode("utf-8")

if 'version="3.1.0"' not in source or "async def callback(" not in source:
    raise SystemExit("Unexpected source version; no changes made.")

old_start = '''async def start_command(update, context):
    await update.effective_message.reply_text(
        "💙 Добро пожаловать в SIX SEVEN VPN!\\n\\n"
        "Твой VPN — твои правила.\\n"
        "Здесь ты сможешь оформить доступ, "
        "управлять подпиской и получить помощь.\\n\\n"
        "Используй /whoami, чтобы узнать свой Telegram ID."
    )
'''
new_start = '''async def start_command(update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Подключить", callback_data="connect:start")]
    ])
    caption = (
        "💙 SIX SEVEN VPN\\n\\n"
        "Быстрый и удобный доступ к VPN.\\n"
        "Нажми «Подключить», чтобы начать."
    )
    photo = os.getenv("BOT_START_PHOTO", "").strip()
    if photo:
        await update.effective_message.reply_photo(
            photo=photo, caption=caption, reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(caption, reply_markup=keyboard)
'''
if old_start not in source:
    raise SystemExit("Could not match current start_command exactly; no changes made.")
source = source.replace(old_start, new_start, 1)

needle = '    if data == "checksub":\n'
connect = '''    if data == "connect:start":
        await q.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ Я подписался — проверить", callback_data="checksub")],
        ])
        await q.message.reply_text(
            "Чтобы продолжить, подпишись на официальный канал SIX SEVEN.\\n"
            "После подписки нажми «Я подписался — проверить».",
            reply_markup=kb,
        )
        return
'''
if needle not in source:
    raise SystemExit("Could not find subscription callback; no changes made.")
source = source.replace(needle, connect + needle, 1)

old_menu = '''                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒 Собрать тариф", callback_data="buy:start")],
                    [InlineKeyboardButton("📦 Мои заказы", callback_data="myorders")],
                ])
'''
new_menu = '''                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Получить ключ", callback_data="keys:help")],
                    [InlineKeyboardButton("🛠 Создать тариф", callback_data="buy:start")],
                    [InlineKeyboardButton("🛒 Купить ключ", callback_data="tariffs:list")],
                    [InlineKeyboardButton("🎟 Готовые тарифы", callback_data="tariffs:list")],
                    [InlineKeyboardButton("📦 Мои заказы", callback_data="myorders")],
                    [InlineKeyboardButton("🆘 Поддержка", callback_data="support:start")],
                ])
'''
if old_menu not in source:
    raise SystemExit("Could not find welcome menu; no changes made.")
source = source.replace(old_menu, new_menu, 1)

needle2 = '    if data == "buy:start":\n'
key_help = '''    if data == "keys:help":
        await q.answer()
        await q.message.reply_text(
            "🔑 Чтобы получить рабочий ключ, сначала выбери тариф и оплати его.\\n"
            "После подтверждения оплаты ключ появится здесь. "
            "Лицензионный ключ сам по себе ещё не создаёт VPN-профиль: "
            "для этого нужна интеграция с VPN-серверами."
        )
        return
'''
if needle2 not in source:
    raise SystemExit("Could not find tariff builder callback; no changes made.")
source = source.replace(needle2, key_help + needle2, 1)

if target.exists():
    shutil.copy2(target, target.with_suffix(".py.backup"))
target.write_text(source, encoding="utf-8")
print("Updated main.py created. Review it before deploying.")
print("Set BOT_START_PHOTO in Render to a Telegram photo file_id or image URL.")
print("Web tariff builder and VPN server provisioning are not included.")
