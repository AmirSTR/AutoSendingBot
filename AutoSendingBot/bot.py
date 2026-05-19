import asyncio
import warnings
import logging
from telegram.warnings import PTBUserWarning
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import vk_api
from database import Database
from config import TG_TOKEN, VK_TOKEN, DATABASE_URL, ALLOWED_USER_ID, WEBHOOK_URL, PORT

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")

def _now_msk() -> datetime:
    """Текущее время в Москве как naive datetime — совместимо с хранилищем в БД."""
    return datetime.now(_MSK).replace(tzinfo=None)


WAIT_MESSAGE, WAIT_PEER_ID, WAIT_DATETIME, WAIT_REPEAT_CHOICE, WAIT_REPEAT_HOURS, WAIT_DAYS_SELECTION = range(6)

db = Database()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

vk = None  # инициализируется в main() после проверки токена

tg_app = None
user_chat_id = None  # устанавливается при первом /start

MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📝 Новая задача"), KeyboardButton("📋 Мои задачи")],
    [KeyboardButton("⏸ Пауза"),          KeyboardButton("▶️ Возобновить")],
    [KeyboardButton("🗑 Удалить задачу")],
], resize_keyboard=True)

CANCEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Отмена")]],
    resize_keyboard=True,
)


def check_auth(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


async def send_vk_message(peer_id: int, message: str) -> bool:
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: vk.messages.send(
                peer_id=peer_id,
                message=message,
                random_id=random.randint(1, 2**31),
            )
        )
        logger.info(f"Отправлено в VK peer_id={peer_id}: {message[:50]}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки в VK: {e}")
        return False


