# Operations

How the books are run day to day with numbr. For installing it, see `README.md`.

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

## Shared bank accounts

For an account you share with someone (e.g. a joint account), open Banking → Manage accounts and set **Your share %** (e.g. 50). Enter and reconcile its transactions at their full amounts, exactly as they appear on the statement. Reports then show only your share:

- Spending and income in the shared account count at your share on the P&L.
- The balance sheet shows your share of its balance.
- Money you move in from your own account, or bills you pay from it, is split; the part that belongs to the other owners shows as *Other owners' share* under Equity.

Changing the percentage changes all past reports for that account too, including locked months.

## Shared invoices and bills

For a bill or invoice you only partly own (e.g. rent split with someone else), set **Your share %** on the bill or invoice (or on its recurring template). Enter it at the full amount on the bill, and pay it in full. Your P&L shows only your share; the rest shows as *Other owners' share* under Equity (for a bill, that's what the other owners owe you back). Aged payables/receivables still show the full amount owed.

## Partner contributions

When a partner is due to put money into an account you track (e.g. their weekly share into the joint account), record it under **Partner** as a partner contribution, or set up a recurring one (Recurring → Creates a *Partner contribution*). A contribution is only a reminder: it isn't income, isn't money owed to you, and doesn't touch the P&L or balance sheet. When the money arrives, click **Mark paid** (or *Partner contribution* on an unmatched statement line when reconciling). The deposit then counts as the other owners' share: your share of the bank goes up, and *Other owners' share* under Equity goes up by the same amount, so your own net worth doesn't change.

Don't record a partner's deposit as a customer invoice: that makes it income or a receivable. If you already have, open the invoice and click **Convert to partner contribution**. That works even when its payments are reconciled (only the non-bank side of each payment changes). It's refused for opening items, for invoices in a locked period, and when one payment covers several invoices.

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
- **Off-machine:** regularly copy the `data` folder to OneDrive or a USB drive while numbr is closed. The `data` folder is deliberately **not** in git.
- **Restore:** close numbr, rename `data/books.db` aside, copy the chosen backup to `data/books.db`, then start numbr.
- **New computer:** clone this repo (or copy the folder), copy your `data` folder into it, then run `start.bat`.

## Checks if something looks wrong

- Trial balance debits = credits (always; if not, it's a bug).
- Balance sheet: Net assets = Total equity, and Historical Balancing = 0.
- Aged reports tie to the AR/AP control accounts (banner on the report).
- Bank balance on the Dashboard = the bank's balance once everything is entered and reconciled.
