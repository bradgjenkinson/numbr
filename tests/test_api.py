"""Backend scenario test: runs the app against a temporary database and checks the books tie out.
Run: python tests/test_api.py"""
import json, os, subprocess, sys, time, urllib.request, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'app.py')
data = tempfile.mkdtemp()
env = dict(os.environ, LEDGER_DATA=data, LEDGER_PORT='8799')
p = subprocess.Popen([sys.executable, APP, '--no-browser'], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(1.2)
BASE = 'http://localhost:8799'


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
    cust = call('POST', '/api/contacts', {'name': 'Acme Pty Ltd', 'kind': 'customer', 'terms_days': 14})['id']
    sup = call('POST', '/api/contacts', {'name': 'Landlord', 'kind': 'supplier', 'terms_days': 7})['id']

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
    check(ivd['number'] == '1001' and ivd['due_date'] == '2026-07-19', f"auto number & due date {ivd['number']} {ivd['due_date']}")
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
    # today 2026-10-31 -> Jul, Aug, Sep, Oct = 4
    check(len([x for x in g if not x.get('skipped')]) == 4, f'4 rent bills generated ({len(g)})')
    rec2 = call('POST', '/api/recurring', {'name': 'Eom sub', 'kind': 'spend', 'bank_account_id': A['1-1100'], 'frequency': 'monthly',
                                           'next_date': '2026-08-31', 'last_day_of_month': True,
                                           'lines': [{'account_id': A['6-1700'], 'description': 'Sub', 'amount': 2000}]})
    dates = [x['date'] for x in rec2['generated']]
    check(dates == ['2026-08-31', '2026-09-30', '2026-10-31'], f'end-of-month anchor {dates}')

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
