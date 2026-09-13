"""Schedule construction shared by creation, editing and restart recovery."""
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

MSK = ZoneInfo('Europe/Moscow')


def build_trigger(task):
    start = datetime.fromisoformat(task['next_run'])
    start = start.replace(tzinfo=MSK) if start.tzinfo is None else start.astimezone(MSK)
    kind, value = task['repeat_type'], task['repeat_value']
    if kind == 'once':
        return DateTrigger(run_date=start, timezone=MSK)
    if kind == 'interval':
        return IntervalTrigger(minutes=int(value), start_date=start, timezone=MSK)
    fields = {}
    if kind == 'daily':
        hour, minute = value.split(':')
    elif kind in ('weekly', 'weekly_days', 'monthly_days'):
        days, hour, minute = value.split(':')
        fields['day' if kind == 'monthly_days' else 'day_of_week'] = days
    else:
        raise ValueError(f'Неизвестный режим повтора: {kind}')
    return CronTrigger(hour=int(hour), minute=int(minute), start_date=start,
                       timezone=MSK, **fields)


def next_run_iso(task, now=None):
    fire = build_trigger(task).get_next_fire_time(None, now or datetime.now(MSK))
    if fire is None:
        raise ValueError('У расписания нет следующей отправки')
    return fire.astimezone(MSK).replace(tzinfo=None).isoformat()
