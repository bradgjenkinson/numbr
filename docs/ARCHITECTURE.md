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
| `accounts` | Chart of accounts. `type` ∈ asset, liability, equity, income, cos, expense. `subtype` ∈ bank, ar, ap, re, hist, partner, other. `share_bp` = user's share of a shared bank account in basis points (10000 = 100%). `csv_map` = JSON column choices from the last statement CSV import into a bank account (each column by position and header name) |
| `contacts` | Customers/suppliers, default account |
| `journals` | One row per financial event: date, memo, reference, `source` (opening, sale, bill, receipt, payment, spend, receive, transfer, journal), `source_id`, `contact_id`, `recurring_id`; `rule_id`/`rule_mode` (auto, recommend) when a bank rule created it, `reviewed`=1 once a recommended one is edited or reconciled |
| `journal_lines` | Debit/credit lines. `amount` in cents (+debit/−credit), `role` (bank/control), `recon_id`, `bank_text` (the statement description an imported/matched bank line came from) and `bank_date` (that statement line's date, which can differ from the transaction date); together with the amount, the duplicate-check key |
| `bank_rules` | Rules applied to imported statement lines: `priority` (first match wins), `mode` (auto, recommend), text condition (`text_op` contains/equals + `text_tokens` JSON), `direction`, amount condition (`amount_op` eq/lt/gt on the size of the amount), `account_ids` JSON (NULL = all bank accounts), `target_account_id` (a category, or another bank account for a transfer rule), optional `contact_id`/`memo`, `active`, `archived` |
| `invoices` / `invoice_lines` | Sales invoices (`kind='sale'`) and supplier bills (`kind='bill'`). `status` open/void, `historical` flag, `journal_id`, `share_bp` = user's share of a shared invoice/bill (10000 = 100%), `partner` = 1 for a partner contribution |
| `payments` / `allocations` | Customer receipts and supplier payments, and how each is split across invoices |
| `reconciliations` | Statement date and closing balance per bank account. `note='opening'` for the opening-balance pseudo-reconciliation |
| `recurring` | Templates: kind (sale, bill, spend, receive; `partner`=1 makes contributions), frequency, `next_date`, `anchor_day`, `end_date`, lines as JSON, `share_bp` copied to each invoice/bill it creates |
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

### Shared bank accounts and shared invoices/bills

A bank account or credit card can be shared (e.g. a joint account), with `accounts.share_bp` < 10000 giving the user's share. The ledger always stores full amounts, so the account still reconciles to the real statement. All reports (P&L, balance sheet, trial balance, dashboard profit, and the "Your share" column in registers) read lines through `report_lines()`, which cuts them to the user's share per journal (`share_journal()`):

- lines in a shared bank account are cut to that account's share;
- if every bank line in the journal is in one shared account and the journal isn't the opening balances (a spend, receive, payment or journal from that account), its other lines are cut by the same share, except AR/AP, which stay full so the aged reports tie;
- every other line stays full (e.g. the other side of a transfer from an unshared account).

An invoice or bill can also be shared (e.g. rent split with someone else): `invoices.share_bp` < 10000. Its journal still posts the full amount, and AR/AP stay full (the whole amount is owed), but `report_lines()` cuts its income/expense lines to the user's share (`shared_documents()` supplies the share per journal). Payments against it are normal payments. A recurring sale/bill template carries its own `share_bp`, which each generated invoice/bill copies. The API takes the share as `share_pct` (e.g. 50) on `POST/PUT /invoices` and `/recurring`; leaving it out of a `PUT /invoices` keeps the current share.

Each line is rounded to the cent on its own. Whatever no longer balances belongs to the other owners and is posted to a pseudo account (`PARTNER`, id 0), shown as the computed equity line "Other owners' share" on the balance sheet and trial balance, so both still balance. Changing the share re-states all history, including locked periods.

### Partner contributions

Money a partner is due to put into an account the user tracks (e.g. a joint account) is a *partner contribution*: an `invoices` row with `kind='sale'` and `partner=1`. It posts **no journal**, so it never touches the P&L, balance sheet or aged receivables (all of those filter `partner=0`). It has one `invoice_lines` row in the `partner` system account (equity, "Partner Contributions", created by `init_db()`). Paying it is a normal receipt (`POST /payments`), but the payment credits the partner account instead of AR; a payment can't mix contributions and invoices. `report_lines()` never cuts partner-account lines and reports them as `PARTNER`, so a deposit into a 50% joint account shows as +half on the bank and +half on "Other owners' share". In the API, `kind=partner` is accepted by `GET /invoices`, `POST /invoices` (with `amount` and optional `description` instead of `lines`) and `POST/PUT /recurring` (stored as `kind='sale'`, `partner=1`; read back as `kind='partner'`). `GET /payments?kind=contribution` lists contribution receipts.

`POST /invoices/:id/to-partner` converts an existing customer invoice: it deletes the invoice's journal, replaces its lines with one partner-account line, and moves each payment's `role='control'` line from AR to the partner account. Bank lines aren't touched, so it works on reconciled payments. It refuses opening items (they're inside the opening AR balance), locked dates, and payments allocated to more than one invoice.

