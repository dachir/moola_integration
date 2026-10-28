"""Sync regression tests; no site, database, or remote API required."""
import sqlite3
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from moola_integration import utils
from moola_integration.setup import after_install


class TestSync(unittest.TestCase):
    def setUp(self):
        clock = patch.object(utils, "now_datetime", return_value=datetime(2026, 10, 28, 10))
        clock.start()
        self.addCleanup(clock.stop)

    def settings(self, **values):
        defaults = dict(from_date=None, last_success_time=None,
                        resync_lookback_days=7, page_size=100)
        defaults.update(values)
        return SimpleNamespace(**defaults)

    @patch.object(utils, 'nowdate', return_value='2026-10-28')
    def test_initial_backfill_not_truncated(self, _):
        s = self.settings(from_date='2026-01-01')
        self.assertEqual(utils._sync_from_date(s), date(2026, 1, 1))

    @patch.object(utils, 'nowdate', return_value='2026-10-28')
    def test_recent_cursor_revisits_lookback(self, _):
        s = self.settings(last_success_time='2026-10-28 10:00:00')
        self.assertEqual(utils._sync_from_date(s), date(2026, 10, 21))

    @patch.object(utils, 'nowdate', return_value='2026-10-28')
    def test_old_cursor_not_skipped_after_outage(self, _):
        s = self.settings(last_success_time='2026-09-01')
        self.assertEqual(utils._sync_from_date(s), date(2026, 9, 1))

    @patch.object(utils, 'nowdate', return_value='2026-10-28')
    def test_backfill_is_lower_bound(self, _):
        s = self.settings(from_date='2026-10-25', last_success_time='2026-10-28')
        self.assertEqual(utils._sync_from_date(s), date(2026, 10, 25))

    @patch.object(utils, 'frappe')
    @patch.object(utils, '_make_je')
    @patch.object(utils, '_fetch_page')
    def test_pages_duplicates_and_failed_expense_rollback(self, fetch, make, frappe):
        fetch.side_effect = [dict(data=[{'id': 'a'}, {'id': 'b'}], hasNextPage=True),
                             dict(data=[{'id': 'c'}], hasNextPage=False)]
        make.side_effect = [('JE-a', None), (None, 'duplicate'), ValueError('submit failed')]
        s = self.settings()
        result = utils._run_sync(date(2026, 1, 1), date(2026, 10, 28), True, s)
        self.assertEqual(result, dict(fetched=3, created=1, skipped=2, errors=1))
        frappe.db.rollback.assert_called_once_with(save_point='moola_expense')
        self.assertEqual(fetch.call_args_list[1].args[1], 2)
        self.assertFalse(hasattr(s, 'db_set'))  # Partial run cannot move cursor.
        log = frappe.get_doc.return_value.insert.return_value
        self.assertEqual(log.status, 'Partial')

    @patch.object(utils, 'frappe')
    @patch.object(utils, '_make_je', return_value=(None, 'duplicate'))
    @patch.object(utils, '_fetch_page', return_value=dict(data=[{'id': 'a'}]))
    @patch.object(utils, 'now_datetime', return_value=datetime(2026, 10, 28, 10))
    def test_clean_duplicate_only_run_advances_cursor(self, now, fetch, make, frappe):
        s = self.settings()
        s.db_set = MagicMock()
        utils._run_sync(date(2026, 10, 21), date(2026, 10, 28), True, s)
        s.db_set.assert_called_once_with('last_success_time', now.return_value)

    @patch.object(utils, 'frappe')
    @patch.object(utils, '_fetch_page', return_value=dict(data=[]))
    @patch.object(utils, 'now_datetime', return_value=datetime(2026, 10, 28, 10))
    def test_historical_range_caps_cursor(self, now, fetch, frappe):
        s = self.settings()
        s.db_set = MagicMock()
        utils._run_sync(date(2026, 1, 1), date(2026, 1, 31), True, s)
        cursor = s.db_set.call_args.args[1]
        self.assertEqual(cursor.date(), date(2026, 1, 31))

    @patch.object(utils, 'frappe')
    @patch.object(utils, '_fetch_page', return_value=dict(data=[]))
    def test_manual_run_preserves_cursor(self, fetch, frappe):
        for flag in (False, '0'):
            s = self.settings(last_success_time='2026-10-28 09:00:00')
            s.db_set = MagicMock()
            utils._run_sync(date(2026, 1, 1), date(2026, 1, 31), flag, s)
            s.db_set.assert_not_called()

    @patch.object(utils, 'frappe')
    @patch.object(utils, '_fetch_page', side_effect=ValueError('network failure'))
    def test_fetch_failure_saved_as_failed(self, fetch, frappe):
        with self.assertRaisesRegex(ValueError, 'network failure'):
            utils._run_sync(date(2026, 1, 1), date(2026, 1, 31), settings=self.settings())
        log = frappe.get_doc.return_value.insert.return_value
        self.assertEqual(log.status, 'Failed')
        log.save.assert_called_once_with(ignore_permissions=True)

    @patch.object(utils, 'frappe')
    def test_existing_transaction_skipped_before_creation(self, frappe):
        frappe.db.get_value.return_value = 'JE-existing'
        self.assertEqual(utils._make_je(self.settings(), {'id': '123'}),
                         (None, 'duplicate: JE-existing'))
        frappe.get_doc.assert_not_called()

    @patch.object(after_install, '_ensure_active_unique_index')
    @patch.object(after_install, 'frappe')
    def test_upgrade_repairs_existing_field(self, frappe, active_index):
        frappe.db.get_value.return_value = 'existing-field'
        frappe.db.sql.return_value = []
        frappe.db.get_column_index.return_value = None
        after_install._ensure_custom_field()
        cf = frappe.get_doc.return_value
        self.assertEqual(cf.unique, 0)
        cf.save.assert_called_once_with(ignore_permissions=True)
        active_index.assert_called_once()
        audit_sql = frappe.db.sql.call_args_list[0].args[0]
        self.assertIn("WHERE docstatus != 2", audit_sql)

    @patch.object(after_install, 'frappe')
    def test_upgrade_reports_duplicates_without_deleting(self, frappe):
        frappe.db.get_value.return_value = 'existing-field'
        frappe.db.sql.return_value = [SimpleNamespace(moola_transaction_id='123')]
        frappe.throw.side_effect = ValueError('existing duplicates')
        with self.assertRaisesRegex(ValueError, 'existing duplicates'):
            after_install._ensure_custom_field()
        frappe.db.add_unique.assert_not_called()
        frappe.get_doc.assert_not_called()

    @patch.object(after_install, '_ensure_active_unique_index')
    @patch.object(after_install, 'frappe')
    def test_upgrade_removes_old_global_index(self, frappe, active_index):
        frappe.db.get_value.return_value = 'existing-field'
        frappe.db.sql.return_value = []
        frappe.db.get_column_index.side_effect = [
            SimpleNamespace(Key_name='unique_moola_transaction_id'), None]
        after_install._ensure_custom_field()
        statements = [call.args[0] for call in frappe.db.sql.call_args_list]
        self.assertIn('ALTER TABLE `tabJournal Entry` DROP INDEX `unique_moola_transaction_id`',
                      statements)

    @patch.object(after_install, 'frappe')
    def test_generated_key_creation_is_idempotent(self, frappe):
        frappe.db.has_column.return_value = False
        frappe.db.get_column_type.return_value = 'varchar(140)'
        after_install._ensure_active_unique_index()
        self.assertIn(after_install.ACTIVE_ID_EXPRESSION, frappe.db.sql.call_args.args[0])
        frappe.db.sql.reset_mock()
        frappe.db.has_column.return_value = True
        after_install._ensure_active_unique_index()
        frappe.db.sql.assert_not_called()

    @patch.object(utils, 'frappe')
    def test_lookup_excludes_cancelled_history(self, frappe):
        utils._already_posted('123')
        filters = frappe.db.get_value.call_args.args[1]
        self.assertEqual(filters['docstatus'], ['!=', 2])


