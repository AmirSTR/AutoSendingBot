"""Offline regression tests: no Telegram or VK requests are made."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduling import MSK, build_trigger, next_run_iso
import database

# Import the application against a temporary DB, never a developer's real task file.
with tempfile.TemporaryDirectory() as temp:
    with patch.object(database, 'DB_PATH', str(Path(temp) / 'import.db')):
        with patch.dict(os.environ, {'TG_TOKEN': '123:offline', 'VK_TOKEN': 'offline'}):
            import bot


class ScheduleTests(unittest.TestCase):
    def task(self, kind, value, start):
        return dict(repeat_type=kind, repeat_value=value, next_run=start)

    def fires(self, task, count):
        trigger = build_trigger(task)
        now = datetime.fromisoformat(task['next_run']).replace(tzinfo=MSK)
        result = []
        previous = None
        for _ in range(count):
            previous = trigger.get_next_fire_time(previous, now)
            result.append(previous.isoformat())
            now = previous + timedelta(seconds=1)
        return result

    def test_first_of_every_month(self):
        self.assertEqual(self.fires(self.task('monthly_days', '1:09:00', '2026-09-13T09:00:00'), 3), [
            '2026-10-01T09:00:00+03:00', '2026-11-01T09:00:00+03:00', '2026-12-01T09:00:00+03:00'])

    def test_thursday_and_sunday(self):
        self.assertEqual(self.fires(self.task('weekly_days', '3,6:09:00', '2026-09-14T09:00:00'), 4), [
            '2026-09-17T09:00:00+03:00', '2026-09-20T09:00:00+03:00',
            '2026-09-24T09:00:00+03:00', '2026-09-27T09:00:00+03:00'])

    def test_multiple_month_days_and_short_months(self):
        self.assertEqual(self.fires(self.task('monthly_days', '1,31:09:00', '2027-01-31T09:00:00'), 4), [
            '2027-01-31T09:00:00+03:00', '2027-02-01T09:00:00+03:00',
            '2027-03-01T09:00:00+03:00', '2027-03-31T09:00:00+03:00'])

    def test_leap_day(self):
        dates = self.fires(self.task('monthly_days', '29:09:00', '2028-02-01T09:00:00'), 1)
        self.assertEqual(dates, ['2028-02-29T09:00:00+03:00'])

    def test_start_date_is_respected_for_every_cron_mode(self):
        now = datetime(2026, 9, 1, tzinfo=MSK)
        for kind, value in [('daily', '09:00'), ('weekly', '3:09:00'),
                            ('weekly_days', '3,6:09:00'), ('monthly_days', '1:09:00')]:
            with self.subTest(kind=kind):
                task = self.task(kind, value, '2026-10-10T10:00:00')
                self.assertGreaterEqual(next_run_iso(task, now), task['next_run'])

    def test_interval_keeps_anchor_after_restart(self):
        task = self.task('interval', '60', '2026-09-13T09:15:00')
        self.assertEqual(next_run_iso(task, datetime(2026, 9, 13, 11, 40, tzinfo=MSK)), '2026-09-13T12:15:00')

    def test_once_unchanged(self):
        task = self.task('once', '', '2026-09-13T09:00:00')
        self.assertEqual(self.fires(task, 1), ['2026-09-13T09:00:00+03:00'])


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(database, 'DB_PATH', str(Path(self.temp.name) / 'tasks.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.db = database.Database()
        self.scheduler = bot.AsyncIOScheduler(timezone=MSK)
        self.scheduler.start(paused=True)
        self.patches = [patch.object(bot, 'db', self.db), patch.object(bot, 'scheduler', self.scheduler),
                        patch.object(bot, 'ALLOWED_USER_ID', 42), patch.object(bot, 'user_chat_id', None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.context = SimpleNamespace(user_data={})
        self.future = (datetime.now(MSK) + timedelta(days=30)).replace(tzinfo=None, second=0, microsecond=0).isoformat()

    async def asyncTearDown(self):
        self.scheduler.shutdown(wait=False)
        await asyncio.sleep(0)

    def update(self, text=None, callback=None, user=42):
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        query = None if callback is None else SimpleNamespace(
            data=callback, answer=AsyncMock(), edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(), message=message)
        return SimpleNamespace(message=message, callback_query=query, effective_message=message,
                               effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=42))

    def add(self, kind='weekly_days', value='3,6:09:00', paused=False):
        task_id = self.db.add_task('Original', 123, self.future, kind, value)
        self.db.set_paused(task_id, paused)
        bot.schedule_job(self.db.get_task(task_id))
        return task_id

    async def edit(self, task_id):
        self.assertEqual(await bot.edit_start(self.update(callback=f'task_edit:{task_id}'), self.context), bot.EDIT_CHOICE)

    async def test_edit_text_keeps_id_schedule_and_sends_new_text(self):
        task_id = self.add()
        old = self.db.get_task(task_id)
        await self.edit(task_id)
        await bot.edit_field(self.update(callback='edit_field:message'), self.context)
        self.assertEqual(await bot.edit_message(self.update(text='Updated'), self.context), bot.ConversationHandler.END)
        updated = self.db.get_task(task_id)
        self.assertEqual(updated, {**old, 'message': 'Updated'})
        self.assertEqual(len(self.db.get_all_tasks()), 1)
        self.assertEqual(len(self.scheduler.get_jobs()), 1)
        with patch.object(bot, 'send_vk_message', AsyncMock(return_value=True)) as send:
            await self.scheduler.get_job(f'task_{task_id}').func()
            send.assert_awaited_once_with(123, 'Updated')

    async def test_edit_peer_validation_and_paused_state(self):
        task_id = self.add(paused=True)
        await self.edit(task_id)
        self.assertEqual(await bot.edit_peer(self.update(text='abc'), self.context), bot.EDIT_PEER)
        self.assertEqual(self.db.get_task(task_id)['peer_id'], 123)
        await bot.edit_peer(self.update(text='2000000004'), self.context)
        self.assertEqual(self.db.get_task(task_id)['peer_id'], 2000000004)
        self.assertEqual(self.db.get_task(task_id)['paused'], 1)
        self.assertIsNone(self.scheduler.get_job(f'task_{task_id}').next_run_time)
        await bot.task_resume(self.update(callback=f'task_resume:{task_id}'), self.context)
        self.assertIsNotNone(self.scheduler.get_job(f'task_{task_id}').next_run_time)

    async def test_edit_schedule_to_monthly_preserves_pause(self):
        task_id = self.add(paused=True)
        await self.edit(task_id)
        self.assertEqual(await bot.edit_field(self.update(callback='edit_field:schedule'), self.context), bot.WAIT_DATETIME)
        date = datetime.fromisoformat(self.future).strftime('%d.%m.%Y %H:%M')
        await bot.add_datetime(self.update(text=date), self.context)
        self.assertEqual(await bot.add_repeat_choice(self.update(callback='repeat:monthly_days'), self.context), bot.WAIT_MONTH_DAYS)
        self.assertEqual(await bot.month_days_done(self.update(callback='month_done'), self.context), bot.WAIT_MONTH_DAYS)
        await bot.toggle_month_day(self.update(callback='month_day:1'), self.context)
        await bot.toggle_month_day(self.update(callback='month_day:15'), self.context)
        await bot.month_days_done(self.update(callback='month_done'), self.context)
        task = self.db.get_task(task_id)
        self.assertEqual(task['repeat_type'], 'monthly_days')
        self.assertTrue(task['repeat_value'].startswith('1,15:'))
        self.assertEqual(task['paused'], 1)
        self.assertIsNone(self.scheduler.get_job(f'task_{task_id}').next_run_time)
        self.assertEqual(len(self.scheduler.get_jobs()), 1)

    async def test_create_monthly_and_weekly_through_handlers(self):
        for kind, days, toggle, done in [
            ('monthly_days', [1], bot.toggle_month_day, bot.month_days_done),
            ('weekly_days', [3, 6], bot.toggle_day_selection, bot.days_done_handler),
        ]:
            with self.subTest(kind=kind):
                await bot.add_start(self.update(text='/add'), self.context)
                await bot.add_message(self.update(text='Hello'), self.context)
                await bot.add_peer_id(self.update(text='123'), self.context)
                await bot.add_datetime(self.update(text=datetime.fromisoformat(self.future).strftime('%d.%m.%Y %H:%M')), self.context)
                await bot.add_repeat_choice(self.update(callback=f'repeat:{kind}'), self.context)
                for day in days:
                    await toggle(self.update(callback=f'day:{day}'), self.context)
                await done(self.update(callback='done'), self.context)
        self.assertEqual({t['repeat_type'] for t in self.db.get_all_tasks()}, {'monthly_days', 'weekly_days'})

    async def test_edit_cancel_and_timeout_do_not_change_task(self):
        task_id = self.add()
        old = self.db.get_task(task_id)
        for cancel in [bot.cancel_conv, bot.edit_cancel, bot.conversation_timeout, bot.start]:
            with self.subTest(cancel=cancel.__name__):
                await self.edit(task_id)
                self.context.user_data['message'] = 'Unsaved'
                await cancel(self.update(text='/start', callback='edit_cancel'), self.context)
                self.assertEqual(self.db.get_task(task_id), old)
                self.assertEqual(self.context.user_data, {})

    async def test_deleted_task_is_not_recreated(self):
        task_id = self.add()
        await self.edit(task_id)
        self.db.delete_task(task_id)
        await bot.edit_message(self.update(text='New'), self.context)
        self.assertEqual(self.db.get_all_tasks(), [])

    async def test_unauthorized_callbacks_cannot_change_tasks(self):
        task_id = self.add()
        old = self.db.get_task(task_id)
        for action in [bot.edit_start, bot.task_pause, bot.task_resume, bot.task_del_yes]:
            update = self.update(callback=f'action:{task_id}', user=99)
            self.assertEqual(await action(update, self.context), bot.ConversationHandler.END)
            update.callback_query.answer.assert_awaited_once_with('Нет доступа', show_alert=True)
        self.assertEqual(self.db.get_task(task_id), old)

    async def test_restart_restores_monthly_trigger_from_database(self):
        task_id = self.add('monthly_days', '1:09:00')
        expected = self.scheduler.get_job(f'task_{task_id}').next_run_time
        self.scheduler.remove_all_jobs()
        bot.schedule_job(database.Database().get_task(task_id))
        self.assertEqual(self.scheduler.get_job(f'task_{task_id}').next_run_time, expected)

    async def test_reschedule_during_send_does_not_delete_edited_task(self):
        task_id = self.add('once', '')
        old_job = self.scheduler.get_job(f'task_{task_id}')

        async def sending(peer, message):
            await self.edit(task_id)
            bot._persist_task(self.context, 'monthly_days', '1:09:00')
            return True

        with patch.object(bot, 'send_vk_message', sending):
            await old_job.func()
        self.assertEqual(self.db.get_task(task_id)['repeat_type'], 'monthly_days')

    async def test_telegram_routes_edit_save_and_start_cancellation(self):
        from telegram import Update, User
        from telegram.ext import Application, ExtBot

        task_id = self.add()
        with patch.object(Application, 'run_polling'), patch.object(bot, 'WEBHOOK_URL', ''), \
             patch('telegram.request.HTTPXRequest._build_client'):
            bot.main()
        app = bot.tg_app
        app._initialized = True
        app.bot._bot_user = User(123, 'Offline', is_bot=True, username='offline_bot')
        await app.job_queue.start()
        update_id = 0

        async def process(text=None, callback=None):
            nonlocal update_id
            update_id += 1
            message = {'message_id': update_id, 'date': 1,
                       'chat': {'id': 42, 'type': 'private'},
                       'from': {'id': 42, 'first_name': 'Owner', 'is_bot': False},
                       'text': text or 'Task card'}
            if text and text.startswith('/'):
                message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text)}]
            payload = {'update_id': update_id}
            if callback:
                payload['callback_query'] = {'id': str(update_id), 'from': message['from'],
                                             'chat_instance': 'offline', 'data': callback, 'message': message}
            else:
                payload['message'] = message
            await app.process_update(Update.de_json(payload, app.bot))

        try:
            with patch.object(ExtBot, 'send_message', AsyncMock()), \
                 patch.object(ExtBot, 'answer_callback_query', AsyncMock()), \
                 patch.object(ExtBot, 'edit_message_text', AsyncMock()), \
                 patch.object(ExtBot, 'edit_message_reply_markup', AsyncMock()), \
                 patch.object(app.__class__, 'process_error', AsyncMock()) as errors:
                await process(callback=f'task_edit:{task_id}')
                await process(callback='edit_field:message')
                await process(text='Through Telegram routing')
                self.assertEqual(self.db.get_task(task_id)['message'], 'Through Telegram routing')
                await process(callback=f'task_edit:{task_id}')
                await process(callback='edit_field:peer')
                await process(text='/start')
                await process(text='999')
                self.assertEqual(self.db.get_task(task_id)['peer_id'], 123)
                self.assertEqual(app.handlers[0][0]._conversations, {})
                errors.assert_not_awaited()
        finally:
            await app.job_queue.stop(wait=False)
            app._initialized = False


if __name__ == '__main__':
    unittest.main()
