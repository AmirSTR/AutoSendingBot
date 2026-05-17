# AutoSendingBot — справочник для Claude

## Что это за проект

Telegram-бот планировщик, который отправляет сообщения во ВКонтакте по расписанию.
Один пользователь (owner) управляет задачами через Telegram; бот шлёт VK-сообщения через VK API.

---

## Структура файлов

```
AutoSendingBot/
├── bot.py          # весь бот — handlers, scheduler, точка входа main()
├── config.py       # читает env-переменные
├── database.py     # обёртка над SQLite (data/tasks.db)
├── requirements.txt
├── Procfile        # для деплоя (Railway / Heroku)
└── runtime.txt     # версия Python
```

---

## Стек

| Библиотека | Версия | Роль |
|---|---|---|
| python-telegram-bot[webhooks] | 21.3 | Telegram Bot API |
| apscheduler | 3.10.4 | планировщик задач (AsyncIOScheduler) |
| vk-api | 11.9.9 | отправка сообщений в VK |
| sqlite3 | stdlib | хранение задач |

---

## Переменные окружения (config.py)

| Переменная | Обязательна | Описание |
|---|---|---|
| `TG_TOKEN` | да | токен Telegram-бота |
| `VK_TOKEN` | да | токен VK (user token с правом messages) |
| `ALLOWED_USER_ID` | нет (0 = все) | Telegram user_id единственного владельца |
| `WEBHOOK_URL` | нет | если задан — бот запускается в webhook-режиме |
| `PORT` | нет (8443) | порт webhook-сервера |

---

## База данных — таблица `tasks`

```sql
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message      TEXT    NOT NULL,
    peer_id      INTEGER NOT NULL,   -- VK peer_id
    next_run     TEXT    NOT NULL,   -- ISO datetime (московское время)
    repeat_type  TEXT    NOT NULL,   -- см. ниже
    repeat_value TEXT    NOT NULL DEFAULT '',
    paused       INTEGER NOT NULL DEFAULT 0,  -- 0/1
    created_at   TEXT    DEFAULT (datetime('now'))
)
```

Файл: `data/tasks.db` (создаётся автоматически).

### Типы повтора (`repeat_type` / `repeat_value`)

| repeat_type | repeat_value | Пример |
|---|---|---|
| `once` | `''` | одна отправка, задача удаляется после выполнения |
| `interval` | `"60"` | каждые N минут |
| `daily` | `"14:30"` | каждый день в HH:MM |
| `weekly` | `"0:09:00"` | раз в неделю: `день(0-6):HH:MM` |
| `weekly_days` | `"0,2,4:09:00"` | выбранные дни: `д,д,...:HH:MM` |

---

## VK peer_id — справка

- **Личка**: ID пользователя (например `123456`)
- **Беседа**: номер из ссылки `/convo/4` → `2000000004` (прибавить 2 000 000 000)
- **Группа**: `-ID` (например `-987654`)

---

## Ключевые функции bot.py

### Инициализация
- `main()` — строит `Application`, регистрирует все хендлеры, запускает scheduler
- `on_startup()` — при старте загружает активные задачи из БД и планирует их в APScheduler

### Планирование
- `schedule_job(task: dict)` — добавляет job в APScheduler согласно `repeat_type`; job_id = `"task_{id}"`
- `send_vk_message(peer_id, message)` — отправляет сообщение в VK, после чего уведомляет владельца в Telegram

### ConversationHandler — создание задачи
Состояния (int-константы 0–5):

```
WAIT_MESSAGE → WAIT_PEER_ID → WAIT_DATETIME → WAIT_REPEAT_CHOICE
    → (если interval) WAIT_REPEAT_HOURS
    → (если weekly_days) WAIT_DAYS_SELECTION
```

- `add_start` → `add_message` → `add_peer_id` → `add_datetime` → `add_repeat_choice`
- `add_repeat_minutes` — вводится число минут для interval
- `toggle_day_selection` / `days_done_handler` — мультивыбор дней недели
- `_save_task_from_query` / `_save_task_from_message` — финальное сохранение в БД + планирование

### Управление задачами (inline-кнопки и reply-кнопки)
| Действие | callback_data | Функция |
|---|---|---|
| Пауза | `task_pause:{id}` | `task_pause` — `db.set_paused(True)` + `scheduler.pause_job` |
| Возобновить | `task_resume:{id}` | `task_resume` — `db.set_paused(False)` + `scheduler.resume_job` или `schedule_job` |
| Удалить (подтверждение) | `task_del_ask:{id}` | `task_del_ask` |
| Удалить (да) | `task_del_yes:{id}` | `task_del_yes` — `db.delete_task` + `scheduler.remove_job` |
| Удалить (нет) | `task_del_no:{id}` | `task_del_no` — восстанавливает карточку |

### Хелперы
- `parse_datetime(text)` — парсит `"сегодня HH:MM"`, `"завтра HH:MM"`, `"DD.MM.YYYY HH:MM"` → `datetime`
- `repeat_label(repeat_type, repeat_value)` → человекочитаемая строка
- `_task_card_text(t)` → текст карточки задачи
- `_task_card_kb(t)` → inline-клавиатура (пауза/возобновить + удалить)
- `_fmt_dt(iso)` → `"DD.MM.YYYY HH:MM"`

---

## Reply-клавиатура (MAIN_KB)

```
📝 Новая задача    📋 Мои задачи
⏸ Пауза            ▶️ Возобновить
🗑 Удалить задачу
```

---

## Авторизация

`check_auth(user_id)` — пропускает всех если `ALLOWED_USER_ID == 0`, иначе только совпавший ID.

---

## Деплой

- **Webhook** (production): задать `WEBHOOK_URL` → `tg_app.run_webhook(listen="0.0.0.0", port=PORT, ...)`
- **Polling** (локально): не задавать `WEBHOOK_URL` → `tg_app.run_polling(...)`

---

## Частые задачи и где искать

| Задача | Файл / функция |
|---|---|
| Добавить новый тип повтора | `schedule_job()` + `add_repeat_choice()` + `repeat_label()` |
| Изменить формат peer_id или парсинг даты | `parse_datetime()`, `add_peer_id()` |
| Поменять схему БД | `database.py → _init_db()` |
| Поменять env-переменные | `config.py` |
| Отладить планировщик | `schedule_job()`, `on_startup()` |
| Добавить команду бота | `main()` — зарегистрировать `CommandHandler` или `MessageHandler` |
