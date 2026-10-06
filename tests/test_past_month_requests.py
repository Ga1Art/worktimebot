import ast
from datetime import date
from decimal import Decimal
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_project_accounting import db, load_module, ROOT

with patch.dict('sys.modules', {'services.db': db}):
    past = load_module('_test_past_requests', 'services/past_month_requests.py')
views = load_module('_test_past_views', 'bot/past_month_requests.py')


def request_row(**overrides):
    values = dict(id=12, worker_id=3, full_name='Иван', request_date=date(2026, 9, 25),
                  entry_type='install', project_id=7, project_name='Проект', hours=Decimal('8'),
                  amount=None, description=None, source_platform='telegram', source_peer_id=99,
                  status='pending', archive_sync_pending=False)
    values.update(overrides)
    return tuple(values[field] for field in past.FIELDS)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        self.cursor = self.conn.cursor.return_value
        self.connection_patch = patch.object(past, 'get_connection', return_value=self.conn)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)

    def inserts(self, table):
        return [call.args for call in self.cursor.execute.call_args_list if f'INSERT INTO {table}' in call.args[0]]

    def test_installation_is_saved_with_original_date_and_project(self):
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), request_row(), (55,)]
        result = past.decide_request(12, True, 'telegram:1')
        self.assertTrue(result['changed'])
        self.assertEqual(self.inserts('work_logs')[0][1], (3, date(2026, 9, 25), 'install', 7, 'Проект', Decimal('8')))
        self.assertEqual(result['request']['status'], 'approved')
        self.assertTrue(result['request']['archive_sync_pending'])
        self.assertEqual(self.conn.commit.call_count, 2)

    def test_expense_is_approved_in_one_step(self):
        row = request_row(entry_type='expense', hours=None, amount=Decimal('100'), description='Материалы')
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), row, (56,)]
        past.decide_request(12, True, 'vk:1')
        query, params = self.inserts('expenses')[0]
        self.assertIn("'approved'", query)
        self.assertEqual(params, (3, date(2026, 9, 25), Decimal('100'), 'Материалы', 7, 'telegram', 99))
        self.assertEqual(self.inserts('work_logs'), [])

    def test_rejected_request_creates_no_accounting_record(self):
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), request_row()]
        result = past.decide_request(12, False, 'vk:1')
        self.assertEqual(result['request']['status'], 'rejected')
        self.assertFalse(result['request']['archive_sync_pending'])
        self.assertEqual(self.inserts('work_logs') + self.inserts('expenses'), [])

    def test_repeated_approval_does_not_insert_a_second_record(self):
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), request_row(status='approved', archive_sync_pending=True)]
        result = past.decide_request(12, True, 'telegram:2')
        self.assertFalse(result['changed'])
        self.assertEqual(self.inserts('work_logs') + self.inserts('expenses'), [])
        queries = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertTrue(any('FOR UPDATE' in query for query in queries))

    def test_cannot_approve_a_rejected_request(self):
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), request_row(status='rejected')]
        result = past.decide_request(12, True, 'telegram:2')
        self.assertEqual(result['request']['status'], 'rejected')
        self.assertFalse(result['changed'])
        self.assertEqual(self.inserts('work_logs'), [])

    def test_missing_request_is_rejected(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ValueError):
            past.decide_request(99, True, 'telegram:2')
        self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()

    def test_record_and_decision_rollback_together(self):
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), request_row(), (55,)]
        def fail_status_update(query, *args):
            if 'UPDATE past_month_requests SET status' in query:
                raise RuntimeError('Database unavailable')
        self.cursor.execute.side_effect = fail_status_update
        with self.assertRaises(RuntimeError):
            past.decide_request(12, True, 'telegram:1')
        self.conn.rollback.assert_called_once()
        self.assertEqual(self.conn.commit.call_count, 1)  # Schema setup only, no record commit.

    def test_shift_has_no_project(self):
        self.cursor.fetchone.side_effect = [(date(2026, 9, 25),), request_row(entry_type='shift', project_id=None, project_name=None), (55,)]
        past.decide_request(12, True, 'telegram:1')
        self.assertEqual(self.inserts('work_logs')[0][1][2:5], ('shift', None, None))

    def test_invalid_numbers_cannot_be_submitted(self):
        for value in ('nan', 'inf', '-1', '0', 'abc', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                past._positive_number(value, 'Часы')

    def test_submission_is_saved_with_a_stable_unique_key(self):
        self.cursor.fetchone.side_effect = [(7, 'Проект'), (12,), request_row()]
        clock = Mock(wraps=date)
        clock.today.return_value = date(2026, 10, 6)
        form = {'request_date': '2026-09-25', 'entry_type': 'install', 'project_id': 7, 'hours': 8}
        with patch.object(past, 'date', clock):
            request = past.create_request(form, 3, 'Иван', 'stable-key', 'telegram', 99)
        query, params = self.inserts('past_month_requests')[0]
        self.assertIn('ON CONFLICT (submission_key)', query)
        self.assertEqual(params[0], 'stable-key')
        self.assertEqual(request['id'], 12)
        self.assertEqual(request['status'], 'pending')
        self.assertEqual(self.inserts('work_logs') + self.inserts('expenses'), [])

    def test_submission_for_current_month_is_rejected(self):
        clock = Mock(wraps=date)
        clock.today.return_value = date(2026, 10, 6)
        with patch.object(past, 'date', clock), self.assertRaises(ValueError):
            past.create_request({'request_date': '2026-10-05', 'entry_type': 'shift', 'hours': 8}, 3, 'Иван', 'key', 'vk', 99)
        self.conn.cursor.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        self.cursor = self.conn.cursor.return_value
        self.cursor.fetchone.return_value = (True,)
        self.cursor.fetchall.return_value = [(12,), (13,)]
        self.sheets = ModuleType('services.google_sheets')
        self.sheets.archive_monthly_report = Mock()
        self.connection_patch = patch.object(past, 'get_connection', return_value=self.conn)
        self.connection_patch.start()
        self.module_patch = patch.dict('sys.modules', {'services.google_sheets': self.sheets})
        self.module_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.addCleanup(self.module_patch.stop)

    def pending_updates(self):
        return [call for call in self.cursor.execute.call_args_list if 'SET archive_sync_pending = FALSE' in call.args[0]]

    def test_updates_only_the_requested_archive(self):
        self.assertTrue(past.sync_request_archive(2026, 9))
        self.sheets.archive_monthly_report.assert_called_once_with(2026, 9)
        self.assertEqual(self.pending_updates()[0].args[1], ([12, 13],))
        self.assertTrue(any('pg_advisory_unlock' in c.args[0] for c in self.cursor.execute.call_args_list))

    def test_sheet_failure_keeps_requests_pending_for_retry(self):
        self.sheets.archive_monthly_report.side_effect = RuntimeError('Sheets unavailable')
        with self.assertRaises(RuntimeError):
            past.sync_request_archive(2026, 9)
        self.assertEqual(self.pending_updates(), [])
        self.conn.rollback.assert_called_once()

    def test_busy_archive_remains_queued(self):
        self.cursor.fetchone.return_value = (False,)
        self.assertFalse(past.sync_request_archive(2026, 9))
        self.sheets.archive_monthly_report.assert_not_called()
        self.assertEqual(self.pending_updates(), [])


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_approve_telegram_request(self):
        tree = ast.parse((ROOT / 'bot/handlers.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callbacks')
        node.decorator_list = []
        for arg in node.args.args:
            arg.annotation = None
        process = AsyncMock()
        namespace = {'is_admin_callback': lambda _: False, 'process_request_decision': process}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'callbacks', 'exec'), namespace)
        callback = SimpleNamespace(data='past_approve_12', answer=AsyncMock())
        await namespace['callbacks'](callback, SimpleNamespace())
        process.assert_not_awaited()
        callback.answer.assert_awaited_once()

    async def test_non_admin_cannot_approve_vk_request(self):
        tree = ast.parse((ROOT / 'bot/adapters/vk_handlers.py').read_text(encoding='utf-8-sig'))
        handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
        block = next(n for n in handler.body if isinstance(n, ast.If) and 'одобрить заявку #' in ast.unparse(n.test))
        function = ast.parse('async def approve():\n    pass').body[0]
        function.body = [block]
        async def run_sync(function, *args):
            return function(*args)
        process = AsyncMock()
        namespace = {'text': 'одобрить заявку #12', 'run_sync': run_sync,
                     'is_admin_by_vk_id': lambda _: False, 'message': SimpleNamespace(from_id=3),
                     'process_request_decision': process}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), 'vk_approval', 'exec'), namespace)
        await namespace['approve']()
        process.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
