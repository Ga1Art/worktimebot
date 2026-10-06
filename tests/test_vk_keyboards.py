"""Check real menu layouts without loading messaging SDKs or credentials."""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'bot/adapters/vk_handlers.py'


class VKKeyboardTests(unittest.TestCase):
    def check_menu(self, name, expected_rows):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        constants = [n for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id.startswith('BTN_') for t in n.targets)]
        definitions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
        for definition in definitions:
            namespace = {'build_keyboard': lambda rows: rows}
            exec(compile(ast.Module(body=constants + [definition], type_ignores=[]), str(SOURCE), 'exec'), namespace)
            rows = namespace[name]()
            self.assertEqual(len(rows), expected_rows)
            self.assertLessEqual(len(rows), 10)
            self.assertTrue(all(1 <= len(row) <= 4 for row in rows))
            labels = [label for row in rows for label in row]
            self.assertEqual(len(labels), len(set(labels)))
            self.assertIn('Обновить проекты Yougile', labels)
            self.assertIn('Заявки за прошлый месяц', labels)
            for action in ('BTN_ADMIN_ADD_PROJECT', 'BTN_ADMIN_DELETE_PROJECT', 'BTN_ADMIN_SET_RATE',
                           'BTN_ADMIN_USERS', 'BTN_ADMIN_EXPENSES', 'BTN_ADMIN_CLOSE_MONTH'):
                self.assertIn(namespace[action], labels)

    def test_admin_main_menu(self):
        self.check_menu('admin_menu_keyboard', 10)

    def test_admin_shortcuts(self):
        self.check_menu('admin_shortcuts_keyboard', 9)


if __name__ == '__main__':
    unittest.main()
