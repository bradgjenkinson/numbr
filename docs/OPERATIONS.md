# Operations

How the books are run day to day with Ledger. For installing it, see `README.md`.

## Weekly

1. **Enter what's happened:** new invoices (Sales → New invoice), supplier bills (Purchases → New bill), customer payments (Receive payment), bill payments (Pay bills).
2. **Reconcile each bank account and credit card** (Reconcile):
   - Choose the account, set the statement date, and type the closing balance from the bank (for a credit card, the amount owed).
   - Optionally *Import statement CSV* from internet banking. Matching lines (same amount, within 7 days) are ticked automatically, and statement lines with no match are listed.
   - For each unmatched line:
     - customer money in → **Customer payment**
     - supplier payment out → **Bill payment**
     - anything else (fees, interest, direct debits) → **Spend/Receive money**
   - When *Out by* shows $0.00, click **Reconcile**.
3. Check the Dashboard for overdue invoices and anything recurring that was skipped.

## Month end

1. Finish the last weekly reconciliation up to the bank statement's month-end date.
2. Reports:
   - **Profit & Loss** for the month (or the financial year to date): Export CSV or Print → Save as PDF.
   - **Balance Sheet** as at month end (Compare: 13 months gives year on year).
   - **Aged Receivables / Aged Payables.** The green banner confirms they agree with the balance sheet.
3. Optional adjustments (General Journal): depreciation, owner drawings or contributions, corrections.
4. **Settings → Lock to [last month end]** so the reported month can't change.

## Year end (financial year starts in July by default)

Nothing needs closing. Retained earnings roll over automatically on 1 July. Give your accountant the P&L for the year, the balance sheet at 30 June, the aged reports and the trial balance.

## Fixing mistakes

| Situation | Fix |
|---|---|
| Wrong unpaid invoice/bill | Edit it, or Void it |
| Wrong invoice that's already paid | Open the payment → Delete payment, then edit/void the invoice and re-enter the payment |
| Wrong bank transaction that's reconciled | Reconcile → history → Undo (latest only), fix it, then reconcile again |
| Anything in a locked month | Settings → move the lock date back, fix it, then re-lock (the change is recorded in the audit log) |
| Opening balances wrong after reconciling | Undo reconciliations back to the opening one, then edit opening balances |

## Backups and recovery

- **Automatic:** each start copies `data/books.db` to `data/backups/books-YYYYMMDD-HHMMSS-startup.db` (latest 40 kept).
- **Manual:** Settings → Back up now.
- **Off-machine:** regularly copy the `data` folder to OneDrive or a USB drive while Ledger is closed. The `data` folder is deliberately **not** in git.
- **Restore:** close Ledger, rename `data/books.db` aside, copy the chosen backup to `data/books.db`, then start Ledger.
- **New computer:** clone this repo (or copy the folder), copy your `data` folder into it, then run `start.bat`.

## Checks if something looks wrong

- Trial balance debits = credits (always; if not, it's a bug).
- Balance sheet: Net assets = Total equity, and Historical Balancing = 0.
- Aged reports tie to the AR/AP control accounts (banner on the report).
- Bank balance on the Dashboard = the bank's balance once everything is entered and reconciled.
