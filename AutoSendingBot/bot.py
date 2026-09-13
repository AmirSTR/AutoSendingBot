import asyncio
import logging
import random
from functools import wraps
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, TypeHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scheduling import build_trigger, next_run_iso
import vk_api
from database import Database
from config import TG_TOKEN, VK_TOKEN, ALLOWED_USER_ID, WEBHOOK_URL, PORT

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")

def _now_msk() -> datetime:
    """Текущее время в Москве как naive datetime — совместимо с хранилищем в БД."""
    return datetime.now(_MSK).replace(tzinfo=None)


WAIT_MESSAGE, WAIT_PEER_ID, WAIT_DATETIME, WAIT_REPEAT_CHOICE, WAIT_REPEAT_HOURS, WAIT_DAYS_SELECTION, WAIT_MONTH_DAYS, EDIT_CHOICE, EDIT_MESSAGE, EDIT_PEER = range(10)

db = Database()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

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


def authorized(handler):
    @wraps(handler)
    async def wrapped(update, context):
        if not check_auth(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer("Нет доступа", show_alert=True)
            return ConversationHandler.END
        return await handler(update, context)
    return wrapped


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
        current = db.get_task(task['id'])
        if not current or current['paused']:
            return
        ok = await send_vk_message(current['peer_id'], current['message'])
        if user_chat_id and tg_app:
            icon = "✅" if ok else "❌"
            verb = "отправлено в ВК" if ok else "ошибка отправки в ВК"
            try:
                await tg_app.bot.send_message(
                    chat_id=user_chat_id,
                    text=(
                        f"{icon} Задача #{task['id']} — {verb}\n"
                        f"📨 {current['message'][:60]}\n"
                        f"📬 peer_id: {current['peer_id']}"
                    ),
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления: {e}")
        replacement = scheduler.get_job(job_id)
        if replacement and replacement.func is not job_func:
            # An edit replaced this job while VK was responding.
            return
        if task['repeat_type'] == 'once':
            db.delete_task(task['id'])
        else:
            # Обновляем next_run в БД чтобы карточка показывала актуальное время
            # Сохраняем как naive MSK строку (без tzinfo) — совместимо с остальным кодом
            job = scheduler.get_job(job_id)
            if job and job.next_run_time:
                msk_naive = job.next_run_time.astimezone(_MSK).replace(tzinfo=None)
                db.update_next_run(task['id'], msk_naive.isoformat())

    trigger = build_trigger(task)
    next_run = next_run_iso(task)
    db.update_next_run(task['id'], next_run)
    scheduler.add_job(
        job_func, trigger, id=job_id, replace_existing=True,
        next_run_time=None if task['paused'] else datetime.fromisoformat(next_run).replace(tzinfo=_MSK),
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
    if repeat_type == 'monthly_days':
        days, hour, minute = repeat_value.split(':')
        return f"Каждый месяц по числам {days.replace(',', ', ')} в {hour}:{minute}"
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
    edit = InlineKeyboardButton("✏️ Редактировать", callback_data=f"task_edit:{t['id']}")
    return InlineKeyboardMarkup([[edit], [toggle, delete]])


# ── /start ────────────────────────────────────────────────────────────────────

@authorized
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
    return ConversationHandler.END


# ── /add conversation ─────────────────────────────────────────────────────────

@authorized
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "📝 Введи текст сообщения, которое нужно отправить в ВК:",
        reply_markup=CANCEL_KB,
    )
    return WAIT_MESSAGE


@authorized
async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Изменения отменены.", reply_markup=MAIN_KB)
    return ConversationHandler.END


@authorized
async def add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['message'] = update.message.text
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


@authorized
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


@authorized
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
        [InlineKeyboardButton("📆 Числа каждого месяца", callback_data="repeat:monthly_days")],
    ]
    await update.message.reply_text("🔄 Выбери режим повтора:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_REPEAT_CHOICE


@authorized
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

    if repeat_type == 'monthly_days':
        context.user_data['selected_month_days'] = set()
        await query.edit_message_text(
            "📆 Выбери числа каждого месяца, затем нажми «Готово».\n"
            "Если числа нет в месяце (например, 31 февраля), отправка пропускается.",
            reply_markup=_month_days_keyboard(set()),
        )
        return WAIT_MONTH_DAYS

    logger.error(f"Неизвестный repeat_type: {repeat_type}")
    await query.edit_message_text("❌ Что-то пошло не так. Начни создание задачи заново.")
    return ConversationHandler.END


@authorized
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


@authorized
async def add_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = query.data.split(':')[1]
    dt = datetime.fromisoformat(context.user_data['next_run'])
    await _save_task_from_query(query, context, 'weekly', f"{day}:{dt.hour:02d}:{dt.minute:02d}")
    return ConversationHandler.END


@authorized
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


@authorized
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


def _persist_task(context, repeat_type, repeat_value):
    data = context.user_data
    task_id = data.get('editing_task_id')
    existing = db.get_task(task_id) if task_id is not None else None
    if task_id is not None and existing is None:
        return None
    candidate = dict(message=data['message'], peer_id=data['peer_id'],
                     next_run=data['next_run'], repeat_type=repeat_type,
                     repeat_value=repeat_value)
    candidate['next_run'] = next_run_iso(candidate)
    if existing is None:
        task_id = db.add_task(**candidate)
    elif not db.update_task(task_id, **candidate):
        return None
    task = db.get_task(task_id)
    schedule_job(task)
    return db.get_task(task_id)


async def _save_task_from_query(query, context, repeat_type, repeat_value):
    editing = 'editing_task_id' in context.user_data
    task = _persist_task(context, repeat_type, repeat_value)
    context.user_data.clear()
    await query.edit_message_reply_markup(reply_markup=None)
    if task is None:
        await query.message.reply_text("❌ Задача уже удалена. Изменения не сохранены.", reply_markup=MAIN_KB)
        return
    verb = 'обновлена' if editing else 'создана'
    await query.message.reply_text(f"✅ Задача #{task['id']} {verb}!", reply_markup=MAIN_KB)
    await query.message.reply_text(_task_card_text(task), reply_markup=_task_card_kb(task))


async def _save_task_from_message(update, context, repeat_type, repeat_value):
    editing = 'editing_task_id' in context.user_data
    task = _persist_task(context, repeat_type, repeat_value)
    context.user_data.clear()
    if task is None:
        await update.message.reply_text("❌ Задача уже удалена. Изменения не сохранены.", reply_markup=MAIN_KB)
        return
    verb = 'обновлена' if editing else 'создана'
    await update.message.reply_text(f"✅ Задача #{task['id']} {verb}!", reply_markup=MAIN_KB)
    await update.message.reply_text(_task_card_text(task), reply_markup=_task_card_kb(task))


def _month_days_keyboard(selected):
    buttons = [InlineKeyboardButton(
        f"{'✅ ' if day in selected else ''}{day}", callback_data=f"month_day:{day}"
    ) for day in range(1, 32)]
    rows = [buttons[i:i + 7] for i in range(0, len(buttons), 7)]
    return InlineKeyboardMarkup(rows + [[InlineKeyboardButton("✅ Готово", callback_data="month_done")]])


@authorized
async def toggle_month_day(update, context):
    query = update.callback_query
    day = int(query.data.split(':')[1])
    if not 1 <= day <= 31:
        await query.answer("Некорректное число", show_alert=True)
        return WAIT_MONTH_DAYS
    selected = context.user_data.setdefault('selected_month_days', set())
    selected.symmetric_difference_update({day})
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=_month_days_keyboard(selected))
    return WAIT_MONTH_DAYS


@authorized
async def month_days_done(update, context):
    query = update.callback_query
    days = context.user_data.get('selected_month_days', set())
    if not days:
        await query.answer("⚠️ Выбери хотя бы одно число!", show_alert=True)
        return WAIT_MONTH_DAYS
    await query.answer()
    dt = datetime.fromisoformat(context.user_data['next_run'])
    value = ','.join(str(day) for day in sorted(days)) + f":{dt.hour:02d}:{dt.minute:02d}"
    await _save_task_from_query(query, context, 'monthly_days', value)
    return ConversationHandler.END


@authorized
async def edit_start(update, context):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if task is None:
        await query.message.reply_text("❌ Задача не найдена.", reply_markup=MAIN_KB)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data.update(task)
    context.user_data['editing_task_id'] = task_id
    await query.message.reply_text(
        f"✏️ Редактирование задачи #{task_id}. Выбери, что изменить.\n"
        "До сохранения задача работает по прежним настройкам.",
        reply_markup=CANCEL_KB,
    )
    await query.message.reply_text("Что изменить?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Текст", callback_data="edit_field:message")],
        [InlineKeyboardButton("📬 Получателя", callback_data="edit_field:peer")],
        [InlineKeyboardButton("🗓 Расписание", callback_data="edit_field:schedule")],
        [InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")],
    ]))
    return EDIT_CHOICE


