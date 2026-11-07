"""Backend scenario test: runs the app against a temporary database and checks the books tie out.
Run: python tests/test_api.py"""
import json, os, subprocess, sys, time, urllib.request, tempfile, shutil, calendar
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
