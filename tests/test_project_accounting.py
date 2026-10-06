"""Offline checks: no tokens, external services, or production DB are used."""
import ast
import importlib.util
import io
import json
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = ModuleType('config')
for name in ('DB_HOST', 'DB_NAME', 'DB_PASSWORD', 'DB_PORT', 'DB_USER'):
    setattr(config, name, '')
config.YOUGILE_API_KEY = 'test-key'
config.YOUGILE_API_BASE_URL = 'https://yougile.com/api-v2'
config.YOUGILE_SYNC_INTERVAL_SECONDS = 900
driver = ModuleType('psycopg2')
pool = ModuleType('psycopg2.pool')
pool.ThreadedConnectionPool = Mock()
with patch.dict('sys.modules', {'config': config, 'psycopg2': driver, 'psycopg2.pool': pool}):
    db = load_module('_test_db', 'services/db.py')
    with patch.dict('sys.modules', {'services.db': db}):
        yougile = load_module('_test_yougile', 'services/yougile.py')


class FixedDate:
    @staticmethod
    def today():
        return date(2026, 10, 6)


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        self.cursor = self.conn.cursor.return_value
        self.connection_patch = patch.object(db, 'get_connection', return_value=self.conn)
        self.connection_patch.start()
        self.date_patch = patch.object(db, 'date', FixedDate)
        self.date_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.addCleanup(self.date_patch.stop)

    def inserts(self, table):
        return [call.args for call in self.cursor.execute.call_args_list if f'INSERT INTO {table} ' in call.args[0]]

    def test_expense_requires_project(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ValueError):
            db.save_to_db({'date': 5, 'place': 'Расходы', 'amount': 100, 'expense_type': 'Материалы'}, 3)
        self.assertEqual(self.inserts('expenses'), [])
        self.conn.commit.assert_not_called()
        self.conn.close.assert_called_once()

    def test_expense_preserves_description_and_project_id(self):
        self.cursor.fetchone.side_effect = [(7, 'Проект'), (42,)]
        result = db.save_to_db({'date': 5, 'place': 'Расходы', 'project_id': 7, 'amount': 100, 'expense_type': 'Материалы'}, 3)
        self.assertEqual(result, 42)
        self.assertEqual(self.inserts('expenses')[0][1], (3, date(2026, 10, 5), 100, 'Материалы', 7))
        self.conn.commit.assert_called_once()

    def test_inactive_project_cannot_receive_expense(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ValueError):
            db.save_to_db({'date': 5, 'place': 'Расходы', 'project_id': 7}, 3)
        self.assertEqual(self.inserts('expenses'), [])

    def test_installation_uses_id_after_rename(self):
        self.cursor.fetchone.return_value = (7, 'Новое название')
        db.save_to_db({'date': 5, 'place': 'Монтаж', 'project_id': 7, 'project': 'Старое название', 'hours': 8}, 3)
        self.assertEqual(self.inserts('work_logs')[0][1], (3, 'install', date(2026, 10, 5), 'Новое название', 7, 8))

    def test_installation_requires_known_project(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ValueError):
            db.save_to_db({'date': 5, 'place': 'Монтаж', 'project': 'Опечатка', 'hours': 8}, 3)

    def test_shift_has_no_project_link(self):
        db.save_to_db({'date': 5, 'place': 'Смена', 'project_id': 7, 'hours': 8}, 3)
        self.assertIsNone(self.inserts('work_logs')[0][1][4])

    def test_future_date_is_rejected(self):
        with self.assertRaises(ValueError):
            db.save_to_db({'date': 7, 'place': 'Расходы', 'project_id': 7}, 3)

    def test_report_includes_inactive_project_and_separates_expense_statuses(self):
        self.cursor.fetchone.return_value = (7, 'Проект', False, 'external-id')
        self.cursor.fetchall.side_effect = [
            [(3, 'Иван', 8), (4, 'Анна', 4)],
            [(1, 'Иван', date(2026, 10, 5), 'Материалы', 100, 'approved', '/receipt'),
             (2, 'Анна', date(2026, 10, 5), 'Такси', 20, 'pending', None),
             (3, 'Анна', date(2026, 10, 5), 'Такси', 10, 'rejected', None)],
        ]
        report = db.get_project_accounting_report(7)
        self.assertFalse(report['active'])
        self.assertEqual(report['installation_hours'], 12)
        self.assertEqual(report['expense_totals'], {'approved': 100, 'pending': 20, 'rejected': 10})
        self.assertTrue(report['expenses'][0]['has_receipt'])
        self.assertNotIn('/receipt', str(report))