@authorized
async def edit_field(update, context):
    query = update.callback_query
    await query.answer()
    field = query.data.split(':')[1]
    if field == 'message':
        await query.edit_message_text("📝 Введи новый текст сообщения:")
        return EDIT_MESSAGE
    if field == 'peer':
        await query.edit_message_text("📬 Введи новый peer_id получателя ВК:")
        return EDIT_PEER
    await query.edit_message_text(
        "🕐 Введи дату и время начала нового расписания (МСК):\n"
        "• сегодня 14:30\n• завтра 09:00\n• 25.12.2026 14:30\n\n"
        "Затем можно выбрать дни недели или числа месяца."
    )
    return WAIT_DATETIME


@authorized
async def edit_cancel(update, context):
    await update.callback_query.answer()
    context.user_data.clear()
    await update.callback_query.edit_message_text("❌ Изменения отменены.")
    await update.effective_message.reply_text("Меню", reply_markup=MAIN_KB)
    return ConversationHandler.END


@authorized
async def edit_value(update, context):
    data = context.user_data
    task = db.get_task(data['editing_task_id'])
    if task is None:
        data.clear()
        await update.message.reply_text("❌ Задача уже удалена.", reply_markup=MAIN_KB)
        return ConversationHandler.END
    if data.get('edit_value_kind') == 'peer':
        try:
            task['peer_id'] = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ peer_id должен быть числом. Попробуй ещё раз:")
            return EDIT_PEER
    else:
        task['message'] = update.message.text
    # Read the current row so editing text cannot restore an obsolete next_run or pause state.
    data.update(task)
    await _save_task_from_message(update, context, task['repeat_type'], task['repeat_value'])
    return ConversationHandler.END