def schedule_job(task: dict):
    job_id = f"task_{task['id']}"

    async def job_func():
        ok = await send_vk_message(task['peer_id'], task['message'])
        notify_id = user_chat_id or (ALLOWED_USER_ID if ALLOWED_USER_ID else None)
        if notify_id and tg_app:
            icon = "✅" if ok else "❌"
            verb = "отправлено в ВК" if ok else "ошибка отправки в ВК"
            try:
                await tg_app.bot.send_message(
                    chat_id=notify_id,
                    text=(
                        f"{icon} Задача #{task['id']} — {verb}\n"
                        f"📨 {task['message'][:60]}\n"
                        f"📬 peer_id: {task['peer_id']}"
                    ),
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления: {e}")
        if task['repeat_type'] == 'once':
            db.delete_task(task['id'])
        else:
            # Обновляем next_run в БД чтобы карточка показывала актуальное время
            # Сохраняем как naive MSK строку (без tzinfo) — совместимо с остальным кодом
            job = scheduler.get_job(job_id)
            if job and job.next_run_time:
                msk_naive = job.next_run_time.astimezone(_MSK).replace(tzinfo=None)
                db.update_next_run(task['id'], msk_naive.isoformat())

    if task['repeat_type'] == 'once':
        run_date = datetime.fromisoformat(task['next_run'])
        scheduler.add_job(job_func, 'date', run_date=run_date, id=job_id, replace_existing=True)

    elif task['repeat_type'] == 'interval':
        minutes = int(task['repeat_value'])
        scheduler.add_job(
            job_func, IntervalTrigger(minutes=minutes),
            id=job_id, replace_existing=True,
            next_run_time=datetime.fromisoformat(task['next_run']),
        )

    elif task['repeat_type'] == 'daily':
        hour, minute = task['repeat_value'].split(':')
        scheduler.add_job(
            job_func, CronTrigger(hour=int(hour), minute=int(minute), timezone="Europe/Moscow"),
            id=job_id, replace_existing=True,
        )

    elif task['repeat_type'] == 'weekly':
        day, hour, minute = task['repeat_value'].split(':')
        scheduler.add_job(
            job_func, CronTrigger(
                day_of_week=int(day), hour=int(hour), minute=int(minute), timezone="Europe/Moscow"
            ),
            id=job_id, replace_existing=True,
        )

    elif task['repeat_type'] == 'weekly_days':
        parts = task['repeat_value'].split(':')
        days_str, hour, minute = parts[0], int(parts[1]), int(parts[2])
        scheduler.add_job(
            job_func, CronTrigger(day_of_week=days_str, hour=hour, minute=minute, timezone="Europe/Moscow"),
            id=job_id, replace_existing=True,
        )


def repeat_label(repeat_type, repeat_value):
    if repeat_type == 'once':
        return 'Без повтора'
    if repeat_type == 'interval':
        return f'Каждые {repeat_value} мин.'
    if repeat_type == 'daily':
        return f'Каждый день в {repeat_value}'
    if repeat_type == 'weekly':
        days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        parts = repeat_value.split(':')
        return f'Каждую неделю {days[int(parts[0])]} в {parts[1]}:{parts[2]}'
    if repeat_type == 'weekly_days':
        day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        parts = repeat_value.split(':')
        labels = ', '.join(day_names[int(d)] for d in parts[0].split(','))
        return f'По {labels} в {parts[1]}:{parts[2]}'
    return repeat_value


def parse_datetime(text: str) -> datetime | None:
    """Accept 'сегодня HH:MM', 'завтра HH:MM', or 'DD.MM.YYYY HH:MM'."""
    text = text.strip().lower()
    now = _now_msk()

    if text.startswith('сегодня '):
        try:
            t = datetime.strptime(text[len('сегодня '):], '%H:%M')
            return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            return None

    if text.startswith('завтра '):
        try:
            t = datetime.strptime(text[len('завтра '):], '%H:%M')
            return (now + timedelta(days=1)).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            return None

    try:
        return datetime.strptime(text, '%d.%m.%Y %H:%M')
    except ValueError:
        return None


def _days_keyboard(selected: set) -> InlineKeyboardMarkup:
    names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    row1 = [
        InlineKeyboardButton(f"{'✅ ' if i in selected else ''}{names[i]}", callback_data=f"toggle_day:{i}")
        for i in range(4)
    ]
    row2 = [
        InlineKeyboardButton(f"{'✅ ' if i in selected else ''}{names[i]}", callback_data=f"toggle_day:{i}")
        for i in range(4, 7)
    ]
    done = [InlineKeyboardButton("✅ Готово", callback_data="days_done")]
    return InlineKeyboardMarkup([row1, row2, done])


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime('%d.%m.%Y %H:%M')


def _task_card_text(t: dict) -> str:
    status = "⏸ На паузе" if t['paused'] else "▶️ Активна"
    return (
        f"{'⏸' if t['paused'] else '▶️'} Задача #{t['id']} — {status}\n"
        f"📨 {t['message'][:80]}\n"
        f"📬 peer_id: {t['peer_id']}\n"
        f"🕐 Следующий запуск: {_fmt_dt(t['next_run'])}\n"
        f"🔄 {repeat_label(t['repeat_type'], t['repeat_value'])}"
    )


def _task_card_kb(t: dict) -> InlineKeyboardMarkup:
    if t['paused']:
        toggle = InlineKeyboardButton("▶️ Возобновить", callback_data=f"task_resume:{t['id']}")
    else:
        toggle = InlineKeyboardButton("⏸ Пауза", callback_data=f"task_pause:{t['id']}")
    delete = InlineKeyboardButton("🗑 Удалить", callback_data=f"task_del_ask:{t['id']}")
    return InlineKeyboardMarkup([[toggle, delete]])


def _task_confirmation(task_id, data, repeat_type, repeat_value) -> str:
    return (
        f"✅ Задача #{task_id} создана!\n\n"
        f"📨 Сообщение: {data['message'][:50]}\n"
        f"📬 peer_id: {data['peer_id']}\n"
        f"🕐 Первая отправка: {_fmt_dt(data['next_run'])}\n"
        f"🔄 Повтор: {repeat_label(repeat_type, repeat_value)}"
    )


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_chat_id
    if not check_auth(update.effective_user.id):
        return
    user_chat_id = update.effective_chat.id
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я бот-планировщик сообщений для ВКонтакте.\n"
        "Используй кнопки ниже для управления задачами.",
        reply_markup=MAIN_KB,
    )