### Bank rules and statement import

The Reconcile screen parses a bank CSV in the browser, then sends the lines to `POST /reconcile/import` with the bank account the user picked. For each line, in order:

1. **Duplicate.** Same account, date (`bank_date`, else the transaction date), amount and statement text (case and spacing ignored) as an existing line's `bank_text` → skipped. Counted, so two identical lines in the file against one already stored gives one duplicate and one new line. The result names the existing line so the screen can still tick it if it isn't reconciled.
2. **Rule.** The first active rule (by `priority`) whose account scope, direction, amount and text all match creates an ordinary `spend`/`receive` journal (bank line carries `bank_text`; the other line goes to the rule's account), tagged `rule_id`/`rule_mode`. Rules win over items the user entered by hand: an unreconciled hand-entered line with the same amount within 7 days is returned as `possible_dup_line_id` and the screen warns. Locked dates are reported as `locked`; other validation errors as `error` (per-line SAVEPOINT).
   A **transfer rule** (target is a bank account) creates a `transfer` journal instead: money out of the imported account goes to the target, money in comes from it. It never applies when importing the target account itself. If the other side is already there (an unreconciled transfer line in this account to/from the target, same amount, within 7 days, with no `bank_text`, e.g. made by the same rule when the other account's statement was imported), the rule claims that line (stores `bank_text`/`bank_date`) and reports it as a `duplicate`, so the transfer is never created twice.
3. **None.** The screen matches it to an unreconciled item (same amount, within 7 days) or lists it as unmatched.

`auto` lines are ticked on the Reconcile screen; `recommend` lines aren't, until reviewed. Reconciling still needs the statement closing balance, so a wrong rule can't silently reconcile. On reconcile, the CSV text of matched hand-entered lines is saved to `bank_text` so the next import skips them.

The rule builder stores pattern *parts* (`text`, `number`, `letters`, `date` with a format like `DD/MM/YYYY`, `any`); `compile_rule_regex()` turns them into a regex (text escaped, parts joined by optional whitespace, `equals` anchored, case-insensitive). Users never write regex.

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
| `GET /reconcile?account_id=`, `POST /reconcile` (optional `bank_text: {line_id: text or {text, date}}`), `POST /reconcile/undo` | Bank reconciliation |
| `POST /reconcile/import` | Statement lines for one account: skip duplicates, apply bank rules (`{account_id, lines: [{date, amount, description}], mapping?}`; `mapping` = the column choices, saved on the account and returned as `csv_map` in `/state`) |
| `GET/POST /rules`, `GET/PUT/DELETE /rules/:id`, `POST /rules/reorder`, `POST /rules/test` | Bank rules; delete archives a rule that has created transactions; test returns the regex, a sample match and recent matching lines |
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
- **CSV parsing is client-side; only the description is kept.** The file is parsed in the browser. Statement text is stored only as `journal_lines.bank_text` on lines that rules created or that were matched, so re-imports can skip them. Bank rules create ordinary spend/receive journals, so every report and invariant treats them like hand-entered ones.
- **Localhost only** with a custom-header check, so other websites in the same browser can't call the API.