@authorized
async def edit_message(update, context):
    context.user_data['edit_value_kind'] = 'message'
    return await edit_value(update, context)


@authorized
async def edit_peer(update, context):
    context.user_data['edit_value_kind'] = 'peer'
    return await edit_value(update, context)


@authorized
async def conversation_timeout(update, context):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "⌛ Время ожидания истекло. Несохранённые изменения отменены.", reply_markup=MAIN_KB,
    )
    return ConversationHandler.END


# ── /list ─────────────────────────────────────────────────────────────────────

@authorized
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

@authorized
async def task_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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


@authorized
async def task_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    if task['repeat_type'] == 'once' and datetime.fromisoformat(task['next_run']) < _now_msk():
        await query.message.reply_text("⚠️ Время задачи прошло. Измени расписание перед возобновлением.")
        return
    db.set_paused(task_id, False)
    schedule_job(db.get_task(task_id))
    updated = db.get_task(task_id)
    await query.edit_message_text(_task_card_text(updated), reply_markup=_task_card_kb(updated))


@authorized
async def task_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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


@authorized
async def task_del_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    db.delete_task(task_id)
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await query.edit_message_text(f"🗑 Задача #{task_id} удалена.")


@authorized
async def task_del_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    await query.edit_message_text(_task_card_text(task), reply_markup=_task_card_kb(task))


# ── /delete /pause /resume (keyboard-button entry points) ────────────────────

@authorized
async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = db.get_all_tasks()
    if not tasks:
        await update.message.reply_text("📭 Нет задач для удаления.", reply_markup=MAIN_KB)
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_del_ask:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для удаления:", reply_markup=InlineKeyboardMarkup(kb))


@authorized
async def pause_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = [t for t in db.get_all_tasks() if not t['paused']]
    if not tasks:
        await update.message.reply_text("Нет активных задач.", reply_markup=MAIN_KB)
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_pause:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для паузы:", reply_markup=InlineKeyboardMarkup(kb))


@authorized
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

    global tg_app
    tg_app = Application.builder().token(TG_TOKEN).build()

    cancel_filter = filters.Regex('^❌ Отмена$')
    text_no_cmd = filters.TEXT & ~filters.COMMAND & ~cancel_filter

    add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_start, pattern=r'^task_edit:\d+$'),
            CommandHandler('add', add_start),
            MessageHandler(filters.Regex('^📝 Новая задача$'), add_start),
        ],
        states={
            EDIT_CHOICE: [CallbackQueryHandler(edit_field, pattern='^edit_field:(message|peer|schedule)$')],
            EDIT_MESSAGE: [MessageHandler(text_no_cmd, edit_message)],
            EDIT_PEER: [MessageHandler(text_no_cmd, edit_peer)],
            WAIT_MONTH_DAYS: [
                CallbackQueryHandler(toggle_month_day, pattern=r'^month_day:\d+$'),
                CallbackQueryHandler(month_days_done, pattern='^month_done$'),
            ],
            ConversationHandler.TIMEOUT: [TypeHandler(Update, conversation_timeout)],
            WAIT_MESSAGE:      [MessageHandler(text_no_cmd, add_message)],
            WAIT_PEER_ID:      [MessageHandler(text_no_cmd, add_peer_id)],
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
            CallbackQueryHandler(edit_cancel, pattern='^edit_cancel$'),
            CommandHandler('cancel', cancel_conv),
            CommandHandler('start', start),
            MessageHandler(cancel_filter, cancel_conv),
        ],
        per_message=False,
        conversation_timeout=300,
    )

    tg_app.add_handler(add_conv)
    tg_app.add_handler(CommandHandler('start', start))
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