# ── /add conversation ─────────────────────────────────────────────────────────

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "📝 Введи текст сообщения, которое нужно отправить в ВК:",
        reply_markup=CANCEL_KB,
    )
    return WAIT_MESSAGE


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Создание задачи отменено.", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def _get_vk_chat_keyboard() -> InlineKeyboardMarkup | None:
    """Подгружает беседы из VK и возвращает inline-клавиатуру с кнопками выбора."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: vk.messages.getConversations(count=20, filter='all', extended=1)
        )
        profiles = {p['id']: p for p in result.get('profiles', [])}
        groups = {g['id']: g for g in result.get('groups', [])}
        buttons = []
        for item in result.get('items', []):
            conv = item['conversation']
            peer = conv['peer']
            peer_id = peer['id']
            peer_type = peer['type']
            if peer_type == 'chat':
                title = conv.get('chat_settings', {}).get('title', f'Беседа {peer["local_id"]}')
                icon = '👥'
            elif peer_type == 'user':
                u = profiles.get(peer_id, {})
                title = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or f'id{peer_id}'
                icon = '👤'
            elif peer_type == 'group':
                g = groups.get(abs(peer_id), {})
                title = g.get('name', f'Группа {peer_id}')
                icon = '📢'
            else:
                title, icon = f'peer_id {peer_id}', '💬'
            buttons.append([InlineKeyboardButton(
                f"{icon} {title[:40]}", callback_data=f"select_chat:{peer_id}"
            )])
        if not buttons:
            return None
        buttons.append([InlineKeyboardButton("✏️ Ввести peer_id вручную", callback_data="peer_manual")])
        return InlineKeyboardMarkup(buttons)
    except Exception as e:
        logger.error(f"Ошибка получения чатов VK: {e}")
        return None


async def add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['message'] = update.message.text
    kb = await _get_vk_chat_keyboard()
    if kb:
        await update.message.reply_text("📬 Выбери чат для отправки:", reply_markup=kb)
    else:
        await update.message.reply_text(
            "📬 Введи peer_id чата ВК\n\n"
            "• Беседа: число из ссылки + 2 000 000 000\n"
            "  /convo/4 → peer_id = 2000000004\n"
            "• Личка: ID пользователя (например: 123456)\n"
            "• Группа: −ID группы (например: −987654)\n\n"
            "Введи peer_id:",
            reply_markup=CANCEL_KB,
        )
    return WAIT_PEER_ID


async def select_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['peer_id'] = int(query.data.split(':')[1])
    await query.edit_message_text(
        f"✅ Чат выбран (peer_id: {context.user_data['peer_id']})\n\n"
        "🕐 Введи дату и время первой отправки:\n\n"
        "• сегодня 14:30\n"
        "• завтра 09:00\n"
        "• 25.04.2026 14:30\n\n"
        "Время московское (МСК)."
    )
    return WAIT_DATETIME


async def peer_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📬 Введи peer_id чата ВК\n\n"
        "• Беседа: число из ссылки + 2 000 000 000\n"
        "  /convo/4 → peer_id = 2000000004\n"
        "• Личка: ID пользователя (например: 123456)\n"
        "• Группа: −ID группы (например: −987654)\n\n"
        "Введи peer_id:"
    )
    return WAIT_PEER_ID


async def add_peer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['peer_id'] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ peer_id должен быть числом. Попробуй ещё раз:")
        return WAIT_PEER_ID

    await update.message.reply_text(
        "🕐 Введи дату и время первой отправки:\n\n"
        "• сегодня 14:30\n"
        "• завтра 09:00\n"
        "• 25.04.2026 14:30\n\n"
        "Время московское (МСК).",
        reply_markup=CANCEL_KB,
    )
    return WAIT_DATETIME


async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = parse_datetime(update.message.text)
    if dt is None:
        await update.message.reply_text(
            "❌ Не понял дату. Попробуй:\n"
            "• сегодня 14:30\n"
            "• завтра 09:00\n"
            "• 25.04.2026 14:30"
        )
        return WAIT_DATETIME

    if dt < _now_msk():
        await update.message.reply_text(
            "❌ Это время уже прошло. Введи будущую дату и время:"
        )
        return WAIT_DATETIME

    context.user_data['next_run'] = dt.isoformat()
    keyboard = [
        [InlineKeyboardButton("🔂 Раз (без повтора)",        callback_data="repeat:once")],
        [InlineKeyboardButton("🔁 Каждые N минут",           callback_data="repeat:interval")],
        [InlineKeyboardButton("📅 Каждый день в это время",  callback_data="repeat:daily")],
        [InlineKeyboardButton("📆 Раз в неделю",             callback_data="repeat:weekly")],
        [InlineKeyboardButton("🗓 Выбрать дни недели",       callback_data="repeat:weekly_days")],
    ]
    await update.message.reply_text("🔄 Выбери режим повтора:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_REPEAT_CHOICE


async def add_repeat_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    repeat_type = query.data.split(':')[1]
    context.user_data['repeat_type'] = repeat_type

    if repeat_type == 'once':
        await _save_task_from_query(query, context, repeat_type, '')
        return ConversationHandler.END

    if repeat_type == 'interval':
        await query.edit_message_text("Каждые сколько минут отправлять?\nВведи число (например: 60):")
        return WAIT_REPEAT_HOURS

    if repeat_type == 'daily':
        dt = datetime.fromisoformat(context.user_data['next_run'])
        await _save_task_from_query(query, context, repeat_type, f"{dt.hour:02d}:{dt.minute:02d}")
        return ConversationHandler.END

    if repeat_type == 'weekly':
        days_kb = [
            [InlineKeyboardButton("Пн", callback_data="day:0"),
             InlineKeyboardButton("Вт", callback_data="day:1"),
             InlineKeyboardButton("Ср", callback_data="day:2"),
             InlineKeyboardButton("Чт", callback_data="day:3")],
            [InlineKeyboardButton("Пт", callback_data="day:4"),
             InlineKeyboardButton("Сб", callback_data="day:5"),
             InlineKeyboardButton("Вс", callback_data="day:6")],
        ]
        await query.edit_message_text("Выбери день недели:", reply_markup=InlineKeyboardMarkup(days_kb))
        return WAIT_REPEAT_CHOICE

    if repeat_type == 'weekly_days':
        context.user_data['selected_days'] = set()
        await query.edit_message_text(
            "🗓 Выбери дни недели (можно несколько), затем нажми «Готово»:",
            reply_markup=_days_keyboard(set()),
        )
        return WAIT_DAYS_SELECTION

    logger.error(f"Неизвестный repeat_type: {repeat_type}")
    await query.edit_message_text("❌ Что-то пошло не так. Начни создание задачи заново.")
    return ConversationHandler.END


async def add_repeat_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        minutes = int(update.message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи целое число минут (например: 60):")
        return WAIT_REPEAT_HOURS
    await _save_task_from_message(update, context, 'interval', str(minutes))
    return ConversationHandler.END


async def add_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = query.data.split(':')[1]
    dt = datetime.fromisoformat(context.user_data['next_run'])
    await _save_task_from_query(query, context, 'weekly', f"{day}:{dt.hour:02d}:{dt.minute:02d}")
    return ConversationHandler.END


async def toggle_day_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    day = int(query.data.split(':')[1])
    selected: set = context.user_data.setdefault('selected_days', set())
    if day in selected:
        selected.discard(day)
    else:
        selected.add(day)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=_days_keyboard(selected))
    return WAIT_DAYS_SELECTION


async def days_done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    selected: set = context.user_data.get('selected_days', set())
    if not selected:
        await query.answer("⚠️ Выбери хотя бы один день!", show_alert=True)
        return WAIT_DAYS_SELECTION
    await query.answer()
    dt = datetime.fromisoformat(context.user_data['next_run'])
    days_str = ','.join(str(d) for d in sorted(selected))
    repeat_value = f"{days_str}:{dt.hour:02d}:{dt.minute:02d}"
    await _save_task_from_query(query, context, 'weekly_days', repeat_value)
    return ConversationHandler.END


async def _save_task_from_query(query, context, repeat_type, repeat_value):
    data = context.user_data
    task_id = db.add_task(
        message=data['message'],
        peer_id=data['peer_id'],
        next_run=data['next_run'],
        repeat_type=repeat_type,
        repeat_value=repeat_value,
    )
    schedule_job(db.get_task(task_id))
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        _task_confirmation(task_id, data, repeat_type, repeat_value),
        reply_markup=MAIN_KB,
    )


async def _save_task_from_message(update, context, repeat_type, repeat_value):
    data = context.user_data
    task_id = db.add_task(
        message=data['message'],
        peer_id=data['peer_id'],
        next_run=data['next_run'],
        repeat_type=repeat_type,
        repeat_value=repeat_value,
    )
    schedule_job(db.get_task(task_id))
    await update.message.reply_text(
        _task_confirmation(task_id, data, repeat_type, repeat_value),
        reply_markup=MAIN_KB,
    )


# ── /list ─────────────────────────────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = db.get_all_tasks()
    if not tasks:
        await update.message.reply_text("📭 Нет запланированных задач.", reply_markup=MAIN_KB)
        return

    active = sum(1 for t in tasks if not t['paused'])
    paused = len(tasks) - active
    await update.message.reply_text(
        f"📋 Задач всего: {len(tasks)}  (▶️ активных: {active} / ⏸ на паузе: {paused})",
        reply_markup=MAIN_KB,
    )
    for t in tasks:
        await update.message.reply_text(_task_card_text(t), reply_markup=_task_card_kb(t))


# ── task card inline actions ──────────────────────────────────────────────────

async def task_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not check_auth(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    db.set_paused(task_id, True)
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.pause_job(job_id)
    updated = {**task, 'paused': 1}
    await query.edit_message_text(_task_card_text(updated), reply_markup=_task_card_kb(updated))


async def task_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not check_auth(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    if (task['repeat_type'] == 'once'
            and datetime.fromisoformat(task['next_run']) < _now_msk()):
        await query.edit_message_text(
            f"⚠️ Задача #{task_id} не может быть возобновлена — время отправки уже прошло.\n"
            "Удали задачу и создай новую."
        )
        return
    db.set_paused(task_id, False)
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)
    else:
        schedule_job(task)
    updated = {**task, 'paused': 0}
    await query.edit_message_text(_task_card_text(updated), reply_markup=_task_card_kb(updated))


async def task_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not check_auth(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"task_del_yes:{task_id}"),
        InlineKeyboardButton("❌ Отмена",       callback_data=f"task_del_no:{task_id}"),
    ]])
    await query.edit_message_text(
        f"⚠️ Удалить задачу #{task_id}?\n"
        f"📨 {task['message'][:60]}\n\n"
        f"Это действие необратимо.",
        reply_markup=kb,
    )


async def task_del_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not check_auth(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    task_id = int(query.data.split(':')[1])
    db.delete_task(task_id)
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await query.edit_message_text(f"🗑 Задача #{task_id} удалена.")


async def task_del_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not check_auth(query.from_user.id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    await query.edit_message_text(_task_card_text(task), reply_markup=_task_card_kb(task))


# ── /delete /pause /resume (keyboard-button entry points) ────────────────────

async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = db.get_all_tasks()
    if not tasks:
        await update.message.reply_text("📭 Нет задач для удаления.", reply_markup=MAIN_KB)
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_del_ask:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для удаления:", reply_markup=InlineKeyboardMarkup(kb))


async def pause_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = [t for t in db.get_all_tasks() if not t['paused']]
    if not tasks:
        await update.message.reply_text("Нет активных задач.", reply_markup=MAIN_KB)
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_pause:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для паузы:", reply_markup=InlineKeyboardMarkup(kb))


async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = [t for t in db.get_all_tasks() if t['paused']]
    if not tasks:
        await update.message.reply_text("Нет приостановленных задач.", reply_markup=MAIN_KB)
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_resume:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для возобновления:", reply_markup=InlineKeyboardMarkup(kb))


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if not TG_TOKEN:
        raise RuntimeError("Переменная окружения TG_TOKEN не задана")
    if not VK_TOKEN:
        raise RuntimeError("Переменная окружения VK_TOKEN не задана")
    if not DATABASE_URL:
        raise RuntimeError("Переменная окружения DATABASE_URL не задана")

    global tg_app, vk
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    tg_app = Application.builder().token(TG_TOKEN).build()

    cancel_filter = filters.Regex('^❌ Отмена$')
    text_no_cmd = filters.TEXT & ~filters.COMMAND & ~cancel_filter

    warnings.filterwarnings('ignore', message="If 'per_message=False'", category=PTBUserWarning)
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler('add', add_start),
            MessageHandler(filters.Regex('^📝 Новая задача$'), add_start),
        ],
        states={
            WAIT_MESSAGE:      [MessageHandler(text_no_cmd, add_message)],
            WAIT_PEER_ID: [
                CallbackQueryHandler(select_chat_callback, pattern='^select_chat:'),
                CallbackQueryHandler(peer_manual_callback,  pattern='^peer_manual$'),
                MessageHandler(text_no_cmd, add_peer_id),
            ],
            WAIT_DATETIME:     [MessageHandler(text_no_cmd, add_datetime)],
            WAIT_REPEAT_CHOICE: [
                CallbackQueryHandler(add_repeat_choice, pattern='^repeat:'),
                CallbackQueryHandler(add_weekday,        pattern='^day:'),
            ],
            WAIT_REPEAT_HOURS: [MessageHandler(text_no_cmd, add_repeat_minutes)],
            WAIT_DAYS_SELECTION: [
                CallbackQueryHandler(toggle_day_selection, pattern='^toggle_day:'),
                CallbackQueryHandler(days_done_handler,    pattern='^days_done$'),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(cancel_filter, cancel_conv),
        ],
        per_message=False,
        conversation_timeout=300,
    )

    tg_app.add_handler(CommandHandler('start', start))
    tg_app.add_handler(add_conv)
    tg_app.add_handler(CommandHandler('list',   list_tasks))
    tg_app.add_handler(CommandHandler('delete', delete_task))
    tg_app.add_handler(CommandHandler('pause',  pause_task))
    tg_app.add_handler(CommandHandler('resume', resume_task))

    # Reply-keyboard button handlers
    tg_app.add_handler(MessageHandler(filters.Regex('^📋 Мои задачи$'),    list_tasks))
    tg_app.add_handler(MessageHandler(filters.Regex('^⏸ Пауза$'),          pause_task))
    tg_app.add_handler(MessageHandler(filters.Regex('^▶️ Возобновить$'),    resume_task))
    tg_app.add_handler(MessageHandler(filters.Regex('^🗑 Удалить задачу$'), delete_task))

    # Inline-button handlers
    tg_app.add_handler(CallbackQueryHandler(task_pause,   pattern='^task_pause:'))
    tg_app.add_handler(CallbackQueryHandler(task_resume,  pattern='^task_resume:'))
    tg_app.add_handler(CallbackQueryHandler(task_del_ask, pattern='^task_del_ask:'))
    tg_app.add_handler(CallbackQueryHandler(task_del_yes, pattern='^task_del_yes:'))
    tg_app.add_handler(CallbackQueryHandler(task_del_no,  pattern='^task_del_no:'))

    async def on_startup(app):
        tasks = db.get_all_tasks()
        skipped = []
        for task in tasks:
            if not task['paused']:
                try:
                    if (task['repeat_type'] == 'once'
                            and datetime.fromisoformat(task['next_run']) < _now_msk()):
                        skipped.append(task)
                        db.delete_task(task['id'])
                        logger.warning(f"Пропущена задача #{task['id']} — время уже прошло")
                    else:
                        if task['repeat_type'] == 'interval':
                            next_run = datetime.fromisoformat(task['next_run'])
                            now = _now_msk()
                            if next_run < now:
                                minutes = int(task['repeat_value'])
                                elapsed = (now - next_run).total_seconds() / 60
                                periods = int(elapsed / minutes) + 1
                                next_run = next_run + timedelta(minutes=minutes * periods)
                                db.update_next_run(task['id'], next_run.isoformat())
                                task = {**task, 'next_run': next_run.isoformat()}
                        schedule_job(task)
                except Exception as e:
                    logger.warning(f"Не удалось загрузить задачу #{task['id']}: {e}")
        scheduler.start()
        if skipped:
            notify_id = user_chat_id or (ALLOWED_USER_ID if ALLOWED_USER_ID else None)
            if notify_id:
                text = "⚠️ Бот был выключен и пропустил следующие задачи:\n\n"
                for t in skipped:
                    text += f"• #{t['id']} — {t['message'][:40]}\n  ⏰ Планировалось: {_fmt_dt(t['next_run'])}\n"
                text += "\nЗадачи удалены."
                try:
                    await app.bot.send_message(chat_id=notify_id, text=text)
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление о пропущенных задачах: {e}")
        logger.info("Бот запущен!")

    tg_app.post_init = on_startup

    if WEBHOOK_URL:
        tg_app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        tg_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
