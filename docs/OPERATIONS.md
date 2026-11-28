# Operations

How the books are run day to day with numbr. For installing it, see `README.md`.

## Weekly

1. **Enter what's happened:** new invoices (Sales → New invoice), supplier bills (Purchases → New bill), customer payments (Receive payment), bill payments (Pay bills).
2. **Reconcile each bank account and credit card** (Reconcile):
   - Choose the account, set the statement date, and type the closing balance from the bank (for a credit card, the amount owed).
   - Optionally *Import statement CSV* from internet banking and confirm which account it's for. The column choices are remembered per account, so after the first import you normally just click Import. Then:
     - lines already in numbr (same date, amount and text) are skipped, so overlapping statements are safe to import;
     - **bank rules** categorise what they recognise. *Auto-apply* rules tick the line; *Recommend* rules list it under **To review**, so check the category (**Change** if needed) and tick it;
     - other lines matching an existing item (same amount, within 7 days) are ticked, and the rest are listed as unmatched.
   - If you see a *Possible double-up* warning, a rule created a transaction you'd also entered by hand. Delete one.
   - For each unmatched line:
     - customer money in → **Customer payment**
     - supplier payment out → **Bill payment**
     - money moved to or from another of your accounts → **Transfer** (when you import that account's statement, its side is matched)
     - a partner's share arriving → **Partner contribution**
     - anything else (fees, interest, direct debits) → **Spend/Receive money**, or **Make a rule** so it's handled next time (then **Apply rules again**)
   - When *Out by* shows $0.00, click **Reconcile**.
3. Check Aged Receivables for overdue invoices, and the Audit log for any recurring transaction that was skipped (numbr also shows a red message when one is).

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

## Budgets

**Budgets → Build a budget** walks you through it: pick the financial year, then for each income and expense account choose how to budget it. Each account starts with a suggestion: the method you used last year, otherwise its monthly average if it has history.

- **Monthly average** or **Last month**: from your last 12 full months of actuals.
- **My own amounts**: amounts that repeat, e.g. $100 a month from July to October, then $110 a month from November; or $50 a week starting on a given date (weeks and fortnights land in the month they fall in); or $300 every quarter from a start month (with an end month, or leave it to continue); or $199 once a year in March for an annual subscription (next year's budget puts it in March again); or a total for the year spread evenly across the months, or by quarter between two months (e.g. $1,200 from July to June is $300 in July, October, January and April).
- **Type each month**: all 12 months by hand.
- **Don't budget**.

Once built, change any figure on the budget page by typing over it and **Save changes**. Those edits don't change how next year's builder starts: it reuses the choices you made when building (moved forward a year). To change the choices themselves, delete the budget and build it again.

To rework a budget with the builder's methods (e.g. switch an account to a quarterly amount, or refresh the monthly averages), click **Rebuild with builder** on the budget page. Each account starts with the method it was built with; accounts you changed by hand are marked "edited by hand" and start as typed months holding your figures, so they stay as they are unless you pick another method. **Update budget** replaces the budget's figures and the choices next year's builder starts from.

Near the end of each financial year, build next year's budget: each account starts the way you did it this year (marked "as last year"), so just adjust anything that's changed.

## Projects

To see how one part of your affairs is going (say an investment property), make a project for it: **Chart of Accounts → Projects → New project**, name it, and tick the accounts that belong to it (e.g. Rent Income, Property Expenses, the property and its loan). You can also tick projects when you open an account. An account can be in more than one project, e.g. Insurance if one policy covers two properties.

On the **Profit & Loss** or **Balance Sheet**, choose the project in the **Project** box to see only its accounts; choose **All accounts** to go back. Budget vs actual works the same way. The project balance sheet shows the project's assets and loans plus "Project earnings" (its profit). It won't balance on its own, because the rent and bills go through your bank account, which usually isn't in the project. Projects never change any figures; deleting one leaves the accounts and transactions alone.

## Dashboard

- Press **Customise** to choose what the Dashboard shows, then **Done** when you're finished.
  - Add a visual from the list.
  - Move cards and tiles with the arrows.
  - Change a card's settings with ⚙.
  - Remove a card with ×.
- **Account tiles** sit along the top, and you can add as many as you like. Each tile shows one account or a whole group (for example "Tax savings" or "Vehicle costs"), with its trend over 12 months.
- **Spending calendars** can be added more than once:
  - one for all spending
  - one for money out of a chosen bank account or card, with transfers between your own accounts left out
- **Profit forecast**, **Profit against budget** and **Budget used so far** need a budget for this financial year. The forecast starts from your profit so far and adds each later month's budgeted profit.
- Some cards have buttons to switch views. **Budget used so far** switches between so far and the whole year, and between sorting by % of budget used and by dollars spent (most first). **This year vs last year** switches between year and month, and between spending and income.
- **Budget used so far** and **This year vs last year** have a **Choose** button (it says **Change** once set). Show the default rows, the default rows less the accounts or groups you leave out, or only the ones you pick. This year vs last year keeps separate choices for spending and for income. **Coming up** switches between 14, 30 and 60 days.

## Alerts

**Alerts → New alert** tells numbr to keep an eye on a figure for you. Pick an example to start from, or fill in the three steps: a name, the account (or accounts, added together) to watch, and when it should go off, compared with a dollar amount or with other accounts (plus or minus an amount). For example: *Offset A plus Offset B goes below Home Loan minus $1,000*. The **Right now** box shows today's figures and whether the alert would go off as you type.

- Bank, loan and other balance sheet accounts use today's balance; income and expense accounts use the total for this financial year so far. Shared bank accounts count at their full balance, the same as the bank statement.
- The red number next to **Alerts** in the menu is how many alerts are going off; they're also listed at the top of the Dashboard.
- Click **Got it** to mark one as seen. It stays in the count and on the Dashboard until the figures change so the condition is no longer true, so you can't forget it. Untick **Turned on** to pause an alert without deleting it.

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

## Bank rules

**Bank rules** (in the menu, or from Banking) categorise statement lines when you import a CSV. A rule checks the bank text (contains / is exactly a pattern built from text, a number, letters, a date or "anything"), and/or the amount (money in or out; equal to, less than or more than a figure), in all bank accounts or only some. It then either categorises the line, or (Action: *Transfer to/from another bank account*) records a transfer to or from another of your accounts. When you later import that other account's statement, its side of the transfer is recognised as already in numbr and ticked, not entered twice. Use **Try it** to test the pattern against a line from your statement. Rules run top to bottom and the first match wins, so put specific rules (e.g. *Bean Cafe under $10 → coffee*) above general ones (*Bean Cafe → meals*).

In Banking and on the Reconcile screen, every transaction shows the account(s) it was put to (its category) under the description. Transactions a rule made also show a badge with the rule's name: **Auto** (applied and ticked) or **Review** (recommended, not yet checked; it becomes *Suggested* once you edit or reconcile it). Use *Show* to list only rule transactions, and click a badge to open the rule. If a rule put something in the wrong category, change the transaction and fix the rule. Deleting a rule keeps the transactions it made.

## Fixing mistakes

| Situation | Fix |
|---|---|
| Wrong unpaid invoice/bill | Edit it, or Void it |
| Wrong invoice that's already paid | Open the payment → Delete payment, then edit/void the invoice and re-enter the payment |
| Reconciled bank transaction in the wrong category | Open it and change the account (or split it) and save. The bank account, date and total stay fixed, and it stays reconciled |
| Wrong amount, date or bank account on a reconciled transaction | Reconcile → history → Undo (latest only), fix it, then reconcile again |
| Anything in a locked month | Settings → move the lock date back, fix it, then re-lock (the change is recorded in the audit log) |
| Opening balance wrong for a reconciled account | Undo that account's reconciliations back to the opening one, then edit it. Accounts you haven't reconciled can be edited any time |

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
- A bank account's tile on the Dashboard = the bank's balance once everything is entered and reconciled (a shared account's tile shows your share).