class SyncTests(unittest.TestCase):
    def response(self, content, next_page=False):
        return io.BytesIO(json.dumps({'content': content, 'paging': {'next': next_page}}).encode())

    def test_fetch_all_pages(self):
        pages = [self.response([{'id': 'a', 'title': 'A'}], True), self.response([{'id': 'b', 'title': 'B', 'deleted': True}])]
        with patch.object(yougile, 'urlopen', side_effect=pages) as request, patch.object(yougile.time, 'sleep'):
            result = yougile.fetch_projects()
        self.assertEqual([p['id'] for p in result], ['a', 'b'])
        self.assertIn('offset=1', request.call_args_list[1].args[0].full_url)

    def test_repeated_page_is_rejected(self):
        pages = [self.response([{'id': 'a', 'title': 'A'}], True), self.response([{'id': 'a', 'title': 'A'}])]
        with patch.object(yougile, 'urlopen', side_effect=pages), patch.object(yougile.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                yougile.fetch_projects()

    def test_bad_response_is_rejected(self):
        with patch.object(yougile, 'urlopen', return_value=io.BytesIO(b'{"error":"bad"}')):
            with self.assertRaises(RuntimeError):
                yougile.fetch_projects()

    def test_download_failure_does_not_touch_database(self):
        with patch.object(yougile, 'fetch_projects', side_effect=RuntimeError('offline')), patch.object(yougile, 'get_connection') as connect:
            with self.assertRaises(RuntimeError):
                yougile.sync_projects()
            connect.assert_not_called()

    def test_rename_keeps_local_id_and_deletion_only_deactivates(self):
        conn = Mock()
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.side_effect = [(7,), None]
        with patch.object(yougile, 'fetch_projects', return_value=[{'id': 'a', 'title': 'New', 'deleted': True}]), patch.object(yougile, 'get_connection', return_value=conn):
            yougile.sync_projects()
        updates = [call.args for call in cursor.execute.call_args_list if 'UPDATE active_projects SET name' in call.args[0]]
        self.assertEqual(updates[0][1], ('New', False, 7))
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_name_collision_rolls_back(self):
        conn = Mock()
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        conn.cursor.return_value = cursor
        cursor.fetchone.side_effect = [None, (7, 'other-id')]
        with patch.object(yougile, 'fetch_projects', return_value=[{'id': 'a', 'title': 'Same'}]), patch.object(yougile, 'get_connection', return_value=conn):
            with self.assertRaises(ValueError):
                yougile.sync_projects()
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()


class TelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_expense_project_selection_advances_to_description(self):
        await self.check_selection('waiting_project', {'place': 'Расходы'}, 'waiting_expense_type')

    async def test_previous_month_expense_also_selects_project(self):
        await self.check_selection('waiting_past_month_project', {'entry_type': 'expense'}, 'waiting_past_month_expense_description')

    async def test_installation_project_selection_advances_to_hours(self):
        await self.check_selection('waiting_project', {'place': 'Монтаж'}, 'waiting_hours')

    async def check_selection(self, current, form, target):
        # Isolate the real callback function from Telegram SDK and network setup.
        tree = ast.parse((ROOT / 'bot/handlers.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callbacks')
        node.decorator_list = []
        node.args.args[0].annotation = None
        node.args.args[1].annotation = None
        names = ['waiting_project', 'waiting_past_month_project', 'waiting_expense_type', 'waiting_past_month_expense_description', 'waiting_hours', 'waiting_past_month_hours']
        work_state = SimpleNamespace(**{n: SimpleNamespace(state=n) for n in names})
        async def run_sync(function, *args):
            return function(*args)
        namespace = {'WorkState': work_state, 'run_sync': run_sync, 'get_project_by_id': lambda _: (7, 'Проект', True), 'back_to_menu_keyboard': lambda: None, 'get_admin_worker_action_from_callback': lambda _: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'callbacks', 'exec'), namespace)
        state = SimpleNamespace(get_state=AsyncMock(return_value=current), get_data=AsyncMock(return_value=form), update_data=AsyncMock(), set_state=AsyncMock())
        callback = SimpleNamespace(data='project_pick_7_page_0', answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock()))
        await namespace['callbacks'](callback, state)
        state.set_state.assert_awaited_once_with(getattr(work_state, target))
        self.assertEqual(state.update_data.call_args.kwargs['project_id'], 7)


class VKFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_expense_selects_project_before_description(self):
        await self.check_selection('waiting_project', {'place': 'Расходы'}, 'waiting_expense_type')

    async def test_previous_month_expense_selects_project(self):
        await self.check_selection('waiting_past_month_project', {'entry_type': 'expense'}, 'waiting_past_month_expense_description')

    async def test_installation_selects_project_before_hours(self):
        await self.check_selection('waiting_project', {'place': 'Монтаж'}, 'waiting_hours')

    async def check_selection(self, current, form, target):
        tree = ast.parse((ROOT / 'bot/adapters/vk_handlers.py').read_text(encoding='utf-8-sig'))
        handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
        block = next(n for n in handler.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                     and isinstance(n.test.left, ast.Name) and n.test.left.id == 'state'
                     and isinstance(n.test.comparators[0], ast.Constant) and n.test.comparators[0].value == current)
        function = ast.parse('async def select():\n    pass').body[0]
        function.body = [block]
        async def run_sync(function, *args):
            return function(*args)
        namespace = {'state': current, 'state_data': form, 'message': SimpleNamespace(from_id=3),
                     'text_raw': 'Проект', 'text': 'проект', 'normalize_text': lambda v: v.strip().lower(),
                     'run_sync': run_sync, 'get_active_projects_cached': lambda: [(7, 'Проект')],
                     'get_user_meta': lambda *args: 0, 'clear_user_meta': Mock(), 'set_user_state': Mock(),
                     'reply': AsyncMock(), 'input_step_keyboard': lambda: None, 'build_keyboard': lambda rows: rows}
        constants = [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('BTN_') for t in n.targets)]
        module = ast.Module(body=constants + [function], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), 'vk_selection', 'exec'), namespace)
        await namespace['select']()
        saved = namespace['set_user_state'].call_args.args
        self.assertEqual(saved[1], target)
        self.assertEqual(saved[2]['project_id'], 7)


if __name__ == '__main__':
    unittest.main()
