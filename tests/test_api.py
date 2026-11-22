"""Backend scenario test: runs the app against a temporary database and checks the books tie out.
Run: python tests/test_api.py"""
import json, os, sqlite3, subprocess, sys, time, urllib.request, tempfile, shutil, calendar
from datetime import date, timedelta

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
    # opening balances lock per account: only the reconciled bank account is frozen
    ob = call('GET', '/api/opening')
    check(ob['locked_accounts'] == [A['1-1100']], f"only reconciled account locked {ob['locked_accounts']}")
    orig = {A['1-1100']: 1000000, A['1-1300']: 150000, A['2-1200']: 40000, A['1-2000']: 300000, A['3-1000']: 1410000}
    r = call('PUT', '/api/opening', {'balances': {**orig, A['1-1100']: 999999}}, expect_ok=False)
    check('reconciled' in r.get('error', ''), 'reconciled account opening balance locked')
    recon_before = [h for h in call('GET', f"/api/reconcile?account_id={A['1-1100']}")['history'] if h['note'] == 'opening']
    ob = call('PUT', '/api/opening', {'balances': {**orig, A['1-2000']: 350000, A['3-1000']: 1460000}})
    check(ob['balances'][str(A['1-2000'])] == 350000 and ob['historical_balancing'] == 0, 'unreconciled account opening editable')
    check(ob['balances'][str(A['1-1100'])] == 1000000, 'locked bank opening kept')
    recon_after = [h for h in call('GET', f"/api/reconcile?account_id={A['1-1100']}")['history'] if h['note'] == 'opening']
    check([h['id'] for h in recon_after] == [h['id'] for h in recon_before], 'opening reconciliation kept for locked account')
    check(call('GET', '/api/reports/tb')['debit'] == call('GET', '/api/reports/tb')['credit'], 'TB balances after opening edit')
    check(call('GET', '/api/reports/bs?to=2026-10&count=1')['balanced'], 'BS balances after opening edit')
    ob = call('PUT', '/api/opening', {'balances': {k: v for k, v in orig.items() if k != A['1-1100']}})
    check(ob['balances'][str(A['1-1100'])] == 1000000 and ob['historical_balancing'] == 0, 'omitted locked account keeps its balance')
    # an archived, unreconciled account left out of the form keeps its opening balance
    call('PUT', '/api/opening', {'balances': {**orig, A['1-2000']: 350000, A['3-1000']: 1460000}})
    # (archived directly: the API only archives zero-balance accounts, but the account's history may predate that)
    db = sqlite3.connect(os.path.join(data, 'books.db'))
    db.execute('UPDATE accounts SET archived=1 WHERE id=?', (A['1-2000'],)); db.commit(); db.close()
    ob = call('PUT', '/api/opening', {'balances': {k: v for k, v in orig.items() if k != A['1-2000']} | {A['3-1000']: 1460000}})
    check(ob['balances'][str(A['1-2000'])] == 350000 and ob['historical_balancing'] == 0, 'omitted archived account keeps its opening balance')
    db = sqlite3.connect(os.path.join(data, 'books.db'))
    db.execute('UPDATE accounts SET archived=0 WHERE id=?', (A['1-2000'],)); db.commit(); db.close()
    ob = call('PUT', '/api/opening', {'balances': {k: v for k, v in orig.items() if k != A['1-1100']}})
    check(ob['historical_balancing'] == 0, 'opening balances restored after archived check')
    call('POST', '/api/reconcile/undo', {'account_id': A['1-1100']})
    call('POST', '/api/reconcile', {'account_id': A['1-1100'], 'statement_date': '2026-07-31', 'statement_balance': expected,
                                    'line_ids': [l['id'] for l in july]})

    # a reconciled transaction can still be recategorised, but its bank side can't change
    fj = call('GET', f"/api/journals/{fee['id']}")
    check(fj['reconciled'] and not fj['editable'] and fj['recategorise'], 'reconciled spend offers recategorising')
    rc_before = call('GET', f"/api/reconcile?account_id={A['1-1100']}")
    fee_body = {'date': '2026-07-31', 'bank_account_id': A['1-1100'], 'memo': 'Monthly fee'}
    call('PUT', f"/api/journals/{fee['id']}", {**fee_body, 'lines': [{'account_id': A['6-1700'], 'amount': 600},
                                                                     {'account_id': A['6-1200'], 'amount': 400}]})
    fj = call('GET', f"/api/journals/{fee['id']}")
    bank_line = [l for l in fj['lines'] if l['role'] == 'bank'][0]
    check(fj['reconciled'] and bank_line['recon_id'] and sorted(i['amount'] for i in fj['items']) == [400, 600],
          'recategorised and split reconciled spend keeps its reconciliation')
    rc_after = call('GET', f"/api/reconcile?account_id={A['1-1100']}")
    check(rc_after['cleared_balance'] == rc_before['cleared_balance'] and rc_after['history'] == rc_before['history'],
          'recategorising leaves the reconciliation untouched')
    for bad, word in ((dict(fee_body, lines=[{'account_id': A['6-1200'], 'amount': 1001}]), 'amount'),
                      (dict(fee_body, date='2026-07-30', lines=[{'account_id': A['6-1200'], 'amount': 1000}]), 'date'),
                      (dict(fee_body, bank_account_id=A['1-1200'], lines=[{'account_id': A['6-1200'], 'amount': 1000}]), 'amount')):
        r = call('PUT', f"/api/journals/{fee['id']}", bad, expect_ok=False)
        check(word in r.get('error', ''), f'reconciled spend {word} change rejected')
    call('PUT', f"/api/journals/{fee['id']}", {**fee_body, 'lines': [{'account_id': A['6-1200'], 'amount': 1000}]})
    fj = call('GET', f"/api/journals/{fee['id']}")
    check(fj['reconciled'] and [i['account_id'] for i in fj['items']] == [A['6-1200']], 'reconciled spend recategorised back')
    r = call('DELETE', f"/api/journals/{fee['id']}", expect_ok=False)
    check('reconciled' in r.get('error', ''), 'recategorised reconciled txn still cannot be deleted')
    check(call('GET', '/api/reports/tb')['debit'] == call('GET', '/api/reports/tb')['credit'], 'TB still balances after recategorising')
    check(call('GET', '/api/reports/bs?to=2026-10&count=1')['balanced'], 'BS still balances after recategorising')

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
    bs_r = call('GET', '/api/reports/bs?from=2026-07&to=2026-10')
    check(bs_r['months'] == ['2026-07', '2026-08', '2026-09', '2026-10'] and bs_r['net_assets'] == bs['net_assets'],
          f"BS from/to range gives one month-end column per month {bs_r['months']}")
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
    tiles = [w['opts']['ref']['account_id'] for w in dash['layout'] if w['type'] == 'tile']
    check(sorted(tiles) == sorted(a['id'] for a in call('GET', '/api/accounts') if a['subtype'] == 'bank' and not a['archived'])
          and 'profit_forecast' in [w['type'] for w in dash['layout']] and dash['alerts'] == [],
          'the default dashboard has a tile for each bank account and the standard visuals')
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
    jd = call('GET', '/api/dashboard')['widgets'][f'tile{joint}']
    check(jd['value'] == 45001 and jd['kind'] == 'bs', f"dashboard tile shows the user's share {jd['value']}")
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

    # ---- budgets: built from choices per account, then plain monthly figures edited by hand
    def ym_add(ym, n):
        y, m = int(ym[:4]), int(ym[5:7]) - 1 + n
        return f'{y + m // 12:04d}-{m % 12 + 1:02d}'

    def half_away(a, n):
        q, r = divmod(abs(a), n)
        v = q + (1 if 2 * r >= n else 0)
        return v if a >= 0 else -v

    td = date.today()
    fy = f'{td.year if td.month >= 7 else td.year - 1}-07'
    fy_months = [ym_add(fy, i) for i in range(12)]
    fy_end = f'{fy_months[-1]}-{calendar.monthrange(int(fy_months[-1][:4]), int(fy_months[-1][5:7]))[1]:02d}'
    h_to = ym_add(td.isoformat()[:7], -1)
    h_from = max(ym_add(h_to, -11), '2026-07')
    in_budget = call('POST', '/api/accounts', {'name': 'Budget only expense', 'type': 'expense'})['id']
    not_budgeted = call('POST', '/api/accounts', {'name': 'Never budgeted', 'type': 'expense'})['id']
    spread_wk = call('POST', '/api/accounts', {'name': 'Spread by weekday', 'type': 'expense'})['id']
    spread_fn = call('POST', '/api/accounts', {'name': 'Spread by fortnight', 'type': 'expense'})['id']
    quarterly = call('POST', '/api/accounts', {'name': 'Quarterly fee', 'type': 'expense'})['id']
    annual = call('POST', '/api/accounts', {'name': 'Annual subscription', 'type': 'expense'})['id']
    fy_first = f'{fy}-01'

    def spread_units(keep, start=fy_first, end=fy_end, step=1):
        """How many of the dates start..end (every `step` days) that `keep` accepts fall in each month."""
        d, out = date.fromisoformat(start), {}
        while d.isoformat() <= end:
            if keep(d):
                out[d.isoformat()[:7]] = out.get(d.isoformat()[:7], 0) + 1
            d = date.fromordinal(d.toordinal() + step)
        return [out.get(m, 0) for m in fy_months]

    def shared(vals, amount, units):
        """Months add up to the total exactly and each is within a cent of its fair share."""
        tot = sum(units)
        return sum(vals) == amount and all(abs(v * tot - amount * u) <= tot for v, u in zip(vals, units))

    bld = call('GET', '/api/budgets/builder')
    check(bld['fy'] == fy and bld['months'] == fy_months, f"builder defaults to this financial year {bld['fy']}")
    check(bld['history'] and bld['history']['from'] == h_from and bld['history']['to'] == h_to,
          f"history is the last 12 full months since the books started {bld['history']}")
    check(bld['prior'] is None, 'first budget has no earlier choices')
    hm = [h_from]
    while hm[-1] < h_to:
        hm.append(ym_add(hm[-1], 1))
    pl_h = call('GET', f'/api/reports/pl?from={h_from}&to={h_to}')
    actual = {r['account_id']: r['values'] for s in pl_h['sections'] for r in s['rows']}
    ba = {a['account_id']: a for a in bld['accounts']}
    ok_hist = True
    for aid, vals in actual.items():
        if aid not in ba:
            continue
        exp_last = vals[-1]  # the window's final month, even when it is $0
        a = ba[aid]
        if (a['average'] != half_away(sum(vals), len(hm)) or a['last'] != exp_last
                or a['last_month'] != h_to or a['method'] != 'average'):
            ok_hist = False
            print('  history mismatch', a, vals)
    check(ok_hist and actual, 'builder average and last month match the P&L for the history window (shared cut included)')
    check(all(a['method'] == 'none' for a in bld['accounts'] if not a['has_history']), 'accounts with no history default to not budgeted')
    check(all(a['type'] in ('income', 'cos', 'expense') for a in bld['accounts']), 'builder only offers P&L accounts')

    def occurrences(first, step):
        d, out = date.fromisoformat(first), {}
        while d.isoformat() <= fy_end:
            out[d.isoformat()[:7]] = out.get(d.isoformat()[:7], 0) + 1
            d = date.fromordinal(d.toordinal() + step)
        return [out.get(m, 0) for m in fy_months]

    wk_first, fn_first = f'{fy[:4]}-07-03', f'{fy[:4]}-07-10'
    rent_cfg = [{'freq': 'monthly', 'amount': 10000, 'from': fy_months[0], 'to': fy_months[3]},
                {'freq': 'monthly', 'amount': 11000, 'from': fy_months[4]}]
    manual = [100 * (i + 1) for i in range(12)]
    choose = [
        {'account_id': A['4-1000'], 'method': 'average'},
        {'account_id': A['4-2000'], 'method': 'last'},
        {'account_id': A['6-1600'], 'method': 'schedule', 'config': rent_cfg},
        {'account_id': A['6-1300'], 'method': 'schedule', 'config': [{'freq': 'weekly', 'amount': 2500, 'first': wk_first}]},
        {'account_id': A['6-1200'], 'method': 'schedule', 'config': [{'freq': 'fortnightly', 'amount': 5000, 'first': fn_first}]},
        {'account_id': A['6-1100'], 'method': 'manual', 'config': {'amounts': manual}},
        {'account_id': in_budget, 'method': 'manual', 'config': {'amounts': [500] * 12}},
        {'account_id': not_budgeted, 'method': 'none'},
        {'account_id': A['6-1700'], 'method': 'schedule', 'config': [
            {'freq': 'yearly', 'amount': 100000},
            {'freq': 'yearly', 'amount': -1000, 'from': fy_months[0], 'to': fy_months[2]}]},
        {'account_id': spread_wk, 'method': 'schedule', 'config': [{'freq': 'yearly', 'amount': 520000, 'spread': 'weekly', 'weekday': 0}]},
        {'account_id': spread_fn, 'method': 'schedule', 'config': [
            {'freq': 'yearly', 'amount': 260000, 'spread': 'fortnightly', 'start': fn_first, 'end': fy_end}]},
        {'account_id': quarterly, 'method': 'schedule', 'config': [
            {'freq': 'quarterly', 'amount': 30000, 'from': ym_add(fy, -1)},
            {'freq': 'quarterly', 'amount': 500, 'from': fy_months[0], 'to': fy_months[6]},
            {'freq': 'yearly', 'amount': 100001, 'spread': 'quarterly', 'from': fy_months[1], 'to': fy_months[11]}]},
        {'account_id': annual, 'method': 'schedule', 'config': [
            {'freq': 'annual', 'amount': 19900, 'from': fy_months[8]},
            {'freq': 'monthly', 'amount': 1000, 'from': fy_months[0], 'to': fy_months[11]}]},
    ]
    pv = call('POST', '/api/budgets/preview', {'fy': fy, 'accounts': choose})
    pa = {a['account_id']: a for a in pv['accounts']}
    check(pa[A['4-1000']]['values'] == [ba[A['4-1000']]['average']] * 12, 'average puts the monthly average in every month')
    check(pa[A['4-2000']]['values'] == [ba[A['4-2000']]['last']] * 12, 'last month repeats the last month of actuals')
    check(pa[A['6-1600']]['values'] == [10000] * 4 + [11000] * 8, 'monthly amounts can change part way through the year')
    wk = occurrences(wk_first, 7)
    check(pa[A['6-1300']]['values'] == [2500 * n for n in wk] and sum(wk) in (52, 53) and set(wk) == {4, 5},
          f'weekly amounts land in the month they happen ({wk})')
    fn = occurrences(fn_first, 14)
    check(pa[A['6-1200']]['values'] == [5000 * n for n in fn] and sum(fn) in (26, 27), f'fortnightly amounts ({fn})')
    check(pa[A['6-1100']]['values'] == manual, 'typed monthly amounts are used as entered')
    check(pa[not_budgeted]['values'] == [0] * 12, 'not budgeted is all zeros')
    check(pa[A['6-1700']]['values'] == [8000, 8001, 8001, 8334] + [8333] * 8 and pa[A['6-1700']]['total'] == 99000,
          f"a yearly total is spread evenly over its months and adds up exactly ({pa[A['6-1700']]['values']})")
    mondays = spread_units(lambda d: d.weekday() == 0)
    check(sum(mondays) in (52, 53) and shared(pa[spread_wk]['values'], 520000, mondays),
          f"a yearly total spread by weekday follows the Mondays in each month ({pa[spread_wk]['values']}, {mondays})")
    check(pa[spread_wk]['config'][0] == {'freq': 'yearly', 'amount': 520000, 'spread': 'weekly', 'weekday': 0,
                                         'start': fy_first, 'end': fy_end},
          f"weekday spread dates default to the whole financial year ({pa[spread_wk]['config']})")
    fns = spread_units(lambda d: True, fn_first, fy_end, 14)
    check(fns == occurrences(fn_first, 14) and shared(pa[spread_fn]['values'], 260000, fns),
          f"a yearly total spread by fortnight follows the fortnights from the start date ({pa[spread_fn]['values']})")

    q_want = [0] * 12
    for k in (2, 5, 8, 11):
        q_want[k] += 30000  # started the month before the year: its rhythm carries on
    for k in (0, 3, 6):
        q_want[k] += 500    # an end month stops it
    for k, v in zip((1, 4, 7, 10), (25001, 25000, 25000, 25000)):
        q_want[k] += v      # a total by quarter: shared over its quarters, leftover cents first
    check(pa[quarterly]['values'] == q_want and pa[quarterly]['total'] == 120000 + 1500 + 100001,
          f"quarterly amounts and a total spread by quarter ({pa[quarterly]['values']})")
    check(pa[quarterly]['config'][0] == {'freq': 'quarterly', 'amount': 30000, 'from': ym_add(fy, -1)},
          f"a quarterly amount with no end month keeps going ({pa[quarterly]['config'][0]})")

    a_want = [1000] * 12
    a_want[8] += 19900
    check(pa[annual]['values'] == a_want and pa[annual]['total'] == 12000 + 19900,
          f"an annual amount lands once, in its month ({pa[annual]['values']})")

    def spread_preview(*segs):
        res = call('POST', '/api/budgets/preview', {'fy': fy, 'accounts': [
            {'account_id': in_budget, 'method': 'schedule', 'config': list(segs)}]})['accounts'][0]
        return res.get('error') or res['values']

    days = spread_units(lambda d: True)
    v = spread_preview({'freq': 'yearly', 'amount': 365000, 'spread': 'daily', 'days': 'all'})
    check(sum(days) in (365, 366) and shared(v, 365000, days), f'a yearly total spread by day follows the days in each month ({v})')
    wdays = spread_units(lambda d: d.weekday() < 5)
    v = spread_preview({'freq': 'yearly', 'amount': -123457, 'spread': 'daily', 'days': 'weekdays'})
    check(shared(v, -123457, wdays), f'spread by weekdays only (Mon-Fri), negative totals add up too ({v})')
    part_s, part_e = f'{fy_months[2]}-10', f'{fy_months[4]}-20'
    fri = spread_units(lambda d: d.weekday() == 4, part_s, part_e)
    v = spread_preview({'freq': 'yearly', 'amount': 100000, 'spread': 'weekly', 'weekday': 4, 'start': part_s, 'end': part_e})
    check(shared(v, 100000, fri) and v[0] == v[1] == v[5] == 0 and v[2] > 0,
          f'a spread between two dates leaves the other months empty ({v})')
    v = spread_preview({'freq': 'yearly', 'amount': 1200, 'spread': 'monthly', 'from': fy_months[0], 'to': fy_months[11]})
    check(v == [100] * 12, f'an even monthly spread still divides by the months ({v})')
    one_day = f'{fy_months[0]}-08'
    other_wd = (date.fromisoformat(one_day).weekday() + 1) % 7
    for seg, want, what in [
        ({'spread': 'weekly', 'weekday': other_wd, 'start': one_day, 'end': one_day}, 'there are no ', 'no matching weekday'),
        ({'spread': 'daily', 'days': 'all', 'start': f'{fy_months[3]}-01', 'end': f'{fy_months[2]}-01'}, 'before the start', 'end before start'),
        ({'spread': 'daily', 'days': 'all', 'start': f'{int(fy[:4]) - 1}-12-01'}, 'inside this financial year', 'start outside the year'),
        ({'spread': 'hourly'}, 'how to spread', 'unknown spread'),
        ({'spread': 'quarterly', 'from': fy_months[5], 'to': fy_months[2]}, 'before the "from"', 'quarters to before from'),
        ({'spread': 'weekly'}, 'day of the week', 'missing weekday'),
        ({'spread': 'daily', 'days': 'some'}, 'every day or weekdays', 'bad day count')]:
        err = spread_preview({'freq': 'yearly', 'amount': 100, **seg})
        check(isinstance(err, str) and want in err, f'spread total refuses {what} ({err})')
    for seg, want, what in [
        ({'from': ym_add(fy, 12)}, 'after the end', 'a start after the year'),
        ({'from': fy_months[4], 'to': fy_months[2]}, 'before the start month', 'an end before the start'),
        ({'from': ym_add(fy, -6), 'to': ym_add(fy, -1)}, 'none of its quarters', 'quarters all before the year')]:
        err = spread_preview({'freq': 'quarterly', 'amount': 100, **seg})
        check(isinstance(err, str) and want in err, f'quarterly amount refuses {what} ({err})')
    for frm, what in [(ym_add(fy, -1), 'before'), (ym_add(fy, 12), 'after')]:
        err = spread_preview({'freq': 'annual', 'amount': 100, 'from': frm})
        check(isinstance(err, str) and 'inside this financial year' in err, f'an annual amount refuses a month {what} the year ({err})')
    exp_np = [sum(pa[a]['values'][i] * (1 if pa[a]['type'] == 'income' else -1) for a in pa) for i in range(12)]
    check(pv['totals']['net_profit'] == exp_np, 'preview totals add up')
    bad = call('POST', '/api/budgets/preview', {'fy': fy, 'accounts': [
        {'account_id': A['6-1600'], 'method': 'schedule', 'config': [{'freq': 'monthly', 'amount': 1, 'from': fy_months[5], 'to': fy_months[2]}]},
        {'account_id': A['6-1300'], 'method': 'schedule', 'config': [{'freq': 'weekly', 'amount': 1}]}]})
    check(all(a.get('error') for a in bad['accounts']), 'preview reports a problem per account without failing')
    r = call('POST', '/api/budgets', {'fy': fy, 'accounts': [{'account_id': A['6-1300'], 'method': 'schedule',
                                                              'config': [{'freq': 'weekly', 'amount': 1}]}]}, expect_ok=False)
    check('first date' in r.get('error', '') and 'Insurance' in r.get('error', ''), 'saving refuses a bad setup and names the account')
    r = call('POST', '/api/budgets', {'fy': fy, 'accounts': [{'account_id': A['1-2000'], 'method': 'average'}]}, expect_ok=False)
    check('income' in r.get('error', ''), 'balance sheet accounts cannot be budgeted')
    r = call('POST', '/api/budgets', {'fy': ym_add(fy, 1), 'accounts': choose}, expect_ok=False)
    check('start of a financial year' in r.get('error', ''), 'a budget must start at the start of a financial year')

    bid = call('POST', '/api/budgets', {'fy': fy, 'accounts': choose})['id']
    r = call('POST', '/api/budgets', {'fy': fy, 'accounts': choose}, expect_ok=False)
    check('already a budget' in r.get('error', ''), 'only one budget per financial year')
    r = call('GET', f'/api/budgets/builder?fy={fy}', expect_ok=False)
    check('already a budget' in r.get('error', ''), 'builder refuses a year that already has a budget')
    bud = call('GET', f'/api/budgets/{bid}')
    rows_ = {r['account_id']: r for s in bud['sections'] for r in s['rows']}
    check(rows_[A['6-1600']]['values'] == [10000] * 4 + [11000] * 8 and rows_[A['6-1600']]['method'] == 'schedule',
          'built budget holds the monthly figures and the method used')
    check(not_budgeted not in rows_, 'not budgeted accounts are left off the budget')
    check(bud['net_profit'] == exp_np, 'built budget matches the preview')
    lst = call('GET', '/api/budgets')
    check(len(lst) == 1 and lst[0]['net_profit'] == sum(exp_np), 'budget list shows the yearly net profit')

    # hand edits change the figures only, never the stored choices
    upd = call('PUT', f'/api/budgets/{bid}', {'amounts': [
        {'account_id': A['6-1600'], 'month': fy_months[0], 'amount': 99999},
        {'account_id': A['6-1100'], 'month': fy_months[1], 'amount': 0},
        {'account_id': A['6-1900'], 'month': fy_months[2], 'amount': 4200}]})
    urows = {r['account_id']: r for s in upd['sections'] for r in s['rows']}
    check(urows[A['6-1600']]['values'][0] == 99999 and urows[A['6-1100']]['values'][1] == 0,
          'budget figures can be edited by hand')
    check(urows[A['6-1900']]['values'][2] == 4200, 'an account can be added to a built budget by hand')
    r = call('PUT', f'/api/budgets/{bid}', {'amounts': [{'account_id': A['6-1600'], 'month': ym_add(fy, 12), 'amount': 1}]}, expect_ok=False)
    check('not in this budget' in r.get('error', ''), 'edits must be inside the budget year')
    call('PUT', f'/api/budgets/{bid}', {'name': 'My first budget'})
    check(call('GET', f'/api/budgets/{bid}')['name'] == 'My first budget', 'budget can be renamed')

    # next year's builder starts from this year's choices, moved forward a year
    nfy = ym_add(fy, 12)
    nb = call('GET', f'/api/budgets/builder?fy={nfy}')
    na_ = {a['account_id']: a for a in nb['accounts']}
    check(nb['prior'] and nb['prior']['id'] == bid, 'next year knows the earlier budget')
    check(na_[A['4-2000']]['method'] == 'last' and na_[A['4-1000']]['method'] == 'average', 'average / last month choices carry forward')
    check(na_[A['6-1600']]['config'] == [{'freq': 'monthly', 'amount': 10000, 'from': nfy, 'to': ym_add(nfy, 3)},
                                         {'freq': 'monthly', 'amount': 11000, 'from': ym_add(nfy, 4), 'to': ym_add(nfy, 11)}],
          f"monthly amounts move forward a year, not the hand edits ({na_[A['6-1600']]['config']})")
    check(na_[A['6-1700']]['config'][1] == {'freq': 'yearly', 'amount': -1000, 'from': nfy, 'to': ym_add(nfy, 2)},
          'yearly totals move forward a year')
    wk2 = date.fromisoformat(na_[A['6-1300']]['config'][0]['first'])
    wk_target = date(int(wk_first[:4]) + 1, 7, 3)
    check(wk2.weekday() == date.fromisoformat(wk_first).weekday() and 0 <= (wk2 - wk_target).days < 7,
          f'weekly amounts keep their weekday next year ({wk2})')
    check(na_[A['6-1100']]['method'] == 'manual' and na_[A['6-1100']]['config']['amounts'] == manual,
          'typed amounts carry forward as typed, not as edited')
    check(na_[not_budgeted]['method'] == 'none' and na_[not_budgeted]['from_prior'], 'not budgeted carries forward')
    check(all(a['from_prior'] for a in nb['accounts'] if a['account_id'] in pa), 'carried choices are flagged')
    nfy_end = f'{ym_add(nfy, 11)}-{calendar.monthrange(int(ym_add(nfy, 11)[:4]), int(ym_add(nfy, 11)[5:7]))[1]:02d}'
    check(na_[spread_wk]['config'] == [{'freq': 'yearly', 'amount': 520000, 'spread': 'weekly', 'weekday': 0,
                                        'start': f'{nfy}-01', 'end': nfy_end}],
          f"a weekday spread moves forward to next year's dates ({na_[spread_wk]['config']})")
    fn2 = na_[spread_fn]['config'][0]
    fn_target = date(int(fn_first[:4]) + 1, 7, 10)
    check(fn2['spread'] == 'fortnightly' and fn2['end'] == nfy_end and (date.fromisoformat(fn2['start']) - date.fromisoformat(fn_first)).days % 14 == 0
          and 0 <= (date.fromisoformat(fn2['start']) - fn_target).days < 14,
          f'a fortnight spread keeps its rhythm next year ({fn2})')
    check(na_[quarterly]['config'] == [{'freq': 'quarterly', 'amount': 30000, 'from': ym_add(nfy, -1)},
                                       {'freq': 'quarterly', 'amount': 500, 'from': nfy, 'to': ym_add(nfy, 6)},
                                       {'freq': 'yearly', 'amount': 100001, 'spread': 'quarterly', 'from': ym_add(nfy, 1), 'to': ym_add(nfy, 11)}],
          f"quarterly amounts move forward a year and keep their rhythm ({na_[quarterly]['config']})")
    check(na_[annual]['config'][0] == {'freq': 'annual', 'amount': 19900, 'from': ym_add(nfy, 8)},
          f"an annual amount lands in the same month next year ({na_[annual]['config']})")
    nb_prev = call('POST', '/api/budgets/preview', {'fy': nfy, 'accounts': [
        {'account_id': a['account_id'], 'method': a['method'], 'config': a['config']} for a in nb['accounts']]})
    check(not [a for a in nb_prev['accounts'] if a.get('error')],
          f"next year's carried spreads are valid ({[a for a in nb_prev['accounts'] if a.get('error')]})")

    # a built budget can be opened in the builder again: saved choices, hand edits kept as typed months
    before = call('GET', f'/api/budgets/{bid}')
    before_vals = {r['account_id']: r['values'] for sec in before['sections'] for r in sec['rows']}
    rb = call('GET', f'/api/budgets/builder?budget={bid}')
    ra = {a['account_id']: a for a in rb['accounts']}
    check(rb['budget_id'] == bid and rb['fy'] == fy and rb['name'] == 'My first budget', 'rebuild opens on the budget itself')
    check(ra[A['6-1600']]['edited'] and ra[A['6-1600']]['method'] == 'manual'
          and ra[A['6-1600']]['config']['amounts'] == before_vals[A['6-1600']],
          'an account edited by hand starts as typed months holding its figures')
    check(ra[A['6-1900']]['edited'] and ra[A['6-1900']]['config']['amounts'] == before_vals[A['6-1900']],
          'an account added by hand starts as typed months too')
    check(not ra[A['6-1300']]['edited'] and ra[A['6-1300']]['method'] == 'schedule' and ra[A['6-1300']]['config'] == pa[A['6-1300']]['config'],
          'an untouched account starts with the choice it was built with')
    check(not ra[A['4-1000']]['edited'] and ra[A['4-1000']]['method'] == 'average', 'average is not mistaken for a hand edit')
    check(not ra[spread_wk]['edited'] and not ra[quarterly]['edited'], 'spread and quarterly choices are not mistaken for hand edits')
    same = [{'account_id': a['account_id'], 'method': a['method'], 'config': a['config']} for a in rb['accounts']]
    call('PUT', f'/api/budgets/{bid}/rebuild', {'name': 'My first budget', 'accounts': same})
    after = call('GET', f'/api/budgets/{bid}')
    check({r['account_id']: r['values'] for sec in after['sections'] for r in sec['rows']} == before_vals,
          'rebuilding without changes keeps every figure')
    same = [x if x['account_id'] != A['6-1300'] else
            {'account_id': A['6-1300'], 'method': 'schedule', 'config': [{'freq': 'monthly', 'amount': 7000}]} for x in same]
    call('PUT', f'/api/budgets/{bid}/rebuild', {'name': 'Rebuilt budget', 'accounts': same})
    after = call('GET', f'/api/budgets/{bid}')
    arows = {r['account_id']: r for sec in after['sections'] for r in sec['rows']}
    check(after['name'] == 'Rebuilt budget' and arows[A['6-1300']]['values'] == [7000] * 12 and arows[A['6-1600']]['values'] == before_vals[A['6-1600']],
          'rebuilding replaces the figures the builder changed and keeps the rest')
    rb2 = {a['account_id']: a for a in call('GET', f'/api/budgets/builder?budget={bid}')['accounts']}
    check(rb2[A['6-1300']]['config'] == [{'freq': 'monthly', 'amount': 7000, 'from': fy_months[0], 'to': fy_months[11]}]
          and not rb2[A['6-1300']]['edited'], 'rebuilding stores the new choices')
    r = call('PUT', f'/api/budgets/{bid}/rebuild', {'accounts': [{'account_id': A['6-1300'], 'method': 'schedule', 'config': [{'freq': 'weekly', 'amount': 1}]}]}, expect_ok=False)
    check('first date' in r.get('error', '') and call('GET', f'/api/budgets/{bid}')['name'] == 'Rebuilt budget',
          'a bad rebuild changes nothing')
    call('PUT', f'/api/budgets/{bid}/rebuild', {'name': 'My first budget', 'accounts': [
        {'account_id': a['account_id'], 'method': a['method'], 'config': a['config']} for a in rb['accounts']]})
    check({r['account_id']: r['values'] for sec in call('GET', f'/api/budgets/{bid}')['sections'] for r in sec['rows']} == before_vals,
          'rebuilding back to the earlier choices restores the figures')

    # accounts in a budget can't be deleted; one only marked "not budgeted" can
    r = call('DELETE', f'/api/accounts/{in_budget}', expect_ok=False)
    check('budget' in r.get('error', ''), 'an account in a budget cannot be deleted')
    call('DELETE', f'/api/accounts/{not_budgeted}')
    check(not_budgeted not in [a['id'] for a in call('GET', '/api/accounts')], 'an unused account marked not budgeted can be deleted')

    # budget vs actual on the P&L: only when every month has a budget; actual figures never change
    rng = f'from={fy}&to={fy_months[-1]}'
    plain = call('GET', f'/api/reports/pl?{rng}')
    check(plain['budget_cover']['available'] and plain['budget_cover']['budgets'][0]['id'] == bid,
          'P&L says a budget covers the financial year')
    check('net_profit_budget' not in plain, 'plain P&L carries no budget figures')
    bva = call('GET', f'/api/reports/pl?{rng}&budget=1')
    cur = call('GET', f'/api/budgets/{bid}')
    want = {r['account_id']: r for s in cur['sections'] for r in s['rows']}
    got = {r['account_id']: r for s in bva['sections'] for r in s['rows']}
    check(all(got[a]['budget'] == w['values'] for a, w in want.items() if any(w['values'])),
          'budget vs actual shows each account\'s budget by month')
    check(bva['net_profit_budget'] == cur['net_profit'] and bva['net_profit_budget_total'] == cur['net_profit_total'],
          'budget vs actual net profit budget matches the budget')
    check(bva['net_profit'] == plain['net_profit'] and bva['net_profit_total'] == plain['net_profit_total'],
          'budget vs actual leaves the actual figures unchanged')
    check(in_budget in got and in_budget not in {r['account_id'] for s in plain['sections'] for r in s['rows']},
          'a budgeted account with no actuals shows only in budget vs actual')
    check(all(s['budget_total'] == sum(s['budget_totals']) for s in bva['sections']), 'section budget totals add up')
    one_m = call('GET', f'/api/reports/pl?from={fy_months[2]}&to={fy_months[2]}&budget=1')
    check(one_m['net_profit_budget'] == [cur['net_profit'][2]], 'a single month compares with that month\'s budget')
    span = call('GET', f'/api/reports/pl?from={fy_months[10]}&to={ym_add(fy, 13)}')
    check(not span['budget_cover']['available'] and span['budget_cover']['missing'] == [ym_add(fy, 12), ym_add(fy, 13)],
          f"months without a budget are listed ({span['budget_cover']})")
    r = call('GET', f'/api/reports/pl?from={fy_months[10]}&to={ym_add(fy, 13)}&budget=1', expect_ok=False)
    check('No budget covers' in r.get('error', ''), 'budget vs actual is refused when a month has no budget')

    # projects: named sets of accounts that filter the P&L and Balance Sheet; an account can be in several
    tdi = td.isoformat()
    bank = A['1-1100']
    rent = call('POST', '/api/accounts', {'name': 'Rent Income', 'type': 'income'})['id']
    prop_exp = call('POST', '/api/accounts', {'name': 'Property Expenses', 'type': 'expense'})['id']
    prop = call('POST', '/api/accounts', {'name': 'Investment Property', 'type': 'asset'})['id']
    pl_all0 = call('GET', f'/api/reports/pl?{rng}')
    rental = call('POST', '/api/projects', {'name': 'Rental', 'account_ids': [rent, prop_exp, prop]})['id']
    shared_p = call('POST', '/api/projects', {'name': 'Shared costs', 'account_ids': [prop_exp, A['6-1600']]})['id']
    r = call('POST', '/api/projects', {'name': '  rental ', 'account_ids': []}, expect_ok=False)
    check('already a project' in r.get('error', ''), 'project names must be unique ignoring case and spacing')
    r = call('POST', '/api/projects', {'name': ''}, expect_ok=False)
    check('name' in r.get('error', ''), 'a project needs a name')
    projs = {p['name']: p for p in call('GET', '/api/state')['projects']}
    check(projs['Rental']['account_ids'] == sorted([rent, prop_exp, prop]), 'project keeps its accounts')
    check(prop_exp in projs['Shared costs']['account_ids'] and prop_exp in projs['Rental']['account_ids'],
          'one account can be in more than one project')
    call('POST', '/api/txn/receive', {'date': tdi, 'bank_account_id': bank, 'memo': 'Rent',
                                      'lines': [{'account_id': rent, 'amount': 100000}]})
    call('POST', '/api/txn/spend', {'date': tdi, 'bank_account_id': bank, 'memo': 'Repairs',
                                    'lines': [{'account_id': prop_exp, 'amount': 30000}]})
    call('POST', '/api/txn/spend', {'date': tdi, 'bank_account_id': bank, 'memo': 'Not in a project',
                                    'lines': [{'account_id': A['6-1200'], 'amount': 5000}]})
    call('POST', '/api/txn/spend', {'date': tdi, 'bank_account_id': bank, 'memo': 'Buy property',
                                    'lines': [{'account_id': prop, 'amount': 500000}]})
    pl_all = call('GET', f'/api/reports/pl?{rng}')
    check(pl_all['project'] is None and pl_all['net_profit_total'] == pl_all0['net_profit_total'] + 100000 - 30000 - 5000,
          'unfiltered P&L still shows every account')
    pl_r = call('GET', f'/api/reports/pl?{rng}&project={rental}')
    ids_r = {x['account_id'] for s in pl_r['sections'] for x in s['rows']}
    check(pl_r['project'] == {'id': rental, 'name': 'Rental'} and ids_r == {rent, prop_exp},
          f'project P&L shows only its accounts ({ids_r})')
    check(pl_r['net_profit_total'] == 70000, f"project net profit is its income less its costs ({pl_r['net_profit_total']})")
    pl_s = call('GET', f'/api/reports/pl?{rng}&project={shared_p}')
    exp_6_1600 = next((x['total'] for s in pl_all['sections'] for x in s['rows'] if x['account_id'] == A['6-1600']), 0)
    check({x['account_id'] for s in pl_s['sections'] for x in s['rows']} <= {prop_exp, A['6-1600']}
          and pl_s['net_profit_total'] == -30000 - exp_6_1600, 'an account in two projects counts in both')
    bva_s = call('GET', f'/api/reports/pl?{rng}&project={shared_p}&budget=1')
    bud_6_1600 = next(x['budget_total'] for s in bva['sections'] for x in s['rows'] if x['account_id'] == A['6-1600'])
    check({x['account_id'] for s in bva_s['sections'] for x in s['rows']} <= {prop_exp, A['6-1600']}
          and bva_s['net_profit_budget_total'] == -bud_6_1600, 'budget vs actual for a project uses only its budgets')
    bs_r = call('GET', f'/api/reports/bs?to={tdi[:7]}&count=1&project={rental}')
    a_rows = bs_r['sections'][0]['rows']
    eq_rows = {x['name']: x['values'] for x in bs_r['sections'][2]['rows']}
    check([x['account_id'] for x in a_rows] == [prop] and a_rows[0]['values'] == [500000] and not bs_r['sections'][1]['rows'],
          'project balance sheet shows only its accounts')
    check(eq_rows == {'Project earnings': [70000]}, f'project earnings are the project profit this year ({eq_rows})')
    check(bs_r['balanced'] is None and bs_r['project']['name'] == 'Rental', 'project balance sheet is not checked for balance')
    r = call('GET', f'/api/reports/pl?{rng}&project=99999', expect_ok=False)
    check('Project not found' in r.get('error', ''), 'an unknown project is refused')
    # the account form's tick-boxes replace that account's projects
    call('PUT', f'/api/accounts/{prop_exp}', {'name': 'Property Expenses', 'type': 'expense', 'project_ids': [shared_p]})
    projs = {p['name']: p for p in call('GET', '/api/state')['projects']}
    check(prop_exp not in projs['Rental']['account_ids'] and prop_exp in projs['Shared costs']['account_ids'],
          'account form sets which projects an account is in')
    call('PUT', f'/api/accounts/{prop_exp}', {'name': 'Property Expenses'})
    check(prop_exp in {p['name']: p for p in call('GET', '/api/state')['projects']}['Shared costs']['account_ids'],
          'saving an account without project_ids leaves its projects alone')
    agent = call('POST', '/api/accounts', {'name': 'Letting Agent Fees', 'type': 'expense', 'project_ids': [rental]})['id']
    check(agent in {p['name']: p for p in call('GET', '/api/state')['projects']}['Rental']['account_ids'],
          'a new account can join a project')
    r = call('PUT', f'/api/accounts/{agent}', {'project_ids': [99999]}, expect_ok=False)
    check('Project not found' in r.get('error', ''), 'an unknown project id is refused on an account')
    call('DELETE', f'/api/accounts/{agent}')
    check(agent not in {p['name']: p for p in call('GET', '/api/state')['projects']}['Rental']['account_ids'],
          'deleting an account takes it out of its projects')
    call('PUT', f'/api/projects/{rental}', {'name': 'Rental property'})
    projs = {p['name']: p for p in call('GET', '/api/state')['projects']}
    check('Rental property' in projs and projs['Rental property']['account_ids'] == sorted([rent, prop]),
          'renaming a project keeps its accounts')
    call('DELETE', f'/api/projects/{shared_p}')
    check([p['name'] for p in call('GET', '/api/state')['projects']] == ['Rental property']
          and call('GET', f'/api/reports/pl?{rng}')['net_profit_total'] == pl_all['net_profit_total'],
          'deleting a project leaves accounts and figures alone')
    acts = [a['action'] for a in call('GET', '/api/audit')]
    check(all(x in acts for x in ('project_create', 'project_update', 'project_delete')), 'project changes are audited')

    tb_ = call('GET', '/api/reports/tb')
    check(tb_['debit'] == tb_['credit'], 'TB still balances with a budget')
    check(call('GET', f'/api/reports/bs?to={td.isoformat()[:7]}&count=1')['balanced'], 'BS still balances with a budget')
    acts = [a['action'] for a in call('GET', '/api/audit')]
    check(all(x in acts for x in ('budget_create', 'budget_update', 'budget_rebuild')), 'budget changes are audited')
    call('DELETE', f'/api/budgets/{bid}')
    check(call('GET', '/api/budgets') == [] and call('GET', f'/api/budgets/builder?fy={nfy}')['prior'] is None,
          'deleting a budget removes it and its choices')
    check('budget_delete' in [a['action'] for a in call('GET', '/api/audit')], 'budget delete is audited')

    # ---------------------------------------------------------------- alerts
    T = date.today()
    t_iso = T.isoformat()
    oa = call('POST', '/api/accounts', {'name': 'Offset A', 'type': 'asset', 'subtype': 'bank'})['id']
    ob2 = call('POST', '/api/accounts', {'name': 'Offset B', 'type': 'asset', 'subtype': 'bank', 'share_pct': 50})['id']
    loan = call('POST', '/api/accounts', {'name': 'Home Loan', 'type': 'liability', 'subtype': 'other'})['id']
    asales = call('POST', '/api/accounts', {'name': 'Alert Sales', 'type': 'income', 'subtype': 'other'})['id']
    cap = A['3-1000']

    def gj(dr, cr, amt, d=t_iso):
        call('POST', '/api/txn/journal', {'date': d, 'memo': 'alert test', 'lines': [
            {'account_id': dr, 'debit': amt}, {'account_id': cr, 'credit': amt}]})

    def alert_count():
        return call('GET', '/api/alerts/count')['count']

    gj(oa, cap, 500000)        # Offset A $5,000
    gj(ob2, cap, 300000)       # Offset B $3,000 (half of it is someone else's)
    gj(cap, loan, 900000)      # Home loan $9,000 owed
    c0 = alert_count()
    low = call('POST', '/api/alerts', {'name': 'Low offset', 'op': 'lt', 'watch': [{'account_id': oa}], 'amount': 600000})
    check(low['status'] == 'triggered' and low['watch_value'] == 500000 and low['compare_value'] == 600000,
          f"a fixed-amount alert goes off when the balance is below it ({low['status']}, {low['watch_value']})")
    check(low['summary'] == 'Offset A goes below $6,000.00', f"alert summary reads plainly ({low['summary']})")
    check(alert_count() == c0 + 1, 'the alert count includes an alert that is going off')
    check(low['id'] in [a['id'] for a in call('GET', '/api/dashboard')['alerts']], 'the dashboard lists alerts going off')
    r = call('POST', '/api/alerts', {'name': 'No account', 'op': 'lt', 'watch': [], 'amount': 0}, expect_ok=False)
    check('account to watch' in r.get('error', ''), 'an alert needs an account to watch')
    r = call('POST', '/api/alerts', {'name': 'Bad op', 'op': 'zz', 'watch': [{'account_id': oa}]}, expect_ok=False)
    check('go off' in r.get('error', ''), 'an alert needs a valid condition')
    r = call('POST', '/api/alerts', {'name': ' low  OFFSET', 'op': 'gt', 'watch': [{'account_id': oa}]}, expect_ok=False)
    check('already an alert' in r.get('error', ''), 'alert names must be unique ignoring case and spacing')
    r = call('POST', '/api/alerts', {'name': 'Twice', 'op': 'gt', 'watch': [{'account_id': oa}, {'account_id': oa}]}, expect_ok=False)
    check('twice' in r.get('error', ''), 'an account cannot be in the same total twice')

    # offsets compared with a loan less $1,000; shared accounts count at their full balance
    off = call('POST', '/api/alerts', {'name': 'Offsets vs loan', 'op': 'lt', 'watch': [{'account_id': oa}, {'account_id': ob2}],
                                       'compare': [{'account_id': loan}], 'amount': -100000})
    check(off['watch_value'] == 800000, f"a shared bank account counts at its full balance ({off['watch_value']})")
    check(off['compare_value'] == 800000 and off['status'] == 'ok', f"loan less $1,000 is compared ({off['compare_value']}, {off['status']})")
    check(off['summary'] == 'Offset A + Offset B goes below Home Loan - $1,000.00', f"compare summary ({off['summary']})")
    off = call('PUT', f"/api/alerts/{off['id']}", {'name': 'Offsets vs loan', 'op': 'lte', 'watch': [{'account_id': oa}, {'account_id': ob2}],
                                                    'compare': [{'account_id': loan}], 'amount': -100000})
    check(off['status'] == 'triggered', '"is at or below" goes off when the totals are equal')
    mixed = call('POST', '/api/alerts/preview', {'op': 'eq', 'watch': [{'account_id': oa}, {'account_id': ob2, 'sign': -1}], 'amount': 200000})
    check(mixed['met'] and mixed['watch_value'] == 200000, 'a "minus" account is taken away from the total')
    n_alerts = len(call('GET', '/api/alerts'))
    pv = call('POST', '/api/alerts/preview', {'op': 'gt', 'watch': [{'account_id': oa}], 'amount': 0})
    check(pv['met'] and len(call('GET', '/api/alerts')) == n_alerts, 'preview shows the result without saving an alert')

    # "Got it" marks a triggered alert seen, but it keeps counting until the condition stops
    c1 = alert_count()
    seen = call('POST', f"/api/alerts/{low['id']}/dismiss")
    check(seen['status'] == 'seen' and alert_count() == c1, 'Got it marks the alert seen and it still counts')
    check(low['id'] in [a['id'] for a in call('GET', '/api/dashboard')['alerts']], 'a seen alert stays on the dashboard')
    r = call('POST', f"/api/alerts/{low['id']}/dismiss", expect_ok=False)
    check('nothing to mark' in r.get('error', ''), 'an alert already seen cannot be marked again')
    gj(oa, cap, 200000)
    check(call('GET', f"/api/alerts/{low['id']}")['status'] == 'ok' and
          low['id'] not in [a['id'] for a in call('GET', '/api/dashboard')['alerts']] and alert_count() < c1,
          'the alert is quiet and leaves the count once the balance recovers')
    gj(cap, oa, 200000)
    check(call('GET', f"/api/alerts/{low['id']}")['status'] == 'triggered' and alert_count() == c1,
          'the alert goes off again when the balance drops again')
    gj(oa, cap, 400000, (T + timedelta(days=5)).isoformat())
    check(call('GET', f"/api/alerts/{low['id']}")['watch_value'] == 500000, 'transactions dated after today are ignored')
    off_alert = call('PUT', f"/api/alerts/{low['id']}", {'name': 'Low offset', 'op': 'lt', 'watch': [{'account_id': oa}],
                                                         'amount': 600000, 'active': False})
    check(off_alert['status'] == 'off' and alert_count() == c1 - 1, 'a turned-off alert never counts')

    # income accounts use this financial year so far
    prev = date(T.year - (T.month == 1), (T.month - 2) % 12 + 1, 1)
    if prev.isoformat() > call('GET', '/api/state')['settings']['lock_date']:
        call('PUT', '/api/settings', {'fy_start_month': T.month})
        gj(cap, asales, 100000, prev.isoformat())
        gj(cap, asales, 250000)
        inc = call('POST', '/api/alerts', {'name': 'Income target', 'op': 'gte', 'watch': [{'account_id': asales}], 'amount': 300000})
        check(inc['watch_value'] == 250000 and inc['status'] == 'ok', f"income alerts use this financial year only ({inc['watch_value']})")
        call('PUT', '/api/settings', {'fy_start_month': 7})
    else:
        print('SKIP income financial-year alert check (previous month is locked)')

    # accounts used in an alert can't be deleted until the alert is changed or deleted
    spare_w = call('POST', '/api/accounts', {'name': 'Spare Watch', 'type': 'asset', 'subtype': 'other'})['id']
    sw = call('POST', '/api/alerts', {'name': 'Spare', 'op': 'gt', 'watch': [{'account_id': spare_w}], 'amount': 0})
    r = call('DELETE', f'/api/accounts/{spare_w}', expect_ok=False)
    check('used in the alert "Spare"' in r.get('error', ''), 'an account in an alert cannot be deleted')
    call('DELETE', f"/api/alerts/{sw['id']}")
    call('DELETE', f'/api/accounts/{spare_w}')
    check(spare_w not in [a['id'] for a in call('GET', '/api/accounts')], 'the account can be deleted once its alert is gone')
    acts = [a['action'] for a in call('GET', '/api/audit')]
    check(all(x in acts for x in ('alert_create', 'alert_update', 'alert_dismiss', 'alert_delete')), 'alert changes are audited')
    tb_ = call('GET', '/api/reports/tb')
    check(tb_['debit'] == tb_['credit'], 'TB still balances with alerts')
    check(call('GET', f'/api/reports/bs?to={t_iso[:7]}&count=1')['balanced'], 'BS still balances with alerts')

    # ---------------------------------------------------------------- dashboard (presentation only)
    T = date.today()
    t_iso, cur = T.isoformat(), T.isoformat()[:7]
    dfy = f'{T.year if T.month >= 7 else T.year - 1}-07'
    dfy_months = [ym_add(dfy, i) for i in range(12)]
    dgrp = call('POST', '/api/groups', {'name': 'Dash savings', 'type': 'asset'})['id']
    dsave1 = call('POST', '/api/accounts', {'name': 'Dash saver 1', 'type': 'asset', 'subtype': 'bank', 'group_id': dgrp})['id']
    dsave2 = call('POST', '/api/accounts', {'name': 'Dash saver 2', 'type': 'asset', 'subtype': 'other', 'group_id': dgrp})['id']
    dexp = call('POST', '/api/accounts', {'name': 'Dash expense', 'type': 'expense'})['id']
    dinc = call('POST', '/api/accounts', {'name': 'Dash income', 'type': 'income'})['id']
    call('POST', '/api/txn/transfer', {'date': t_iso, 'from_account_id': A['1-1100'], 'to_account_id': dsave1, 'amount': 50000})
    call('POST', '/api/txn/transfer', {'date': t_iso, 'from_account_id': dsave1, 'to_account_id': A['1-1100'], 'amount': 10000})
    call('POST', '/api/txn/spend', {'date': t_iso, 'bank_account_id': dsave1, 'lines': [{'account_id': dexp, 'amount': 1234}]})
    call('POST', '/api/txn/journal', {'date': t_iso, 'memo': 'Dash', 'lines': [
        {'account_id': dsave2, 'debit': 20000}, {'account_id': dinc, 'credit': 20000}]})
    dbid = call('POST', '/api/budgets', {'fy': dfy, 'accounts': [
        {'account_id': dinc, 'method': 'manual', 'config': {'amounts': [300000] * 12}},
        {'account_id': dexp, 'method': 'manual', 'config': {'amounts': [100000] * 12}}]})['id']
    rw = call('POST', '/api/recurring', {'name': 'Dash weekly', 'kind': 'spend', 'bank_account_id': A['1-1100'], 'frequency': 'weekly',
                                         'next_date': (T + timedelta(days=1)).isoformat(),
                                         'lines': [{'account_id': dexp, 'amount': 2500}]})
    layout = [
        {'id': 'g', 'type': 'tile', 'opts': {'ref': {'group_id': dgrp}}},
        {'id': 's1', 'type': 'tile', 'opts': {'ref': {'account_id': dsave1}}},
        {'id': 's2', 'type': 'tile', 'opts': {'ref': {'account_id': dsave2}}},
        {'id': 'inc', 'type': 'tile', 'opts': {'ref': {'account_id': dinc}, 'period': 'month'}},
        {'id': 'pf', 'type': 'profit_forecast'}, {'id': 'pb', 'type': 'profit_budget'},
        {'id': 'bu', 'type': 'budget_used', 'opts': {'mode': 'full_year'}},
        {'id': 'nd', 'type': 'next_days', 'opts': {'days': 14}},
        {'id': 'cal', 'type': 'calendar', 'opts': {'source': dsave1}}, {'id': 'calall', 'type': 'calendar'},
        {'id': 'goals', 'type': 'goals', 'opts': {'goals': [{'name': 'Rainy day', 'ref': {'group_id': dgrp}, 'target': 120000}]}},
        {'id': 'two', 'type': 'two_years', 'opts': {'ref': {'account_id': dexp}}},
        {'id': 'yoy', 'type': 'yoy', 'opts': {'mode': 'month', 'side': 'income'}},
        {'id': 'io', 'type': 'in_out'}, {'id': 'dl', 'type': 'dollar', 'opts': {'by': 'group'}}, {'id': 'nw', 'type': 'net_worth'}]
    for bad, why in [([{'id': 'x', 'type': 'pie'}], 'an unknown visual'),
                     ([{'id': 'x', 'type': 'tile', 'opts': {'ref': {'account_id': 999999}}}], 'a tile for an account that does not exist'),
                     ([{'id': 'x', 'type': 'tile'}], 'a tile with no account'),
                     ([{'id': 'x', 'type': 'yoy', 'opts': {'mode': 'week'}}], 'a choice that is not offered'),
                     ([{'id': 'x', 'type': 'in_out'}, {'id': 'x', 'type': 'net_worth'}], 'two visuals with the same id'),
                     ([{'id': 'x', 'type': 'calendar', 'opts': {'source': dexp}}], 'a calendar for an account that is not a bank account'),
                     ([{'id': 'x', 'type': 'goals', 'opts': {'goals': [{'name': 'G', 'ref': {'account_id': dsave1}, 'target': 0}]}}], 'a goal with no target'),
                     ([{'id': f'x{i}', 'type': 'in_out'} for i in range(41)], 'more than 40 visuals')]:
        check(call('PUT', '/api/dashboard/layout', {'layout': bad}, expect_ok=False).get('error'), f'dashboard refuses {why}')
    d = call('PUT', '/api/dashboard/layout', {'layout': layout})
    W = d['widgets']
    check([w['id'] for w in d['layout']] == [w['id'] for w in layout] and W['bu']['mode'] == 'full_year',
          'the dashboard layout saves in order with its choices')
    check(call('GET', '/api/dashboard')['layout'] == d['layout'], 'the saved layout is used from then on')
    check(W['s1']['value'] == 50000 - 10000 - 1234 and W['s2']['value'] == 20000 and W['g']['value'] == W['s1']['value'] + W['s2']['value'],
          f"a group tile adds up its accounts ({W['g']['value']})")
    check(W['g']['spark'][-1] == W['g']['value'] and len(W['g']['spark']) == 12, 'tiles carry a 12-month trend ending today')
    check(W['inc']['value'] == 20000 and W['inc']['kind'] == 'pl', 'an income tile shows this month so far')
    pf, pb = W['pf'], W['pb']
    later = dfy_months[dfy_months.index(cur) + 1:]
    exp_, ok = pf['actual'][-1], True
    for i, m in enumerate(later):
        exp_ += 300000 - 100000
        ok = ok and pf['forecast'][i] == exp_
    check(pf['has_budget'] and ok and len(pf['actual']) == dfy_months.index(cur) + 1,
          "profit forecast adds each later month's budgeted profit to the profit so far")
    check(pb['actual'] == pf['actual'] and pb['budget'][-1] == 12 * 200000, 'profit against budget adds up the budget for the year')
    bu = [r for r in W['bu']['rows'] if r['ref'] == {'account_id': dexp}]
    check(bu and bu[0]['budget'] == 1200000 and bu[0]['actual'] >= 1234, "budget used compares spending with the whole year's budget")
    bug = call('POST', '/api/groups', {'name': 'Dash budget group', 'type': 'expense'})['id']
    call('PUT', f'/api/accounts/{dexp}', {'name': 'Dash expense', 'type': 'expense', 'group_id': bug})
    bw = call('PUT', '/api/dashboard/layout', {'layout': [
        {'id': 'bg', 'type': 'budget_used', 'opts': {'mode': 'full_year', 'by': 'group'}},
        {'id': 'bt', 'type': 'budget_used', 'opts': {'mode': 'full_year', 'track': [{'account_id': dexp}, {'group_id': bug}]}}]})['widgets']
    check([r for r in bw['bg']['rows'] if r['name'] == 'Dash budget group' and r['budget'] == 1200000],
          'budget used can add up by group')
    check([r['name'] for r in bw['bt']['rows']] == ['Dash expense', 'Dash budget group'] and bw['bt']['chosen']
          and bw['bt']['rows'][1]['budget'] == 1200000, 'budget used shows the accounts and groups the user chose, in order')
    for bad, why in [({'track': [{'account_id': dinc}]}, 'an income account'),
                     ({'track': [{'account_id': dexp}] * 2}, 'the same account twice'),
                     ({'track': [{'account_id': dexp}] * 9}, 'more than 8 rows')]:
        check(call('PUT', '/api/dashboard/layout', {'layout': [{'id': 'b', 'type': 'budget_used', 'opts': bad}]}, expect_ok=False).get('error'),
              f'budget used refuses {why}')
    call('PUT', f'/api/accounts/{dexp}', {'name': 'Dash expense', 'type': 'expense', 'group_id': None})
    call('PUT', '/api/dashboard/layout', {'layout': layout})
    nd = [i for i in W['nd']['items'] if i['recurring_id'] == rw['id']]
    check(len(nd) == 2 and all(i['dir'] == 'out' and i['amount'] == 2500 for i in nd), f'coming up repeats a weekly item within 14 days ({len(nd)})')
    check(W['cal']['days'].get(t_iso) == 1234, f"the bank spending calendar leaves out transfers ({W['cal']['days'].get(t_iso)})")
    check(W['calall']['days'].get(t_iso, 0) >= 1234, 'the overall spending calendar counts expenses')
    g = W['goals']['goals'][0]
    check(g['value'] == W['g']['value'] and g['target'] == 120000, 'a savings goal tracks its group balance')
    check(W['two']['this'][-1] >= 1234 and len(W['two']['last']) == 12, 'same account, two years has both years')
    check(W['yoy']['rows'] and W['yoy']['side'] == 'income', 'this month vs last month lists income accounts')
    ch_ = [r['now'] - r['then'] for r in W['yoy']['rows']]
    check(ch_ == sorted(ch_, reverse=True), 'this month vs last month shows the biggest increase first')
    dcg = call('POST', '/api/groups', {'name': 'Dash costs', 'type': 'expense'})['id']
    dexp2 = call('POST', '/api/accounts', {'name': 'Dash expense 2', 'type': 'expense', 'group_id': dcg})['id']
    call('POST', '/api/txn/spend', {'date': t_iso, 'bank_account_id': A['1-1100'], 'lines': [{'account_id': dexp2, 'amount': 777}]})
    yg = call('PUT', '/api/dashboard/layout', {'layout': layout + [{'id': 'yg', 'type': 'yoy', 'opts': {'mode': 'month', 'side': 'spending', 'by': 'group'}}]})['widgets']['yg']
    check(yg['by'] == 'group' and any(r['name'] == 'Dash costs' and r['now'] >= 777 for r in yg['rows'])
          and not any(r['name'] == 'Dash expense 2' for r in yg['rows']), 'this month vs last month can add up by group')
    check(call('PUT', '/api/dashboard/layout', {'layout': [{'id': 'x', 'type': 'yoy', 'opts': {'by': 'team'}}]}, expect_ok=False).get('error'),
          'dashboard refuses an unknown accounts-or-groups choice')
    call('PUT', '/api/dashboard/layout', {'layout': layout})
    check(len(W['io']['months']) == 12 and W['io']['months'][-1] == cur, 'money in vs out covers the last 12 months')
    check(W['dl']['spent'] == sum(p_['amount'] for p_ in W['dl']['parts']), 'where each dollar went splits all spending')
    check(W['nw']['net'][-1] == W['nw']['assets'][-1] - W['nw']['liabilities'][-1], 'net worth is what you own minus what you owe')
    call('DELETE', f'/api/budgets/{dbid}')
    W = call('GET', '/api/dashboard')['widgets']
    check(W['pf']['forecast'] is None or not later, 'without a budget the forecast is left off')
    check('dashboard_layout' in [a['action'] for a in call('GET', '/api/audit')], 'dashboard changes are audited')
    call('DELETE', f"/api/recurring/{rw['id']}")
    tb_ = call('GET', '/api/reports/tb')
    check(tb_['debit'] == tb_['credit'], 'TB still balances with the dashboard')
    check(call('GET', f'/api/reports/bs?to={cur}&count=1')['balanced'], 'BS still balances with the dashboard')

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
