# CLAUDE.md

Guidance for Claude (and anyone else) working on this repository.

## What this is

**numbr** is Brad's self-built replacement for desktop MYOB: a single-user bookkeeping app that runs locally on Windows and opens in the browser. Scope is deliberately small:

- Weekly manual bank reconciliation (with optional bank CSV import and auto-matching)
- Accounts receivable / payable (invoices, bills, part payments, aged reports)
- Recurring sales, bills and bank transactions
- Monthly Profit & Loss and Balance Sheet

Out of scope (on purpose): GST/BAS, payroll/STP, live bank feeds, multi-currency, multi-user. The business is **not GST-registered**. Don't add tax codes unless asked.

## Stack and constraints

- **Backend:** `app.py`, a single file using only the Python standard library (`http.server`, `sqlite3`). **No third-party packages.** The user installs plain Python and double-clicks `start.bat`; keep it that way.
- **Frontend:** `static/index.html`, `static/app.css`, `static/app.js`. Plain JavaScript, no framework, no build step, no CDN (the app must work offline).
- **Database:** SQLite at `data/books.db` (override with env `LEDGER_DATA`). Schema is created in `SCHEMA` in `app.py` with `CREATE TABLE IF NOT EXISTS`.
- **Platform:** Windows first (`start.bat`). It also runs anywhere with `python app.py`.
- Server binds to `127.0.0.1:8765` (override with env `LEDGER_PORT`). API requests must send header `X-Ledger: 1` and a localhost `Host`. That's the CSRF/DNS-rebinding guard; keep it.

## Run and test

```bash
python app.py                 # starts server, opens browser (add --no-browser to skip)
python tests/test_api.py      # backend scenario test against a temp DB; exits non-zero on failure
python tests/test_ui.py       # end-to-end UI test in headless Chromium
                              #   needs: pip install playwright && playwright install chromium
```

Run **both** tests after any change to accounting logic, and `node --check static/app.js` after JS edits. Tests use a throwaway data dir and ports 8798/8799, so they never touch real books.

## Accounting model (read before changing anything financial)

Double-entry ledger. **Every financial event becomes one `journals` row with two or more `journal_lines` that sum to exactly zero.**

- Amounts are **integer cents**. Positive = debit, negative = credit. Never use floats for money. The frontend parses and formats with `parseMoney`/`fmt` and sends cents.
- "Natural" balance = `SIGN[type] * sum(amount)` where assets/expenses/cost of sales are +1 and liabilities/equity/income are -1. The API shows natural balances; the ledger stores raw signed cents.
- **Reports are never stored.** P&L, balance sheet, aged reports and bank balances are all computed from `journal_lines` on request. Don't add cached balance columns.
- Retained earnings and Current Year Earnings are **computed** from the financial-year start (`fy_start_month`, default 7 = July). There are no year-end closing entries.
- System accounts are found by `accounts.subtype`: `ar`, `ap`, `re` (retained earnings), `hist` (historical balancing), `partner` (partner contributions). `bank` marks reconcilable accounts (bank and credit cards). AR/AP may only be touched by invoices, bills and payments, never by journals or spend/receive lines, so the aged reports always tie to the balance sheet.
- Each `journal_lines.role` is `'bank'`, `'control'` or NULL. It's used to rebuild editable forms from a journal.
- **Shared bank accounts:** `accounts.share_bp` < 10000 marks a bank account the user only partly owns. The ledger keeps full amounts (reconciliation is unchanged); reports must read lines through `report_lines()`, which cuts them to the user's share and sends the remainder to the computed "Other owners' share" equity line (`PARTNER`). Don't query `journal_lines` directly for a report. See `docs/ARCHITECTURE.md`.
- **Partner contributions:** `invoices.partner=1` (kind `'sale'`) is money a partner is due to put into an account the user tracks. It posts no journal; its payments credit the `partner` system account, which `report_lines()` never cuts and reports as `PARTNER`. Never count it as income or AR: queries for invoices, aged reports and dashboard totals must filter on `partner`.
- **Shared invoices/bills:** `invoices.share_bp` < 10000 (and `recurring.share_bp` for the invoices a template creates) cuts that invoice's non-AR/AP lines to the user's share in `report_lines()` the same way; the remainder also goes to `PARTNER`.

