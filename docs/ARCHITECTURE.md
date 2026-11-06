# Architecture

## Overview

```
Browser (static/app.js)  ──HTTP JSON──▶  app.py (http.server, 127.0.0.1:8765)  ──▶  data/books.db (SQLite)
                                                                         └──▶  data/backups/*.db
```

A single Python process serves the static frontend and a JSON API. All state is in one SQLite file. There's no other storage, no network access and no external services.

## Data model

| Table | Purpose |
|---|---|
| `settings` | Key/value: `business_name`, `conversion_date` (start date), `fy_start_month`, `lock_date`, `next_invoice_number`, `setup_done`, `last_recurring_run` |
| `accounts` | Chart of accounts. `type` ∈ asset, liability, equity, income, cos, expense. `subtype` ∈ bank, ar, ap, re, hist, other. `share_bp` = user's share of a shared bank account in basis points (10000 = 100%) |
| `contacts` | Customers/suppliers, default account |
| `journals` | One row per financial event: date, memo, reference, `source` (opening, sale, bill, receipt, payment, spend, receive, transfer, journal), `source_id`, `contact_id`, `recurring_id` |
| `journal_lines` | Debit/credit lines. `amount` in cents (+debit/−credit), `role` (bank/control), `recon_id` |
| `invoices` / `invoice_lines` | Sales invoices (`kind='sale'`) and supplier bills (`kind='bill'`). `status` open/void, `historical` flag, `journal_id` |
| `payments` / `allocations` | Customer receipts and supplier payments, and how each is split across invoices |
| `reconciliations` | Statement date and closing balance per bank account. `note='opening'` for the opening-balance pseudo-reconciliation |
| `recurring` | Templates: kind (sale, bill, spend, receive), frequency, `next_date`, `anchor_day`, `end_date`, lines as JSON |
| `audit_log` | Append-only change history |

### How documents post to the ledger

| Document | Journal lines |
|---|---|
| Sales invoice | Dr Accounts Receivable (total) / Cr each income line |
| Supplier bill | Dr each expense line / Cr Accounts Payable (total) |
| Customer payment | Dr Bank / Cr Accounts Receivable |
| Supplier payment | Dr Accounts Payable / Cr Bank |
| Spend money | Dr each line / Cr Bank |
| Receive money | Dr Bank / Cr each line |
| Transfer | Dr destination bank / Cr source bank |
| General journal | As entered (AR/AP not allowed) |
| Opening balances | One journal dated the day before the start date; any difference goes to Historical Balancing |

Invoices and bills dated before the start date (opening items) post nothing; see invariant 5 in `CLAUDE.md`.

### Reports

All computed on request from `journal_lines` joined to `journals` and `accounts`:

- **P&L:** income/cos/expense lines grouped by `substr(date,1,7)` for the chosen months
- **Balance sheet:** balances as at each month end. Retained earnings = RE account + all profit before the current financial-year start. Current Year Earnings = profit from FY start to the date
- **Aged AR/AP:** open invoices, balance = total − allocations from payments dated on or before the as-at date, bucketed by days past due. The report also returns the control account balance so the UI can show whether they tie
- **Trial balance:** debit/credit per account

### Shared bank accounts

A bank account or credit card can be shared (e.g. a joint account), with `accounts.share_bp` < 10000 giving the user's share. The ledger always stores full amounts, so the account still reconciles to the real statement. All reports (P&L, balance sheet, trial balance, dashboard profit, and the "Your share" column in registers) read lines through `report_lines()`, which cuts them to the user's share per journal (`share_journal()`):

- lines in a shared bank account are cut to that account's share;
- if every bank line in the journal is in one shared account and the journal isn't the opening balances (a spend, receive, payment or journal from that account), its other lines are cut by the same share, except AR/AP, which stay full so the aged reports tie;
- every other line stays full (e.g. the other side of a transfer from an unshared account).

Each line is rounded to the cent on its own. Whatever no longer balances belongs to the other owners and is posted to a pseudo account (`PARTNER`, id 0), shown as the computed equity line "Other owners' share of shared accounts" on the balance sheet and trial balance, so both still balance. Changing the share re-states all history, including locked periods.

## API reference

All endpoints are under `/api`, take and return JSON, and require header `X-Ledger: 1`. Money is integer cents.

| Method & path | Purpose |
|---|---|
| `GET /state` | Settings, accounts (with balances), contacts; runs due recurring once per day |
| `PUT /settings` | Update settings (business name, FY month, invoice number, start date, lock date, setup_done) |
| `GET/PUT /opening` | Read or replace opening balances (`{balances: {account_id: natural_cents}}`) |
| `GET/POST /accounts`, `PUT/DELETE /accounts/:id` | Chart of accounts |
| `GET/POST /contacts`, `PUT/DELETE /contacts/:id` | Contacts |
| `GET /invoices?kind=&status=&contact_id=` | List (status: open, overdue, paid, void, all) |
| `GET/PUT /invoices/:id`, `POST /invoices`, `POST /invoices/:id/void` | Invoices and bills |
| `GET/POST /payments`, `GET/DELETE /payments/:id` | Receipts and payments with allocations |
| `POST /txn/:source` (spend, receive, transfer, journal) | Create a bank transaction or journal |
| `GET /journals?source=&from=&to=`, `GET/PUT/DELETE /journals/:id` | List, edit and delete those transactions |
| `GET /register?account_id=&from=&to=` | Account transactions with running balance; when shared accounts affect it, also `share`/`share_balance` per line and `share_opening`/`share_closing` |
| `GET /reconcile?account_id=`, `POST /reconcile`, `POST /reconcile/undo` | Bank reconciliation |
| `GET/POST /recurring`, `GET/PUT/DELETE /recurring/:id`, `POST /recurring/:id/skip`, `POST /recurring/run` | Recurring templates |
| `GET /reports/pl?from=YYYY-MM&to=YYYY-MM` | Profit & Loss by month |
| `GET /reports/bs?to=YYYY-MM&count=N` | Balance sheet, N month-end columns |
| `GET /reports/aged?kind=sale\|bill&as_at=` | Aged receivables/payables |
| `GET /reports/tb?as_at=` | Trial balance |
| `GET /dashboard`, `GET /audit`, `POST /backup` | Dashboard figures, audit log, manual backup |

## Design decisions

- **Standard library only.** The app has to start on a fresh Windows machine with just Python and survive years without dependency maintenance.
- **Integer cents.** No floating-point drift in totals.
- **No stored balances.** Reports can't disagree with the ledger, and back-dated corrections flow through automatically (until the period is locked).
- **Opening items rather than migrating MYOB history.** You start clean from a date, which avoids a lossy import.
- **CSV import is client-side and non-destructive.** Statement lines are only used to tick matches and to prefill new transactions; they're never stored.
- **Localhost only** with a custom-header check, so other websites in the same browser can't call the API.
