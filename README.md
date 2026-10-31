# Ledger

Simple bookkeeping that runs on your own computer. Your books never leave your machine.

## Getting started (Windows)

1. Install Python 3.10 or newer from python.org (tick **Add python.exe to PATH** during install). One-time only.
2. Put this `Ledger` folder somewhere permanent, e.g. `Documents\Ledger`.
3. Double-click **start.bat**. A small black window opens and Ledger opens in your browser at http://localhost:8765.
4. Leave the black window open while you work. Close it when you're done.

Tip: right-click `start.bat` → Send to → Desktop (create shortcut) for a desktop icon.

## First-time setup

The setup wizard walks you through:

1. **Business details**: name, start date, financial year, next invoice number.
2. **Bank accounts**: rename/add your bank and credit card accounts.
3. **Opening balances**: from your MYOB balance sheet as at the day before your start date.
4. **Unpaid invoices and bills**: each one still open at your start date, with its original date. These must add up to your opening Accounts Receivable / Payable.

## Weekly routine

- **Sales / Purchases**: enter invoices and bills; record payments with *Receive payment* / *Pay bills*.
- **Banking**: *Spend money* / *Receive money* for anything not on an invoice or bill (fees, direct debits, interest).
- **Reconcile**: pick the account, enter the statement date and closing balance, tick what's on the statement until *Out by* is $0.00, then click Reconcile. Optionally *Import statement CSV* from your bank to auto-match and spot anything missing.
- **Recurring**: rent, subscriptions, retainers and the like are created automatically on their date whenever Ledger is open.

## Month end

- Run **Profit & Loss** and **Balance Sheet** (export to CSV or Print → Save as PDF).
- Check **Aged Receivables / Payables** tie to the balance sheet (the report tells you).
- **Settings → Lock period** to protect the month you've reported on.

## Your data and backups

- Everything is in `data\books.db` inside this folder.
- A copy is saved to `data\backups` every time Ledger starts (latest 40 kept), plus *Settings → Back up now*.
- Also copy the `data` folder somewhere else regularly (OneDrive, USB drive).
- To move to a new computer, copy the whole `Ledger` folder.

## Notes

- Not set up for GST. Payroll, BAS lodgement and bank feeds aren't included.
- To fix a reconciled transaction, undo the reconciliation first (Reconcile → history → Undo).
- To change a paid invoice, delete the payment first, then edit or void the invoice.
- If port 8765 is busy, set the environment variable `LEDGER_PORT` to another number.

## Development

- `CLAUDE.md`: how the app works, the accounting rules it enforces, and conventions for changing it (read this first, whether you or Claude is making changes)
- `docs/ARCHITECTURE.md`: data model, API reference, design decisions
- `docs/OPERATIONS.md`: weekly and month-end routine, fixing mistakes, backups and recovery
- Tests: `python tests/test_api.py` (backend) and `python tests/test_ui.py` (browser end-to-end; needs `pip install playwright && playwright install chromium`)