### Invariants the code enforces (keep them)

1. Journals balance (`check_lines`).
2. **Lock date:** nothing dated on or before `settings.lock_date` can be created, edited or deleted (`check_lock`). It defaults to the day before the start date.
3. **Reconciled lines are immutable.** A journal with any line where `recon_id` is set can't be edited or deleted. The user must undo the reconciliation (only the latest one can be undone).
4. **Paid invoices are immutable.** Delete the payment(s) first, then edit or void.
5. **Opening items:** invoices and bills dated before `settings.conversion_date` are `historical=1`. They post **no journal**, because the opening AR/AP balance already includes them, but they appear in aged reports and accept payments (payments always post).
6. Opening balances are one `source='opening'` journal dated the day before the start date. Any imbalance goes to Historical Balancing. Opening bank lines are pre-reconciled via `reconciliations.note='opening'`. Opening balances lock once any real reconciliation exists.
7. Every request runs in one SQLite transaction (`BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK` in `Handler.handle_api`). Raise `ApiError` for user-facing validation; it rolls back and returns `{error}` with status 400.
8. Every create/edit/delete writes an `audit_log` row.

### Recurring transactions

`run_recurring()` runs on startup, on the first `/api/state` call each day, and after saving a template. It creates everything due up to today. Each item runs inside a SAVEPOINT; a failure such as a locked period is logged as `recurring_skipped` and the schedule still advances. Monthly schedules use `anchor_day` (31 = always month end).

## Code map

- `app.py`
  - helpers (dates, cents, validation), `connect`/`init_db`/`backup`
  - `post_journal`, `check_lines`, `assert_journal_editable`: the core ledger writes
  - routes are declared with `@route('GET', '/api/...')`; handlers receive `(db, query, body, **path_params)`
  - sections: settings/opening → accounts/contacts → invoices → payments → banking txns → register → reconcile → recurring → reports → dashboard → HTTP server
- `static/app.js`
  - utilities (`api`, `fmt`, `parseMoney`, `modal`, `toast`), `lineEditor`, `journalEditor`
  - `ROUTES` hash router → one async function per screen (`dashboard`, `invoiceForm`, `reconcilePage`, `reportPL`, …)
  - global `S` holds settings, accounts and contacts (refresh with `refreshState()`)
  - CSV statement parsing and matching are client-side (`parseCSV`, `csvMapping`, `drawCsv` inside `reconcilePage`)
- `docs/ARCHITECTURE.md`: schema, API reference, design decisions
- `docs/OPERATIONS.md`: how the user runs the books (weekly, month end, backups, recovery)

## Conventions

- Keep the zero-dependency, no-build-step setup. If something seems to need a library, ask first.
- Schema changes: add columns with a guarded `ALTER TABLE` in `init_db()` (check `PRAGMA table_info` first) so existing `books.db` files upgrade in place. **Never** drop or rewrite user data in a migration. A backup is taken on every startup, but don't rely on it.
- UI copy is plain Australian English (dates DD/MM/YYYY via `dfmt`). Error messages should tell the user what to do next.
- New screens: add a `ROUTES` entry plus a nav link in `index.html`, and use `page(title, sub, actions, body)`.
- Extend `tests/test_api.py` for every new accounting rule, including a "TB still balances / BS still balances" check.

## Never commit

`data/` (the real books and backups) is in `.gitignore`. It holds the business's financial records. Don't remove that rule or add sample data built from real figures.

## Ideas not yet built

- Email or PDF invoices (print-to-PDF works today)
- Credit notes (current workaround: void and reissue)
- Saved bank-CSV column mappings per account
- Budget vs actual on the P&L
