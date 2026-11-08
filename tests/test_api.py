"""Backend scenario test: runs the app against a temporary database and checks the books tie out.
Run: python tests/test_api.py"""
import json, os, sqlite3, subprocess, sys, time, urllib.request, tempfile, shutil, calendar
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'app.py')
data = tempfile.mkdtemp()
env = dict(os.environ, LEDGER_DATA=data, LEDGER_PORT='8799')
p = subprocess.Popen([sys.executable, APP, '--no-browser'], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(1.2)
BASE = "http://127.0.0.1:8799"


def monthly_dates(start, day):
    """Dates a monthly schedule starting at `start` (YYYY-MM-DD) falls due up to today.
    day=31 means month end. Keeps the recurring checks right whatever day the tests run."""
    y, m = int(start[:4]), int(start[5:7])
    today, out = date.today().isoformat(), []
    while True:
        d = f'{y:04d}-{m:02d}-{min(day, calendar.monthrange(y, m)[1]):02d}'
        if d > today:
            return out
        out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def call(method, path, body=None, expect_ok=True):
    req = urllib.request.Request(BASE + path, method=method, headers={'X-Ledger': '1', 'Content-Type': 'application/json'},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        res = json.loads(e.read())
        if expect_ok:
            raise AssertionError(f'{method} {path}: {res}')
        return res


fails = 0
def check(cond, msg):
    global fails
    print(('PASS ' if cond else 'FAIL ') + msg)
    if not cond: fails += 1

try:
    st = call('GET', '/api/state')
    banks = [a['name'] for a in st['accounts'] if a['subtype'] == 'bank']
    check(banks == ['Bank 1'], f'new books start with one bank account called Bank 1 {banks}')
    # add a second bank account and a credit card
    call('POST', '/api/accounts', {'code': '1-1200', 'name': 'Bank 2', 'type': 'asset', 'subtype': 'bank'})
    call('POST', '/api/accounts', {'code': '2-1100', 'name': 'Credit Card 1', 'type': 'liability', 'subtype': 'bank'})
    st = call('GET', '/api/state')
    A = {a['code']: a['id'] for a in st['accounts']}
    check(len([a for a in st['accounts'] if a['subtype'] == 'bank']) == 3, 'bank accounts can be added')
    call('PUT', f"/api/accounts/{A['1-1200']}", {'name': 'Savings'})
    names = {a['id']: a['name'] for a in call('GET', '/api/accounts')}
    check(names[A['1-1200']] == 'Savings', 'bank account can be renamed')
    spare = call('POST', '/api/accounts', {'code': '1-1150', 'name': 'Spare', 'type': 'asset', 'subtype': 'bank'})['id']
    call('DELETE', f'/api/accounts/{spare}')
    check(spare not in [a['id'] for a in call('GET', '/api/accounts')], 'unused bank account can be removed')
    call('PUT', '/api/settings', {'business_name': 'Test Co', 'conversion_date': '2026-07-01', 'fy_start_month': 7})
    st = call('GET', '/api/state')
    check(st['settings']['lock_date'] == '2026-06-30', 'lock date defaults to day before start')

    # contacts
    cust = call('POST', '/api/contacts', {'name': 'Acme Pty Ltd', 'kind': 'customer'})['id']
    sup = call('POST', '/api/contacts', {'name': 'Landlord', 'kind': 'supplier'})['id']

    # opening balances: bank 10,000; AR 1,500; AP 400; equipment 3,000; capital 13,000 -> hist diff 1100
    ob = call('PUT', '/api/opening', {'balances': {A['1-1100']: 1000000, A['1-1300']: 150000, A['2-1200']: 40000,
                                                   A['1-2000']: 300000, A['3-1000']: 1300000}})
    check(ob['historical_balancing'] == -110000 or ob['historical_balancing'] == 110000, f"hist balancing {ob['historical_balancing']}")
    # fix capital so it balances: assets 14,500 - AP 400 = 14,100
    ob = call('PUT', '/api/opening', {'balances': {A['1-1100']: 1000000, A['1-1300']: 150000, A['2-1200']: 40000,
                                                   A['1-2000']: 300000, A['3-1000']: 1410000}})
    check(ob['historical_balancing'] == 0, 'opening balances balance')

    # historical invoice (before conversion) makes up AR
    h = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': cust, 'date': '2026-06-15', 'number': 'H-1',
                                       'lines': [{'account_id': A['4-1000'], 'description': 'June work', 'amount': 150000}]})
    hb = call('POST', '/api/invoices', {'kind': 'bill', 'contact_id': sup, 'date': '2026-06-20', 'number': 'R-JUN',
                                        'lines': [{'account_id': A['6-1600'], 'description': 'June rent', 'amount': 40000}]})
    ob = call('GET', '/api/opening')
    check(ob['historical_sales'] == ob['ar_opening'] == 150000, 'historical sales match AR opening')
    check(ob['historical_bills'] == ob['ap_opening'] == 40000, 'historical bills match AP opening')

    # posting before lock fails
    r = call('POST', '/api/txn/spend', {'date': '2026-06-29', 'bank_account_id': A['1-1100'],
                                        'lines': [{'account_id': A['6-1200'], 'amount': 500}]}, expect_ok=False)
    check('locked' in r.get('error', ''), 'lock date blocks posting')

    # July: invoice 1001 for 2,200, receive payment of historical H-1 + part of 1001
    inv = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': cust, 'date': '2026-07-05',
                                         'lines': [{'account_id': A['4-1000'], 'description': 'July consulting', 'amount': 220000}]})
    ivd = call('GET', f"/api/invoices/{inv['id']}")
    check(ivd['number'] == '1001' and ivd['due_date'] == '2026-07-05', f"auto number & due date {ivd['number']} {ivd['due_date']}")
    pay = call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': cust, 'date': '2026-07-10', 'bank_account_id': A['1-1100'],
                                         'allocations': [{'invoice_id': h['id'], 'amount': 150000}, {'invoice_id': inv['id'], 'amount': 100000}]})
    r = call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': cust, 'date': '2026-07-10', 'bank_account_id': A['1-1100'],
                                       'allocations': [{'invoice_id': inv['id'], 'amount': 999999}]}, expect_ok=False)
    check('more than' in r.get('error', ''), 'overpayment rejected')

    # pay historical bill, spend money for bank fee, transfer
    call('POST', '/api/payments', {'kind': 'payment', 'contact_id': sup, 'date': '2026-07-03', 'bank_account_id': A['1-1100'],
                                   'allocations': [{'invoice_id': hb['id'], 'amount': 40000}]})
    fee = call('POST', '/api/txn/spend', {'date': '2026-07-31', 'bank_account_id': A['1-1100'], 'memo': 'Monthly fee',
                                          'lines': [{'account_id': A['6-1200'], 'amount': 1000}]})
    call('POST', '/api/txn/transfer', {'date': '2026-07-20', 'from_account_id': A['1-1100'], 'to_account_id': A['1-1200'], 'amount': 200000})
    # credit card spend
    call('POST', '/api/txn/spend', {'date': '2026-07-12', 'bank_account_id': A['2-1100'], 'memo': 'Software',
                                    'lines': [{'account_id': A['6-1700'], 'amount': 4999}]})

    # recurring monthly rent bill from 2026-07-01, and weekly receive
    rec = call('POST', '/api/recurring', {'name': 'Office rent', 'kind': 'bill', 'contact_id': sup, 'frequency': 'monthly',
                                          'next_date': '2026-07-01', 'terms_days': 7,
                                          'lines': [{'account_id': A['6-1600'], 'description': 'Rent', 'amount': 40000}]})
    # setup not done -> nothing generated
    check(rec['generated'] == [], 'no generation before setup done')
    call('PUT', '/api/settings', {'setup_done': True})
    g = call('POST', '/api/recurring/run')['generated']
    n_rent = len(monthly_dates('2026-07-01', 1))
    check(len([x for x in g if not x.get('skipped')]) == n_rent, f'{n_rent} rent bills generated ({len(g)})')
    rec2 = call('POST', '/api/recurring', {'name': 'Eom sub', 'kind': 'spend', 'bank_account_id': A['1-1100'], 'frequency': 'monthly',
                                           'next_date': '2026-08-31', 'last_day_of_month': True,
                                           'lines': [{'account_id': A['6-1700'], 'description': 'Sub', 'amount': 2000}]})
    dates = [x['date'] for x in rec2['generated']]
    check(dates == monthly_dates('2026-08-31', 31), f'end-of-month anchor {dates}')

    # reconcile: statement 31 July
    rc = call('GET', f"/api/reconcile?account_id={A['1-1100']}")
    check(rc['cleared_balance'] == 1000000, 'opening bank counts as cleared')
    july = [l for l in rc['lines'] if l['date'] <= '2026-07-31']
    expected = 1000000 + sum(l['amount'] for l in july)
    r = call('POST', '/api/reconcile', {'account_id': A['1-1100'], 'statement_date': '2026-07-31', 'statement_balance': expected + 1,
                                        'line_ids': [l['id'] for l in july]}, expect_ok=False)
    check('Out of balance by $0.01' in r.get('error', ''), 'out of balance detected')
    call('POST', '/api/reconcile', {'account_id': A['1-1100'], 'statement_date': '2026-07-31', 'statement_balance': expected,
                                    'line_ids': [l['id'] for l in july]})
    r = call('DELETE', f"/api/journals/{fee['id']}", expect_ok=False)
    check('reconciled' in r.get('error', ''), 'reconciled txn cannot be deleted')
    r = call('PUT', '/api/opening', {'balances': {}}, expect_ok=False)
    check('reconciled' in r.get('error', ''), 'opening balances locked after reconcile')
    call('POST', '/api/reconcile/undo', {'account_id': A['1-1100']})
    call('POST', '/api/reconcile', {'account_id': A['1-1100'], 'statement_date': '2026-07-31', 'statement_balance': expected,
                                    'line_ids': [l['id'] for l in july]})

    # credit card reconcile (liability; owed 49.99)
    rc = call('GET', f"/api/reconcile?account_id={A['2-1100']}")
    call('POST', '/api/reconcile', {'account_id': A['2-1100'], 'statement_date': '2026-07-31', 'statement_balance': 4999,
                                    'line_ids': [l['id'] for l in rc['lines']]})
    check(True, 'credit card reconciles with positive owed balance')

    # Reports
    pl = call('GET', '/api/reports/pl?from=2026-07&to=2026-10')
    # July: income 2200; exp: fee 10 + rent 400 + software 49.99 = 459.99 -> 1740.01
    check(pl['net_profit'][0] == 220000 - 1000 - 40000 - 4999, f"July NP {pl['net_profit'][0]}")
    check(pl['net_profit'][1] == -40000 - 2000, f"Aug NP {pl['net_profit'][1]}")
    bs = call('GET', '/api/reports/bs?to=2026-10&count=4')
    check(bs['balanced'], 'balance sheet balances every month')
    eq = bs['sections'][2]
    cye = [r for r in eq['rows'] if r.get('computed')][0]['values']
    check(cye[-1] == pl['net_profit_total'], f'CYE equals YTD profit {cye[-1]} vs {pl["net_profit_total"]}')
    aged = call('GET', '/api/reports/aged?kind=sale&as_at=2026-10-31')
    check(aged['totals']['total'] == 120000 and aged['totals']['d90plus'] == 120000, f"aged AR {aged['totals']}")
    check(aged['control_balance'] == aged['totals']['total'], 'AR control ties to aged receivables')
    agp = call('GET', '/api/reports/aged?kind=bill&as_at=2026-10-31')
    check(agp['control_balance'] == agp['totals']['total'] == 160000, f"AP ties {agp['control_balance']} {agp['totals']['total']}")
    tb = call('GET', '/api/reports/tb')
    check(tb['debit'] == tb['credit'], 'trial balance balances')

    # FY rollover: BS at 2027-07 - retained earnings picks up FY26-27 profit
    bs2 = call('GET', '/api/reports/bs?to=2027-07&count=1')
    check(bs2['balanced'], 'balance sheet balances after FY rollover')
    re_row = [r for r in bs2['sections'][2]['rows'] if r.get('subtype') == 're'][0]
    check(re_row['values'][0] != 0, f"retained earnings rolls over {re_row['values'][0]}")

    # edit/void
    r = call('POST', f"/api/invoices/{inv['id']}/void", expect_ok=False)
    check('payments' in r.get('error', ''), 'cannot void paid invoice')
    inv2 = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': cust, 'date': '2026-10-01',
                                          'lines': [{'account_id': A['4-1000'], 'amount': 5000}]})
    call('PUT', f"/api/invoices/{inv2['id']}", {'contact_id': cust, 'date': '2026-10-02',
                                                'lines': [{'account_id': A['4-1000'], 'amount': 7000}]})
    check(call('GET', f"/api/invoices/{inv2['id']}")['total'] == 7000, 'invoice edit')
    call('POST', f"/api/invoices/{inv2['id']}/void")
    check(call('GET', '/api/reports/tb')['debit'] == call('GET', '/api/reports/tb')['credit'], 'TB balances after void')
    dash = call('GET', '/api/dashboard')
    check(dash['receivable']['total'] == 120000, 'dashboard AR')
    jl = call('GET', '/api/journals')
    check(len(jl) >= 5, f'journal list {len(jl)}')
    # general journal
    gj = call('POST', '/api/txn/journal', {'date': '2026-10-31', 'memo': 'Depreciation', 'lines': [
        {'account_id': A['6-2200'], 'debit': 5000}, {'account_id': A['1-2100'], 'credit': 5000}]})
    g = call('GET', f"/api/journals/{gj['id']}")
    check(g['editable'] and g['total'] == 5000, 'general journal')
    reg = call('GET', f"/api/register?account_id={A['1-1100']}&from=2026-07-01")
    check(reg['opening'] == 1000000, 'register opening balance')
    # removing used bank accounts
    r = call('DELETE', f"/api/accounts/{A['1-1100']}", expect_ok=False)
    check('Archive' in r.get('error', ''), 'used bank account cannot be deleted')
    r = call('PUT', f"/api/accounts/{A['1-1100']}", {'archived': True}, expect_ok=False)
    check('zero balance' in r.get('error', ''), 'bank account with a balance cannot be archived')
    rec_bank = call('POST', '/api/accounts', {'code': '1-1160', 'name': 'Recurring only', 'type': 'asset', 'subtype': 'bank'})['id']
    rr = call('POST', '/api/recurring', {'name': 'Future', 'kind': 'spend', 'bank_account_id': rec_bank, 'frequency': 'monthly',
                                         'next_date': '2027-01-01', 'lines': [{'account_id': A['6-1700'], 'amount': 100}]})
    r = call('DELETE', f'/api/accounts/{rec_bank}', expect_ok=False)
    check('recurring' in r.get('error', ''), 'bank account used by a recurring template cannot be deleted')
    tb = call('GET', '/api/reports/tb')
    check(tb['debit'] == tb['credit'], 'TB still balances after bank account changes')
    check(call('GET', '/api/reports/bs?to=2026-10&count=1')['balanced'], 'BS still balances after bank account changes')
    # shared bank account: user owns 60%
    r = call('POST', '/api/accounts', {'code': '1-1170', 'name': 'Joint', 'type': 'asset', 'subtype': 'bank', 'share_pct': 0},
             expect_ok=False)
    check('more than 0%' in r.get('error', ''), 'share of 0% rejected')
    r = call('POST', '/api/accounts', {'code': '1-1170', 'name': 'Joint', 'type': 'asset', 'subtype': 'bank', 'share_pct': 150},
             expect_ok=False)
    check('no more than 100%' in r.get('error', ''), 'share over 100% rejected')
    joint = call('POST', '/api/accounts', {'code': '1-1170', 'name': 'Joint', 'type': 'asset', 'subtype': 'bank',
                                           'share_pct': 60})['id']
    check([a for a in call('GET', '/api/accounts') if a['id'] == joint][0]['share_bp'] == 6000, 'share stored as 6000 bp')
    pl0 = call('GET', '/api/reports/pl?from=2026-10&to=2026-10')['net_profit_total']
    bs0 = call('GET', '/api/reports/bs?to=2026-10&count=1')
    assets0 = bs0['sections'][0]['totals'][0]
    # $1,000 from Bank 1 into Joint, spend $100 bank fees from Joint, receive $50.01 income into Joint
    call('POST', '/api/txn/transfer', {'date': '2026-10-10', 'from_account_id': A['1-1100'], 'to_account_id': joint, 'amount': 100000})
    call('POST', '/api/txn/spend', {'date': '2026-10-15', 'bank_account_id': joint,
                                    'lines': [{'account_id': A['6-1200'], 'amount': 10000}]})
    call('POST', '/api/txn/receive', {'date': '2026-10-16', 'bank_account_id': joint,
                                      'lines': [{'account_id': A['4-1000'], 'amount': 5001}]})
    # pay a $200 bill from Joint: AP must clear in full so aged payables still tie
    jb = call('POST', '/api/invoices', {'kind': 'bill', 'contact_id': sup, 'date': '2026-10-18',
                                        'lines': [{'account_id': A['6-1600'], 'amount': 20000}]})
    call('POST', '/api/payments', {'kind': 'payment', 'contact_id': sup, 'date': '2026-10-20', 'bank_account_id': joint,
                                   'allocations': [{'invoice_id': jb['id'], 'amount': 20000}]})
    pl1 = call('GET', '/api/reports/pl?from=2026-10&to=2026-10')['net_profit_total']
    # P&L: fee 60% of 100 = 60, income 60% of 50.01 = 30.01 (rounded), rent bill 200 in full (posted by the bill)
    check(pl1 - pl0 == 3001 - 6000 - 20000, f'P&L shows user share of shared account txns {pl1 - pl0}')
    bs1 = call('GET', '/api/reports/bs?to=2026-10&count=1')
    check(bs1['balanced'], 'BS still balances with a shared account')
    jrow = [r for r in bs1['sections'][0]['rows'] if r['account_id'] == joint][0]
    # Joint holds 1000 - 100 + 50.01 - 200 = 750.01; 60% = 450.006 -> lines cut separately: 600 - 60 + 30.01 - 120
    check(jrow['values'][0] == 60000 - 6000 + 3001 - 12000, f"BS shows user share of Joint {jrow['values'][0]}")
    prow = [r for r in bs1['sections'][2]['rows'] if r.get('partner')]
    # transfer in: 400 belongs to the other owner (-400 equity); bill paid: other owner paid 80 (+80 equity)
    check(prow and prow[0]['values'][0] == -40000 + 8000, f"other owners' share line {prow and prow[0]['values']}")
    check(bs1['sections'][0]['totals'][0] - assets0 == -100000 + jrow['values'][0], 'assets move by Bank 1 out and Joint share in')
    tb = call('GET', '/api/reports/tb')
    check(tb['debit'] == tb['credit'], 'TB still balances with a shared account')
    agp = call('GET', '/api/reports/aged?kind=bill&as_at=2026-10-31')
    check(agp['control_balance'] == agp['totals']['total'], 'AP still ties when a bill is paid from a shared account')
    reg = call('GET', f'/api/register?account_id={joint}&from=2026-10-01')
    check(reg['shared'] and reg['closing'] == 75001 and reg['share_closing'] == jrow['values'][0],
          f"register shows full and share balances {reg['closing']} {reg.get('share_closing')}")
    dash = call('GET', '/api/dashboard')
    jd = [b for b in dash['banks'] if b['id'] == joint][0]
    check(jd['balance'] == 75001 and jd['my_balance'] == 45001, f"dashboard shows user share {jd['my_balance']}")
    rc = call('GET', f'/api/reconcile?account_id={joint}')
    check(sum(l['amount'] for l in rc['lines']) == 75001, 'reconcile uses full amounts')
    # back to 100%: reports go back to full amounts
    call('PUT', f'/api/accounts/{joint}', {'share_pct': 100})
    bs2 = call('GET', '/api/reports/bs?to=2026-10&count=1')
    check(bs2['balanced'] and not [r for r in bs2['sections'][2]['rows'] if r.get('partner')], 'share of 100% removes partner line')
    check([r for r in bs2['sections'][0]['rows'] if r['account_id'] == joint][0]['values'][0] == 75001, 'full balance at 100%')
    call('PUT', f'/api/accounts/{joint}', {'share_pct': 60})
    check(call('GET', '/api/reports/bs?to=2027-07&count=1')['balanced'], 'BS balances after FY rollover with a shared account')

    # shared invoices and bills: rent split 50/50, only the user's half reaches the P&L
    r = call('POST', '/api/invoices', {'kind': 'bill', 'contact_id': sup, 'date': '2026-10-25', 'share_pct': 0,
                                       'lines': [{'account_id': A['6-1600'], 'amount': 100000}]}, expect_ok=False)
    check('more than 0%' in r.get('error', ''), 'bill share of 0% rejected')
    def pl_oct():
        return call('GET', '/api/reports/pl?from=2026-10&to=2026-10')['net_profit_total']
    def partner_oct():
        rows_ = [r for r in call('GET', '/api/reports/bs?to=2026-10&count=1')['sections'][2]['rows'] if r.get('partner')]
        return rows_[0]['values'][0] if rows_ else 0
    def all_balanced(label):
        tb_ = call('GET', '/api/reports/tb?as_at=2026-10-31')
        check(tb_['debit'] == tb_['credit'], f'TB still balances {label}')
        check(call('GET', '/api/reports/bs?to=2026-10&count=1')['balanced'], f'BS still balances {label}')
        for k in ('sale', 'bill'):
            ag = call('GET', f'/api/reports/aged?kind={k}&as_at=2026-10-31')
            check(ag['control_balance'] == ag['totals']['total'], f'aged {k} still ties {label}')
    pl0, pp0 = pl_oct(), partner_oct()
    rent = call('POST', '/api/invoices', {'kind': 'bill', 'contact_id': sup, 'date': '2026-10-25', 'share_pct': 50,
                                          'lines': [{'account_id': A['6-1600'], 'amount': 100000}]})['id']
    check(call('GET', f'/api/invoices/{rent}')['share_bp'] == 5000, 'bill share stored as 5000 bp')
    check(pl_oct() - pl0 == -50000, f'P&L shows half of a 50% shared bill {pl_oct() - pl0}')
    check(partner_oct() - pp0 == -50000, f"other owners' share picks up the other half {partner_oct() - pp0}")
    all_balanced('with a shared bill')
    rinv = call('GET', f'/api/invoices/{rent}')
    check(rinv['total'] == 100000 and rinv['balance'] == 100000, 'shared bill still owes the full amount')
    reg = call('GET', f"/api/register?account_id={A['6-1600']}&from=2026-10-25&to=2026-10-25")
    check(reg['shared'] and [l['share'] for l in reg['lines']] == [50000], f"register shows the share of a shared bill {reg.get('shared')}")
    # editing without share_pct keeps the share; 100% puts it back in full
    call('PUT', f'/api/invoices/{rent}', {'contact_id': sup, 'date': '2026-10-26',
                                          'lines': [{'account_id': A['6-1600'], 'amount': 100000}]})
    check(call('GET', f'/api/invoices/{rent}')['share_bp'] == 5000, 'editing a bill keeps its share')
    call('PUT', f'/api/invoices/{rent}', {'contact_id': sup, 'date': '2026-10-26', 'share_pct': 100,
                                          'lines': [{'account_id': A['6-1600'], 'amount': 100000}]})
    check(pl_oct() - pl0 == -100000 and partner_oct() == pp0, 'bill at 100% shows in full')
    call('PUT', f'/api/invoices/{rent}', {'contact_id': sup, 'date': '2026-10-26', 'share_pct': 50,
                                          'lines': [{'account_id': A['6-1600'], 'amount': 100000}]})
    # paying it from an unshared account clears AP in full and leaves the P&L alone
    call('POST', '/api/payments', {'kind': 'payment', 'contact_id': sup, 'date': '2026-10-27', 'bank_account_id': A['1-1100'],
                                   'allocations': [{'invoice_id': rent, 'amount': 100000}]})
    check(pl_oct() - pl0 == -50000, 'paying a shared bill does not change the P&L')
    all_balanced('after paying a shared bill')
    # shared sales invoice, 25% of $400.03 = $100.01 (rounded half up)
    pl1, pp1 = pl_oct(), partner_oct()
    si = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': cust, 'date': '2026-10-28', 'share_pct': 25,
                                        'lines': [{'account_id': A['4-1000'], 'amount': 40003}]})['id']
    check(pl_oct() - pl1 == 10001, f'P&L shows 25% of a shared invoice {pl_oct() - pl1}')
    check(partner_oct() - pp1 == 30002, f"other owners get the rest of a shared invoice {partner_oct() - pp1}")
    all_balanced('with a shared invoice')
    call('POST', f'/api/invoices/{si}/void')
    check(pl_oct() == pl1 and partner_oct() == pp1, 'voiding a shared invoice removes it')
    # recurring bill at 50%: each bill it creates is shared
    pl2 = pl_oct()
    rb = call('POST', '/api/recurring', {'name': 'Shared rent', 'kind': 'bill', 'contact_id': sup, 'frequency': 'monthly',
                                         'next_date': '2026-10-30', 'end_date': '2026-10-31', 'share_pct': 50,
                                         'lines': [{'account_id': A['6-1600'], 'amount': 30000}]})
    check(call('GET', f"/api/recurring/{rb['id']}")['share_bp'] == 5000, 'recurring bill share stored')
    made = [g for g in rb['generated'] if g.get('type') == 'bill']
    check(len(made) == 1 and call('GET', f"/api/invoices/{made[0]['id']}")['share_bp'] == 5000,
          f"recurring bill creates a shared bill {rb['generated']}")
    check(pl_oct() - pl2 == -15000, f'P&L shows half of the recurring shared bill {pl_oct() - pl2}')
    all_balanced('with a recurring shared bill')
    rs = call('POST', '/api/recurring', {'name': 'Spend', 'kind': 'spend', 'bank_account_id': A['1-1100'], 'frequency': 'monthly',
                                         'next_date': '2027-06-01', 'share_pct': 50,
                                         'lines': [{'account_id': A['6-1200'], 'amount': 100}]})
    check(call('GET', f"/api/recurring/{rs['id']}")['share_bp'] == 10000, 'share is ignored on spend templates')
    call('DELETE', f"/api/recurring/{rs['id']}")

    # partner contributions: a partner's deposits into the joint account (user owns 60%) are the other owners' money
    def bs_oct():
        bs_ = call('GET', '/api/reports/bs?to=2026-10&count=1')
        rows_ = {r['name']: r['values'][0] for sec in bs_['sections'] for r in sec['rows']}
        return bs_['net_assets'][0], rows_
    def mine_oct():
        """Net assets less the other owners' share: what the user is worth."""
        net, rows_ = bs_oct()
        return net - rows_.get("Other owners' share", 0)
    partner_acct = [a for a in call('GET', '/api/accounts') if a['subtype'] == 'partner']
    check(len(partner_acct) == 1 and partner_acct[0]['system'], 'a Partner Contributions system account exists')
    pacct = partner_acct[0]['id']
    pal = call('POST', '/api/contacts', {'name': 'Pat Partner', 'kind': 'customer'})['id']
    pl3, mine0, pp3 = pl_oct(), mine_oct(), partner_oct()
    ar0 = call('GET', '/api/reports/aged?kind=sale&as_at=2026-10-31')['totals']['total']
    c1 = call('POST', '/api/invoices', {'kind': 'partner', 'contact_id': pal, 'date': '2026-10-20', 'amount': 107500,
                                        'description': 'Weekly shared expenses'})['id']
    inv1 = call('GET', f'/api/invoices/{c1}')
    check(inv1['partner'] == 1 and inv1['journal_id'] is None and inv1['balance'] == 107500, 'contribution posts no journal')
    check(pl_oct() == pl3 and mine_oct() == mine0 and partner_oct() == pp3, 'unpaid contribution leaves P&L and BS alone')
    check(call('GET', '/api/reports/aged?kind=sale&as_at=2026-10-31')['totals']['total'] == ar0,
          'unpaid contribution is not in aged receivables')
    check(c1 in [i['id'] for i in call('GET', '/api/invoices?kind=partner')] and
          c1 not in [i['id'] for i in call('GET', '/api/invoices?kind=sale&status=all')], 'contributions list apart from invoices')
    check(call('GET', '/api/dashboard')['contributions']['total'] == 107500, 'dashboard shows contributions due')
    check([c for c in call('GET', '/api/contacts') if c['id'] == pal][0]['contribution_due'] == 107500,
          'contact shows contribution due')
    all_balanced('with an unpaid contribution')
    # it can't be paid together with a customer invoice
    cinv = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': pal, 'date': '2026-10-20',
                                          'lines': [{'account_id': A['4-1000'], 'amount': 1000}]})['id']
    r = call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': pal, 'date': '2026-10-21', 'bank_account_id': joint,
                                       'allocations': [{'invoice_id': c1, 'amount': 107500}, {'invoice_id': cinv, 'amount': 1000}]},
             expect_ok=False)
    check('separately' in r.get('error', ''), 'contribution and invoice paid together rejected')
    call('POST', f'/api/invoices/{cinv}/void')
    # paid into the joint account: user's share of the bank goes up 60% and the other owners' share takes all of it
    pay1 = call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': pal, 'date': '2026-10-21', 'bank_account_id': joint,
                                          'allocations': [{'invoice_id': c1, 'amount': 107500}]})['id']
    check(call('GET', f'/api/payments/{pay1}')['partner'], 'payment knows it is a contribution')
    check(pl_oct() == pl3, 'paid contribution is not income')
    check(mine_oct() == mine0, f'paid contribution leaves the user\'s net worth alone {mine_oct() - mine0}')
    check(partner_oct() - pp3 == 64500, f"other owners' share goes up by the user's share of the deposit {partner_oct() - pp3}")
    check(pacct not in [r_['account_id'] for sec in call('GET', '/api/reports/bs?to=2026-10&count=1')['sections'] for r_ in sec['rows']],
          'partner account folds into the other owners\' share line')
    check(pay1 in [x['id'] for x in call('GET', '/api/payments?kind=contribution')] and
          pay1 not in [x['id'] for x in call('GET', '/api/payments?kind=receipt')], 'contribution payments listed apart')
    all_balanced('with a paid contribution')
    # paid into an account the user owns outright: the whole amount is held for the partner
    c2 = call('POST', '/api/invoices', {'kind': 'partner', 'contact_id': pal, 'date': '2026-10-22', 'amount': 5000})['id']
    pay2 = call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': pal, 'date': '2026-10-22', 'bank_account_id': A['1-1100'],
                                          'allocations': [{'invoice_id': c2, 'amount': 5000}]})['id']
    check(mine_oct() == mine0 and partner_oct() - pp3 == 64500 + 5000, 'contribution into an unshared account is all partner')
    call('DELETE', f'/api/payments/{pay2}')
    call('POST', f'/api/invoices/{c2}/void')
    check(mine_oct() == mine0 and partner_oct() - pp3 == 64500, 'deleting a contribution payment and voiding it undoes it')
    # converting the old way of doing it: a 50% invoice to a "Partner Owes" asset, paid into the joint account and reconciled
    owes = call('POST', '/api/accounts', {'code': '1-9100', 'name': 'Partner Owes', 'type': 'asset'})['id']
    old = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': pal, 'date': '2026-10-23', 'share_pct': 50,
                                         'lines': [{'account_id': owes, 'description': 'Weekly shared expenses', 'amount': 107500}]})['id']
    payo = call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': pal, 'date': '2026-10-23', 'bank_account_id': joint,
                                          'allocations': [{'invoice_id': old, 'amount': 107500}]})['id']
    _, rows_ = bs_oct()
    check(rows_.get('Partner Owes') == -53750, f"old way leaves a negative Partner Owes balance {rows_.get('Partner Owes')}")
    rc = call('GET', f'/api/reconcile?account_id={joint}')
    upto = [l for l in rc['lines'] if l['date'] <= '2026-10-23']
    call('POST', '/api/reconcile', {'account_id': joint, 'statement_date': '2026-10-23',
                                    'statement_balance': rc['cleared_balance'] + sum(l['amount'] for l in upto),
                                    'line_ids': [l['id'] for l in upto]})
    check(call('GET', f'/api/payments/{payo}')['reconciled'], 'old payment is reconciled')
    call('POST', f'/api/invoices/{old}/to-partner')
    oinv = call('GET', f'/api/invoices/{old}')
    check(oinv['partner'] == 1 and oinv['journal_id'] is None and oinv['balance'] == 0 and oinv['share_bp'] == 10000,
          'converted invoice is a paid contribution')
    check(oinv['lines'][0]['account_id'] == pacct and oinv['lines'][0]['description'] == 'Weekly shared expenses',
          'converted invoice keeps its description')
    _, rows_ = bs_oct()
    check(not rows_.get('Partner Owes'), 'converting clears the Partner Owes balance')
    check(mine_oct() == mine0, f'after converting, the user\'s net worth is unchanged {mine_oct() - mine0}')
    check(call('GET', f'/api/payments/{payo}')['reconciled'], 'converted payment stays reconciled')
    pj = call('GET', f"/api/journals/{call('GET', f'/api/payments/{payo}')['journal_id']}")
    check(sorted(l['account_id'] for l in pj['lines']) == sorted([joint, pacct]), 'converted payment now credits the partner account')
    all_balanced('after converting an invoice')
    r = call('POST', f'/api/invoices/{old}/to-partner', expect_ok=False)
    check('Only customer invoices' in r.get('error', ''), 'a contribution cannot be converted twice')
    r = call('POST', f"/api/invoices/{made[0]['id']}/to-partner", expect_ok=False)
    check('Only customer invoices' in r.get('error', ''), 'a bill cannot be converted')
    # a payment covering two invoices must be split first
    i1 = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': pal, 'date': '2026-10-24',
                                        'lines': [{'account_id': owes, 'amount': 100}]})['id']
    i2 = call('POST', '/api/invoices', {'kind': 'sale', 'contact_id': pal, 'date': '2026-10-24',
                                        'lines': [{'account_id': owes, 'amount': 200}]})['id']
    call('POST', '/api/payments', {'kind': 'receipt', 'contact_id': pal, 'date': '2026-10-24', 'bank_account_id': A['1-1100'],
                                   'allocations': [{'invoice_id': i1, 'amount': 100}, {'invoice_id': i2, 'amount': 200}]})
    r = call('POST', f'/api/invoices/{i1}/to-partner', expect_ok=False)
    check('also pays other invoices' in r.get('error', ''), 'converting an invoice with a shared payment is refused')
    check(call('GET', f'/api/invoices/{i1}')['partner'] == 0, 'refused conversion changes nothing')
    hist_inv = [i for i in call('GET', '/api/invoices?kind=sale&status=all') if i['historical']][0]
    r = call('POST', f"/api/invoices/{hist_inv['id']}/to-partner", expect_ok=False)
    check('opening item' in r.get('error', ''), 'converting an opening item is refused')
    # recurring contributions
    rp = call('POST', '/api/recurring', {'name': 'Pat weekly', 'kind': 'partner', 'contact_id': pal, 'frequency': 'weekly',
                                         'next_date': '2026-10-26', 'end_date': '2026-10-31', 'amount': 107500, 'terms_days': 0})
    tpl = call('GET', f"/api/recurring/{rp['id']}")
    check(tpl['kind'] == 'partner' and tpl['lines'][0]['account_id'] == pacct, 'recurring contribution template stored')
    madep = [g for g in rp['generated'] if g.get('type') == 'partner']
    check(len(madep) == 1 and call('GET', f"/api/invoices/{madep[0]['id']}")['partner'] == 1,
          f"recurring template creates a contribution {rp['generated']}")
    check(mine_oct() == mine0 and pl_oct() == pl3, 'recurring contribution leaves the books alone until paid')
    check([r_ for r_ in call('GET', '/api/recurring') if r_['id'] == rp['id']][0]['generated_count'] == 1,
          'recurring list counts the contributions made')
    all_balanced('with a recurring contribution')
    # editing an unpaid contribution
    call('PUT', f"/api/invoices/{madep[0]['id']}", {'contact_id': pal, 'date': '2026-10-26', 'amount': 100000})
    e = call('GET', f"/api/invoices/{madep[0]['id']}")
    check(e['total'] == 100000 and e['partner'] == 1 and e['journal_id'] is None, 'unpaid contribution can be edited')
    r = call('POST', '/api/invoices', {'kind': 'partner', 'contact_id': pal, 'date': '2026-06-30', 'amount': 100}, expect_ok=False)
    check('locked' in r.get('error', ''), 'contribution in a locked period rejected')

    # ---------------------------------------------------------------- bank rules
    def ttest(op, tokens, sample, **kw):
        return call('POST', '/api/rules/test', {'text_op': op, 'text_tokens': tokens, 'sample': sample, **kw})['sample_match']
    T, NUM, ANY = (lambda v: {'t': 'text', 'v': v}), {'t': 'number'}, {'t': 'any'}
    check(ttest('contains', [T('BEAN CAFE'), NUM], 'EFTPOS BEAN  CAFE 1234 SYDNEY'), 'contains text then number matches')
    check(not ttest('contains', [T('BEAN CAFE'), NUM], 'BEAN CAFE SYDNEY'), 'text then number needs the number')
    check(ttest('equals', [T('bean cafe'), NUM], ' BEAN CAFE 12 '), 'equals is case-insensitive and ignores outer spaces')
    check(not ttest('equals', [T('BEAN CAFE'), NUM], 'xx BEAN CAFE 12'), 'equals needs the whole text')
    check(ttest('contains', [T('A.B (C)')], 'paid A.B (C) today') and not ttest('contains', [T('A.B (C)')], 'AXB C'),
          'special characters in text are matched literally')
    check(ttest('contains', [T('PAY'), {'t': 'date', 'fmt': 'DD/MM/YYYY'}], 'PAY 07/11/2026')
          and not ttest('contains', [T('PAY'), {'t': 'date', 'fmt': 'DD/MM/YYYY'}], 'PAY 2026-11-07'), 'date token follows its format')
    check(ttest('equals', [T('REF'), {'t': 'letters', 'count': 3}], 'REF ABC')
          and not ttest('equals', [T('REF'), {'t': 'letters', 'count': 3}], 'REF ABCD'), 'letters token with an exact count')
    check(ttest('equals', [T('TFR'), ANY, T('SAVINGS')], 'TFR TO MY SAVINGS'), '"anything" token fills a gap')
    for bad, why in (({'text_op': 'contains', 'text_tokens': []}, 'empty pattern'),
                     ({'text_op': 'contains', 'text_tokens': [{'t': 'regex', 'v': '.*'}]}, 'unknown part type'),
                     ({'text_op': 'contains', 'text_tokens': [ANY]}, 'pattern of only "anything"'),
                     ({'text_op': 'contains', 'text_tokens': [{'t': 'date', 'fmt': '//'}]}, 'date format without D/M/Y')):
        r = call('POST', '/api/rules/test', bad, expect_ok=False)
        check('error' in r, f'rule test rejects {why}')
    base = {'mode': 'recommend', 'target_account_id': A['6-9000']}
    r = call('POST', '/api/rules', {**base, 'name': 'Nothing'}, expect_ok=False)
    check('needs a bank text pattern' in r.get('error', ''), 'a rule needs at least one condition')
    r = call('POST', '/api/rules', {**base, 'name': 'Bad', 'amount_op': 'lt', 'amount': 100, 'target_account_id': A['1-1300']}, expect_ok=False)
    check('can\'t be used by a rule' in r.get('error', ''), 'Accounts Receivable can\'t be a rule target')
    r = call('POST', '/api/rules', {**base, 'name': 'Bad', 'amount_op': 'lt', 'amount': 100, 'target_account_id': A['1-1200'],
                                    'account_ids': [A['1-1200']]}, expect_ok=False)
    check('can\'t transfer to it' in r.get('error', ''), 'a transfer rule can\'t only apply to the account it transfers to')
    r = call('POST', '/api/rules', {**base, 'name': 'Bad', 'amount_op': 'lt', 'amount': 0}, expect_ok=False)
    check('more than zero' in r.get('error', ''), 'rule amount must be positive')
    bank1, savings = A['1-1100'], A['1-1200']
    coffee = call('POST', '/api/rules', {**base, 'name': 'Coffee', 'text_op': 'contains', 'text_tokens': [T('BEAN CAFE')],
                                         'direction': 'out', 'amount_op': 'lt', 'amount': 1000, 'account_ids': [bank1]})['id']
    lunch = call('POST', '/api/rules', {'name': 'Lunch', 'mode': 'auto', 'target_account_id': A['6-1900'], 'text_op': 'contains',
                                        'text_tokens': [T('BEAN CAFE')], 'direction': 'out', 'amount_op': 'gt', 'amount': 999})['id']
    interest = call('POST', '/api/rules', {'name': 'Interest', 'mode': 'auto', 'target_account_id': A['4-3000'], 'text_op': 'equals',
                                           'text_tokens': [T('INTEREST')], 'direction': 'in'})['id']
    rl = call('GET', '/api/rules')
    check([x['name'] for x in rl] == ['Coffee', 'Lunch', 'Interest'], 'rules list in priority order')
    check(rl[0]['account_ids'] == [bank1] and rl[1]['account_ids'] == [] and rl[0]['regex'], 'rule scope and regex returned')
    manual = call('POST', '/api/txn/spend', {'bank_account_id': bank1, 'date': '2026-11-02', 'memo': 'Lunch with client',
                                             'lines': [{'account_id': A['6-9000'], 'amount': 1550}]})['id']
    lock = call('GET', '/api/state')['settings']['lock_date']
    stmt = [{'date': '2026-11-01', 'amount': -450, 'description': 'BEAN CAFE 1234 SYDNEY'},
            {'date': '2026-11-01', 'amount': -450, 'description': 'BEAN CAFE 1234 SYDNEY'},
            {'date': '2026-11-02', 'amount': -1550, 'description': 'Bean Cafe 99'},
            {'date': '2026-11-03', 'amount': 120, 'description': 'INTEREST'},
            {'date': '2026-11-03', 'amount': -2000, 'description': 'HARDWARE STORE'},
            {'date': lock, 'amount': -300, 'description': 'BEAN CAFE 1'}]
    imp = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': stmt})['results']
    check([x['status'] for x in imp] == ['recommend', 'recommend', 'auto', 'auto', 'none', 'locked'],
          f"import applies rules by amount and text {[x['status'] for x in imp]}")
    check(imp[0]['rule_name'] == 'Coffee' and imp[2]['rule_name'] == 'Lunch' and imp[3]['rule_id'] == interest, 'import names the rule used')
    mline = call('GET', f'/api/journals/{manual}')['lines']
    mbank = [l for l in mline if l['role'] == 'bank'][0]['id']
    check(imp[2].get('possible_dup_line_id') == mbank, 'rule wins but flags the similar hand-entered item')
    cj = call('GET', f"/api/journals/{imp[0]['journal_id']}")
    check(cj['source'] == 'spend' and cj['rule_mode'] == 'recommend' and cj['rule_name'] == 'Coffee'
          and cj['bank_text'] == 'BEAN CAFE 1234 SYDNEY' and cj['items'][0]['account_id'] == A['6-9000']
          and cj['items'][0]['amount'] == 450 and not cj['reconciled'], 'recommended rule creates an unreconciled spend in its category')
    ij = call('GET', f"/api/journals/{imp[3]['journal_id']}")
    check(ij['source'] == 'receive' and ij['total'] == 120 and ij['items'][0]['account_id'] == A['4-3000'], 'money-in rule creates a receive')
    reg = call('GET', f'/api/register?account_id={bank1}&from=2026-11-01')
    rr = {l['journal_id']: l for l in reg['lines']}
    check(rr[imp[2]['journal_id']]['rule_name'] == 'Lunch' and rr[imp[2]['journal_id']]['rule_mode'] == 'auto',
          'banking register shows which rule created a transaction')
    sv = call('POST', '/api/reconcile/import', {'account_id': savings, 'lines': [{'date': '2026-11-01', 'amount': -450, 'description': 'BEAN CAFE 1'}]})
    check(sv['results'][0]['status'] == 'none', 'a rule limited to one bank account ignores other accounts')
    again = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': stmt})['results']
    check([x['status'] for x in again][:4] == ['duplicate'] * 4 and again[4]['status'] == 'none',
          f"re-importing the statement skips lines already in numbr {[x['status'] for x in again]}")
    check(again[0]['line_id'] == imp[0]['line_id'] and again[0]['reconciled'] is False,
          'a duplicate points at the existing unreconciled line so it can still be ticked')
    check(rr[imp[0]['journal_id']]['category'] == 'General Expenses', 'register shows the category a rule chose')
    triple = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': [stmt[0]] * 3})['results']
    check([x['status'] for x in triple] == ['duplicate', 'duplicate', 'recommend'], 'identical lines are counted, not collapsed')
    call('DELETE', f"/api/journals/{triple[2]['journal_id']}")
    # priority: first matching rule wins
    hw_gen = call('POST', '/api/rules', {**base, 'name': 'Hardware', 'text_op': 'contains', 'text_tokens': [T('HARDWARE')]})['id']
    hw_exact = call('POST', '/api/rules', {'name': 'Hardware $20', 'mode': 'auto', 'target_account_id': A['6-1500'],
                                           'amount_op': 'eq', 'amount': 2000, 'direction': 'out'})['id']
    call('POST', '/api/rules/reorder', {'ids': [hw_exact, coffee, lunch, interest, hw_gen]})
    hw = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': [
        {'date': '2026-11-04', 'amount': -2000, 'description': 'HARDWARE STORE 2'},
        {'date': '2026-11-04', 'amount': -3000, 'description': 'HARDWARE STORE'}]})['results']
    check([x['rule_id'] for x in hw] == [hw_exact, hw_gen], 'rules run in priority order')
    call('PUT', f'/api/rules/{hw_gen}', {**base, 'name': 'Hardware', 'text_op': 'contains', 'text_tokens': [T('HARDWARE')], 'active': False})
    off = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': [{'date': '2026-11-04', 'amount': -3100, 'description': 'HARDWARE X'}]})
    check(off['results'][0]['status'] == 'none', 'inactive rules are skipped')
    # editing a recommended transaction marks it reviewed and keeps its statement text
    call('PUT', f"/api/journals/{imp[1]['journal_id']}", {'bank_account_id': bank1, 'date': '2026-11-01', 'memo': 'Coffee',
                                                          'lines': [{'account_id': A['6-1100'], 'amount': 450}]})
    ej = call('GET', f"/api/journals/{imp[1]['journal_id']}")
    check(ej['reviewed'] == 1 and ej['rule_id'] == coffee and ej['bank_text'] == 'BEAN CAFE 1234 SYDNEY',
          'editing a rule transaction marks it reviewed and keeps the bank text')
    # reconcile stores statement text on matched hand-entered items
    rc = call('GET', f'/api/reconcile?account_id={bank1}')
    upto = [l for l in rc['lines'] if l['date'] <= '2026-11-04']
    check(any(l['rule_mode'] == 'recommend' and l['rule_name'] == 'Coffee' for l in upto), 'reconcile lines carry rule info')
    call('POST', '/api/reconcile', {'account_id': bank1, 'statement_date': '2026-11-04',
                                    'statement_balance': rc['cleared_balance'] + sum(l['amount'] for l in upto),
                                    'line_ids': [l['id'] for l in upto], 'bank_text': {str(mbank): 'EFTPOS LUNCH PLACE'}})
    check(call('GET', f"/api/journals/{imp[0]['journal_id']}")['reviewed'] == 1, 'reconciling a recommended transaction marks it reviewed')
    d2 = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': [{'date': '2026-11-02', 'amount': -1550, 'description': 'EFTPOS  lunch place'}]})
    check(d2['results'][0]['status'] == 'duplicate' and d2['results'][0]['reconciled'] is True,
          'a matched hand-entered item is recognised on the next import')
    # deleting rules
    call('DELETE', f'/api/rules/{coffee}')
    check(coffee not in [x['id'] for x in call('GET', '/api/rules')], 'deleted rule leaves the list')
    check(call('GET', f"/api/journals/{imp[0]['journal_id']}")['rule_name'] == 'Coffee', 'transactions keep the name of a deleted rule')
    unused = call('POST', '/api/rules', {**base, 'name': 'Unused', 'amount_op': 'eq', 'amount': 1})['id']
    call('DELETE', f'/api/rules/{unused}')
    r = call('GET', f'/api/rules/{unused}', expect_ok=False)
    check('not found' in r.get('error', ''), 'unused rule is removed')
    # transfer rules
    xfer = call('POST', '/api/rules', {'name': 'To savings', 'mode': 'auto', 'target_account_id': savings, 'text_op': 'contains',
                                       'text_tokens': [T('INTERNET TRANSFER SAVINGS')]})['id']
    ximp = call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': [
        {'date': '2026-11-05', 'amount': -50000, 'description': 'INTERNET TRANSFER SAVINGS'}]})['results'][0]
    check(ximp['status'] == 'auto' and ximp['rule_id'] == xfer, 'a transfer rule applies to a statement line')
    xj = call('GET', f"/api/journals/{ximp['journal_id']}")
    check(xj['source'] == 'transfer' and xj['from_account_id'] == bank1 and xj['to_account_id'] == savings and xj['amount'] == 50000,
          'a transfer rule creates a transfer between the two bank accounts')
    check(ximp['line_id'] == [l['id'] for l in xj['lines'] if l['account_id'] == bank1][0], 'the line returned is in the imported account')
    sv_line = [l['id'] for l in xj['lines'] if l['account_id'] == savings][0]
    rc_sv = call('GET', f'/api/reconcile?account_id={savings}')
    check(any(l['id'] == sv_line and l['category'].startswith('Transfer: ') for l in rc_sv['lines']), 'transfer rule lines show as a transfer')
    # the same rule (all accounts) sees the other side in the savings statement: it's matched, not created again
    other = call('POST', '/api/reconcile/import', {'account_id': savings, 'lines': [
        {'date': '2026-11-06', 'amount': 50000, 'description': 'TRANSFER FROM CHEQUE'},
        {'date': '2026-11-06', 'amount': -2500, 'description': 'INTERNET TRANSFER SAVINGS'}]})['results']
    check(other[0]['status'] == 'none' and other[1]['status'] == 'none',
          'a transfer rule never applies in the account it transfers to; other lines are left alone')
    back = call('POST', '/api/rules', {'name': 'From cheque', 'mode': 'auto', 'target_account_id': bank1, 'text_op': 'contains',
                                       'text_tokens': [T('TRANSFER FROM CHEQUE')], 'account_ids': [savings]})['id']
    n_before = len(call('GET', '/api/journals?source=transfer'))
    other = call('POST', '/api/reconcile/import', {'account_id': savings, 'lines': [
        {'date': '2026-11-06', 'amount': 50000, 'description': 'TRANSFER FROM CHEQUE'}]})['results'][0]
    check(other['status'] == 'duplicate' and other['line_id'] == sv_line and other['reconciled'] is False,
          'importing the other side of a rule transfer matches the existing transfer')
    check(len(call('GET', '/api/journals?source=transfer')) == n_before, 'the transfer is not created twice')
    again = call('POST', '/api/reconcile/import', {'account_id': savings, 'lines': [
        {'date': '2026-11-06', 'amount': 50000, 'description': 'TRANSFER FROM CHEQUE'}]})['results'][0]
    check(again['status'] == 'duplicate' and again['line_id'] == sv_line, 're-importing the other side is still a duplicate')
    # editing a rule transfer keeps both sides' statement text
    call('PUT', f"/api/journals/{xj['id']}", {'from_account_id': bank1, 'to_account_id': savings, 'date': '2026-11-05',
                                              'amount': 50000, 'memo': 'Savings'})
    texts = {l['account_id']: l['bank_text'] for l in call('GET', f"/api/journals/{xj['id']}")['lines']}
    check(texts == {bank1: 'INTERNET TRANSFER SAVINGS', savings: 'TRANSFER FROM CHEQUE'}, 'editing a transfer keeps both statement texts')
    call('DELETE', f'/api/rules/{back}')

    # saved CSV column choices per account
    cmap = {'head': True, 'df': 'dmy', 'mode': 'single', 'flip': False,
            'cols': {'date': {'i': 0, 'name': 'Date'}, 'desc': {'i': 5, 'name': 'Transaction Details'},
                     'amt': {'i': 1, 'name': 'Amount'}, 'dr': {'i': -1, 'name': ''}, 'cr': {'i': -1, 'name': ''}}}
    call('POST', '/api/reconcile/import', {'account_id': bank1, 'mapping': cmap,
                                           'lines': [{'date': '2026-11-06', 'amount': -999, 'description': 'MAP TEST'}]})
    accs = {a['id']: a for a in call('GET', '/api/state')['accounts']}
    check(accs[bank1]['csv_map'] == cmap, 'import saves the CSV column choices on the account')
    check(accs[savings]['csv_map'] is None, 'other accounts keep their own (no) column choices')
    call('POST', '/api/reconcile/import', {'account_id': bank1, 'lines': [{'date': '2026-11-06', 'amount': -998, 'description': 'MAP TEST 2'}]})
    check({a['id']: a for a in call('GET', '/api/state')['accounts']}[bank1]['csv_map'] == cmap,
          'an import without column choices keeps the saved ones')
    r = call('POST', '/api/reconcile/import', {'account_id': bank1, 'mapping': 'bad', 'lines': [{'date': '2026-11-06', 'amount': -997, 'description': 'X'}]}, expect_ok=False)
    check('column choices' in r.get('error', ''), 'invalid column choices are refused')
    tb_ = call('GET', '/api/reports/tb?as_at=2026-11-30')
    check(tb_['debit'] == tb_['credit'], 'TB still balances after bank rules')
    check(call('GET', '/api/reports/bs?to=2026-11&count=1')['balanced'], 'BS still balances after bank rules')

    # account groups (presentation only: headings and subtotals)
    def acc_state(aid):
        return {a['id']: a for a in call('GET', '/api/state')['accounts']}[aid]

    def bal_snapshot():
        return {a['id']: a['balance'] for a in call('GET', '/api/state')['accounts']}

    before = bal_snapshot()
    pl_before = call('GET', '/api/reports/pl?from=2026-07&to=2026-11')
    office = call('POST', '/api/groups', {'name': 'Office costs', 'type': 'expense'})['id']
    check(call('POST', '/api/groups', {'name': 'office COSTS', 'type': 'expense'}, expect_ok=False).get('error'),
          'duplicate group name at the same level is refused')
    premises = call('POST', '/api/groups', {'name': 'Premises', 'type': 'expense', 'parent_id': office,
                                            'account_ids': [A['6-1600'], A['6-2000']]})['id']
    check(acc_state(A['6-1600'])['group_id'] == premises, 'group create can assign accounts')
    r = call('POST', '/api/groups', {'name': 'X', 'type': 'income', 'parent_id': office}, expect_ok=False)
    check('same account type' in r.get('error', ''), 'a group must sit inside a group of the same type')
    r = call('POST', '/api/groups', {'name': 'X', 'type': 'expense', 'account_ids': [A['4-1000']]}, expect_ok=False)
    check('different type' in r.get('error', ''), 'a group only takes accounts of its own type')
    r = call('PUT', f'/api/groups/{office}', {'parent_id': premises}, expect_ok=False)
    check('inside itself' in r.get('error', ''), 'a group can not sit inside its own subgroup')
    g3 = call('POST', '/api/groups', {'name': 'L3', 'type': 'expense', 'parent_id': premises})['id']
    g4 = call('POST', '/api/groups', {'name': 'L4', 'type': 'expense', 'parent_id': g3})['id']
    r = call('POST', '/api/groups', {'name': 'L5', 'type': 'expense', 'parent_id': g4}, expect_ok=False)
    check('levels deep' in r.get('error', ''), 'groups stop at 4 levels')
    call('DELETE', f'/api/groups/{g4}')
    call('DELETE', f'/api/groups/{g3}')

    # account form: pick a group, or create one on the spot
    acc = acc_state(A['6-1200'])
    call('PUT', f"/api/accounts/{A['6-1200']}", {'code': acc['code'], 'name': acc['name'], 'type': 'expense', 'group_id': office})
    check(acc_state(A['6-1200'])['group_id'] == office, 'account edit puts the account in a group')
    r = call('PUT', f"/api/accounts/{A['4-2000']}", {'code': '4-2000', 'name': 'Other Income', 'type': 'income', 'group_id': office}, expect_ok=False)
    check('different type' in r.get('error', ''), 'account edit refuses a group of another type')
    acc = acc_state(A['6-1700'])
    call('PUT', f"/api/accounts/{A['6-1700']}", {'code': acc['code'], 'name': acc['name'], 'type': 'expense',
                                                'new_group': {'name': 'Tech', 'parent_id': office}})
    st_ = call('GET', '/api/state')
    tech = [g for g in st_['groups'] if g['name'] == 'Tech']
    check(len(tech) == 1 and tech[0]['parent_id'] == office and acc_state(A['6-1700'])['group_id'] == tech[0]['id'],
          'account edit can create a new group and join it')
    r = call('PUT', f"/api/accounts/{A['6-1800']}", {'code': '6-1800', 'name': 'Telephone & Internet', 'type': 'expense',
                                                    'new_group': {'name': ''}}, expect_ok=False)
    check(r.get('error') and not [g for g in call('GET', '/api/state')['groups'] if g['name'] == ''], 'a new group needs a name')
    nc = call('POST', '/api/accounts', {'code': '6-9100', 'name': 'Grouped new', 'type': 'expense', 'group_id': office})['id']
    check(acc_state(nc)['group_id'] == office, 'a new account can start in a group')
    call('PUT', f'/api/accounts/{nc}', {'code': '6-9100', 'name': 'Grouped new', 'type': 'cos'})
    check(acc_state(nc)['group_id'] is None, 'changing an account type takes it out of a group of the old type')
    call('DELETE', f'/api/accounts/{nc}')

    # reports carry group_id; totals and balances are untouched
    pl_g = call('GET', '/api/reports/pl?from=2026-07&to=2026-11')
    exp = next(s for s in pl_g['sections'] if s['type'] == 'expense')
    rent = next(r for r in exp['rows'] if r['account_id'] == A['6-1600'])
    check(rent['group_id'] == premises, 'P&L rows carry their group')
    check(pl_g['net_profit_total'] == pl_before['net_profit_total'] and
          [s['total'] for s in pl_g['sections']] == [s['total'] for s in pl_before['sections']],
          'groups do not change P&L totals')
    bs_g = call('GET', '/api/reports/bs?to=2026-11&count=1')
    check(all('group_id' in r for s in bs_g['sections'] for r in s['rows'] if r.get('account_id')), 'BS rows carry group_id')
    check(bs_g['balanced'], 'BS still balances with groups')
    tb_ = call('GET', '/api/reports/tb?as_at=2026-11-30')
    check(tb_['debit'] == tb_['credit'], 'TB still balances with groups')
    check(bal_snapshot() == before, 'groups do not change any account balance')

    # group edit sets the exact members; delete moves contents up a level
    call('PUT', f'/api/groups/{premises}', {'name': 'Premises', 'account_ids': [A['6-1600']]})
    check(acc_state(A['6-2000'])['group_id'] is None and acc_state(A['6-1600'])['group_id'] == premises,
          'group edit sets exactly the ticked accounts')
    call('DELETE', f'/api/groups/{office}')
    gs = {g['id']: g for g in call('GET', '/api/state')['groups']}
    check(office not in gs and gs[premises]['parent_id'] is None and gs[tech[0]['id']]['parent_id'] is None,
          'deleting a group moves its subgroups up')
    check(acc_state(A['6-1200'])['group_id'] is None, 'deleting a group moves its accounts up')
    acts = [a['action'] for a in call('GET', '/api/audit')]
    check(all(x in acts for x in ('group_create', 'group_update', 'group_delete')), 'group changes are audited')

    # account codes are backend-only: the server picks one when none is given
    codes_before = {a['code'] for a in call('GET', '/api/state')['accounts']}
    na = call('POST', '/api/accounts', {'name': 'Parking', 'type': 'expense'})['id']
    nb = call('POST', '/api/accounts', {'name': 'Tolls', 'type': 'expense'})['id']
    ca, cb = acc_state(na)['code'], acc_state(nb)['code']
    check(ca.startswith('6-') and cb.startswith('6-') and ca != cb and ca not in codes_before,
          f'new accounts without a code get a free code of their type ({ca}, {cb})')
    check(acc_state(call('POST', '/api/accounts', {'name': 'Savings 2', 'type': 'asset', 'subtype': 'bank'})['id'])['code'].startswith('1-'),
          'a new bank account gets an asset code')
    call('PUT', f'/api/accounts/{na}', {'name': 'Parking fees'})
    check(acc_state(na)['code'] == ca and acc_state(na)['name'] == 'Parking fees', 'renaming keeps the code')
    r = call('POST', '/api/accounts', {'name': '', 'type': 'expense'}, expect_ok=False)
    check('name' in r.get('error', ''), 'an account needs a name')

    # account names are unique (ignoring case and spaces), because users pick accounts by name
    r = call('POST', '/api/accounts', {'name': '  parking   FEES ', 'type': 'income'}, expect_ok=False)
    check('already an account called' in r.get('error', ''), 'a duplicate name is refused, ignoring case, spaces and type')
    r = call('PUT', f'/api/accounts/{nb}', {'name': 'Parking Fees'}, expect_ok=False)
    check('already an account called' in r.get('error', '') and acc_state(nb)['name'] == 'Tolls', 'renaming onto another name is refused')
    call('PUT', f'/api/accounts/{nb}', {'name': 'Bridge  tolls '})
    check(acc_state(nb)['name'] == 'Bridge tolls', 'names are saved trimmed with single spaces')
    call('PUT', f'/api/accounts/{nb}', {'archived': True})
    r = call('POST', '/api/accounts', {'name': 'Bridge Tolls', 'type': 'expense'}, expect_ok=False)
    check('archived' in r.get('error', ''), 'a name used by an archived account is refused, with a hint to un-archive')
    # books made before this rule may already have duplicates: they can still be edited, just not renamed onto each other
    con = sqlite3.connect(os.path.join(data, 'books.db'))
    dup = con.execute("INSERT INTO accounts(code, name, type, subtype) VALUES ('6-8888', 'parking fees', 'expense', 'other')").lastrowid
    con.commit(); con.close()
    call('PUT', f'/api/accounts/{dup}', {'name': 'parking fees', 'archived': True})
    check(acc_state(dup)['archived'], 'an existing duplicate can still be edited and archived')
    call('PUT', f'/api/accounts/{dup}', {'name': 'Parking (old)'})
    check(acc_state(dup)['name'] == 'Parking (old)', 'an existing duplicate can be renamed to something unique')

    # accounts come back A-Z by name; the trial balance is by type, then name
    names = [a['name'] for a in call('GET', '/api/state')['accounts']]
    check(names == sorted(names, key=str.casefold), 'accounts are listed A-Z by name')
    tb_ = call('GET', '/api/reports/tb?as_at=2026-11-30')
    order = ['asset', 'liability', 'equity', 'income', 'cos', 'expense']
    keys = [(order.index(r['type']), r['name'].casefold()) for r in tb_['rows'] if r['account_id']]
    check(keys == sorted(keys), 'trial balance is in type order, then A-Z')
    check(tb_['debit'] == tb_['credit'], 'TB still balances after account changes')
    check(call('GET', '/api/reports/bs?to=2026-11&count=1')['balanced'], 'BS still balances after account changes')

    # security header
    try:
        urllib.request.urlopen(BASE + '/api/state')
        check(False, 'api requires header')
    except urllib.error.HTTPError as e:
        check(e.code == 403, 'api requires X-Ledger header')
    html = urllib.request.urlopen(BASE + '/').read() if os.path.exists(os.path.join(ROOT, 'static', 'index.html')) else b''
    print('index bytes', len(html))
finally:
    p.terminate()
    out = p.stdout.read().decode()
    if 'Traceback' in out:
        print(out)
    shutil.rmtree(data)
print('FAILS:', fails)
sys.exit(1 if fails else 0)
