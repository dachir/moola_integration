## Moola Integration

Moola expense integration for ERPNext. Scheduled and manual date-range imports use
one sync engine and identify expenses by the remote `id`, saved on Journal Entry
as `moola_transaction_id`.

### Sync behavior

- The first automatic sync honors Backfill From Date. Without one, it starts at
  the configured look-back window.
- Subsequent automatic runs revisit the look-back window, or resume from an older
  last-success date after an outage. Backfill From Date remains the lower bound.
- Manual date-range imports preserve the automatic cursor by default. Explicit
  cursor advancement is capped at the imported range end and never moves backward.
- Failed expenses roll back before processing continues. Partial runs preserve
  the cursor; completed expenses can safely be skipped on retry.
- A database unique constraint protects transaction IDs on noncancelled entries
  even when jobs overlap. Cancelled entries retain their IDs and allow a replacement.
  Expenses approved or changed outside the look-back window require a manual
  range import or a larger look-back setting.

### Upgrade an existing site

Deploy this app version, then run from the live bench (replace `SITE`):

```sh
bench --site SITE migrate
bench build --app moola_integration
bench restart
```

The migration repairs the existing custom field and its unique index. If multiple
noncancelled Journal Entries already share a nonempty transaction ID, migration stops and
lists sample IDs. Review those entries before retrying; the patch does not delete
or cancel Journal Entries. Multiple cancelled entries with the same ID are allowed,
as is one draft or submitted replacement. The MariaDB constraint uses a generated
column, automatically releasing the active ID when an entry is cancelled.
Legacy drafts from failed imports also require review: existing noncancelled
Journal Entries are deliberately treated as already imported.

After migration, run a manual import for the missing period with cursor advancement
disabled. Check Moola Sync Log for errors, then repeat the same period: it should
create no additional Journal Entries for previously imported transaction IDs.
Duplicate protection assumes Moola returns a stable `id` for each expense.
The generated-column index follows [MariaDB generated column support](https://mariadb.com/docs/server/reference/sql-statements/data-definition/create/generated-columns).

### Local regression checks

Using the bench Python environment, from the app directory:

```sh
../../env/bin/python -m unittest discover -s tests -v
```

These tests mock database and remote API calls; they do not perform a live sync.

#### License

MIT