class TestActiveTransactionConstraint(unittest.TestCase):
    """Exercise the generated expression on SQLite, without touching a site.

    This verifies constraint semantics, not MariaDB migration compatibility.
    """
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE entries (name TEXT PRIMARY KEY, docstatus INT, '
                        'moola_transaction_id VARCHAR(140))')
        self.db.execute('ALTER TABLE entries ADD COLUMN active_id VARCHAR(140) '
                        f'GENERATED ALWAYS AS ({after_install.ACTIVE_ID_EXPRESSION}) VIRTUAL')
        self.db.execute('CREATE UNIQUE INDEX active_id_unique ON entries (active_id)')

    def insert(self, name, status, transaction='123'):
        self.db.execute('INSERT INTO entries (name, docstatus, moola_transaction_id) '
                        'VALUES (?, ?, ?)', (name, status, transaction))

    def test_multiple_cancelled_and_one_replacement_allowed(self):
        self.insert('cancelled-1', 2)
        self.insert('cancelled-2', 2)
        self.insert('replacement', 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert('duplicate', 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 3)

    def test_cancellation_releases_id_and_preserves_history(self):
        self.insert('original', 1)
        self.db.execute("UPDATE entries SET docstatus = 2 WHERE name = 'original'")
        self.insert('replacement', 0)
        self.assertEqual(self.db.execute(
            "SELECT moola_transaction_id FROM entries WHERE name = 'original'"
        ).fetchone()[0], '123')
        self.db.execute("UPDATE entries SET docstatus = 1 WHERE name = 'replacement'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert('another', 1)

    def test_non_moola_entries_do_not_collide(self):
        self.insert('blank-1', 1, '')
        self.insert('blank-2', 1, '')
        self.insert('null-1', 1, None)
        self.insert('null-2', 1, None)

    def test_audit_ignores_cancelled_entries(self):
        self.insert('cancelled-1', 2)
        self.insert('cancelled-2', 2)
        self.insert('active', 1)
        rows = self.db.execute("""SELECT moola_transaction_id FROM entries
            WHERE docstatus != 2 AND moola_transaction_id IS NOT NULL
                AND moola_transaction_id != ''
            GROUP BY moola_transaction_id HAVING COUNT(*) > 1""").fetchall()
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
