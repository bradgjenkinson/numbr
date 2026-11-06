"""End-to-end UI test in headless Chromium (needs: pip install playwright && playwright install chromium).
Walks setup, invoices, payments, recurring, CSV reconciliation and reports. Screenshots go to tests/screenshots/.
Run: python tests/test_ui.py"""
import os, subprocess, sys, time, tempfile, shutil
from datetime import date
from playwright.sync_api import sync_playwright

SP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SP)
SHOTS = os.path.join(SP, 'screenshots'); os.makedirs(SHOTS, exist_ok=True)
data = tempfile.mkdtemp()
env = dict(os.environ, LEDGER_DATA=data, LEDGER_PORT='8798')
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py'), '--no-browser'], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(1.2)
URL = 'http://localhost:8798/'
errors = []

def shot(page, name):
    page.screenshot(path=os.path.join(SHOTS, name + '.png'), full_page=True)

def toast_text(page):
    return page.locator('#toast').inner_text()

try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={'width': 1360, 'height': 900})
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(URL); page.wait_for_selector('#s-name')
        shot(page, '01-setup')
        page.fill('#s-name', 'Jenkinson Consulting')
        page.fill('#s-conv', '2026-07-01')
        page.click('#s-next'); page.wait_for_selector('.b-name')
        assert page.locator('.b-name').count() == 1 and page.locator('.b-name').first.input_value() == 'Bank 1'
        page.click('#b-add-cc'); page.wait_for_selector('.b-name >> nth=1')
        assert page.locator('.b-name').nth(1).input_value() == 'Credit Card 1'
        page.locator('.b-name').first.fill('Westpac Business One')
        page.click('#s-next'); page.wait_for_selector('.ob')
        # opening balances: bank 1 = 8000, AR = 2750, credit card 320, capital = rest
        def ob(code, v):
            aid = page.evaluate(f"S.accounts.find(a=>a.code=='{code}').id")
            page.fill(f'.ob[data-id="{aid}"]', v)
        ob('1-1100', '8,000'); ob('1-1300', '2750'); ob('2-1110', '320'); ob('3-1000', '10430')
        page.locator('.ob').first.blur()
        assert page.inner_text('#ob-hist').strip() == '0.00', page.inner_text('#ob-hist')
        shot(page, '02-opening')
        page.click('#ob-save'); page.wait_for_selector('#s-finish')
        # add historical invoice through the UI
        page.click('text=+ Add unpaid invoice'); page.wait_for_selector('#f-contact')
        page.select_option('#f-contact', '__new'); page.wait_for_selector('#qc-name')
        page.fill('#qc-name', 'Harbour Analytics'); page.click('#modal [data-x=yes]')
        page.wait_for_timeout(300)
        assert page.input_value('#f-date') == '2026-06-30'
        page.fill('#f-number', 'INV-0998')
        page.fill('#f-date', '2026-06-20')
        page.fill('[data-f=description]', 'June model build')
        aid = page.evaluate("S.accounts.find(a=>a.code=='4-1000').id")
        page.select_option('[data-f=account_id]', str(aid))
        page.fill('[data-f=amount]', '2750')
        assert not page.locator('#f-hist').is_hidden()
        page.click('#f-save'); page.wait_for_selector('#s-finish')
        assert 'Matches' in page.inner_text('main'), 'AR check'
        shot(page, '03-unpaid')
        page.click('#s-finish'); page.wait_for_selector('.stat')
        shot(page, '04-dashboard')

        # new invoice
        page.goto(URL + '#/sales/new'); page.wait_for_selector('#f-contact')
        cid = page.evaluate("S.contacts[0].id")
        page.select_option('#f-contact', str(cid))
        page.fill('#f-date', '2026-07-15')
        assert page.input_value('#f-due') == '2026-07-15'
        page.select_option('[data-f=account_id]', str(aid)); page.fill('[data-f=description]', 'July retainer'); page.fill('[data-f=amount]', '4400')
        page.click('.add-line'); page.locator('[data-f=description]').nth(1).fill('Workshop')
        page.locator('[data-f=account_id]').nth(1).select_option(str(aid)); page.locator('[data-f=amount]').nth(1).fill('600')
        assert page.inner_text('.tot') == '$5,000.00'
        page.click('#f-save'); page.wait_for_selector('text=INVOICE')
        shot(page, '05-invoice')
        # receive payment for the historical + part
        page.goto(URL + '#/receive'); page.wait_for_selector('#p-contact')
        page.select_option('#p-contact', str(cid)); page.wait_for_selector('.p-alloc')
        page.fill('#p-date', '2026-07-20'); page.fill('#p-amount', '3750')
        page.locator('#p-amount').dispatch_event('input')
        assert page.inner_text('#p-total') == '$3,750.00', page.inner_text('#p-total')
        shot(page, '06-receive')
        page.click('#p-save'); page.wait_for_selector('text=Applied to')

        # recurring spend: software sub monthly from 2026-07-05
        page.goto(URL + '#/recurring/new'); page.wait_for_selector('#r-name')
        page.fill('#r-name', 'Adobe subscription'); page.select_option('#r-kind', 'spend')
        page.fill('#r-next', '2026-07-05')
        sw = page.evaluate("S.accounts.find(a=>a.code=='6-1700').id")
        page.select_option('[data-f=account_id]', str(sw)); page.fill('[data-f=description]', 'Adobe CC'); page.fill('[data-f=amount]', '89.99')
        page.click('#r-save'); page.wait_for_selector('text=Adobe subscription')
        # monthly on the 5th from July 2026, up to today
        t = date.today()
        n_adobe = (t.year - 2026) * 12 + t.month - 7 + (1 if t.day >= 5 else 0)
        assert f'Saved and created {n_adobe}' in toast_text(page), toast_text(page)
        shot(page, '07-recurring')

        # reconcile with CSV import (July statement)
        csv = os.path.join(data, 'stmt.csv')
        with open(csv, 'w') as f:
            f.write('Date,Description,Debit,Credit,Balance\n05/07/2026,ADOBE SYSTEMS,89.99,,7910.01\n20/07/2026,HARBOUR ANALYTICS PAYMENT,,3750.00,11660.01\n31/07/2026,ACCOUNT FEE,10.00,,11650.01\n')
        page.goto(URL + '#/reconcile'); page.wait_for_selector('#rc-bal')
        page.set_input_files('#rc-file', csv); page.wait_for_selector('#m-prev table')
        shot(page, '08-csvmap')
        page.click('#modal [data-x=yes]'); page.wait_for_selector('#rc-csv h2')
        assert '2 matched' in page.inner_text('#rc-csv'), page.inner_text('#rc-csv')
        page.click('[data-new]'); page.wait_for_selector('#q-acct')
        page.click('#modal [data-x=yes]'); page.wait_for_timeout(600)
        page.wait_for_selector('#rc-csv h2')
        assert '3 matched' in page.inner_text('#rc-csv'), page.inner_text('#rc-csv')
        page.fill('#rc-bal', '11,650.01'); page.locator('#rc-bal').dispatch_event('input')
        assert page.inner_text('#rc-diff') == '$0.00', page.inner_text('#rc-diff')
        shot(page, '09-reconcile')
        page.click('#rc-go'); page.wait_for_timeout(600)
        assert 'Reconciled' in toast_text(page) or 'Reconciliation history' in page.inner_text('main')

        # reports
        page.goto(URL + '#/reports/pl'); page.wait_for_selector('.report')
        page.fill('#f-from', '2026-07'); page.fill('#f-to', '2026-10'); page.locator('#f-to').dispatch_event('change'); page.wait_for_timeout(400)
        txt = page.inner_text('.report')
        assert '4,900.01' in txt and '4,630.04' in txt, txt
        shot(page, '10-pl')
        page.goto(URL + '#/reports/bs'); page.wait_for_selector('.report')
        page.select_option('#f-count', '3'); page.wait_for_timeout(400)
        assert 'doesn\'t balance' not in page.inner_text('main')
        shot(page, '11-bs')
        page.goto(URL + '#/reports/aged-ar'); page.wait_for_selector('.report')
        assert 'Ties to' in page.inner_text('main'), page.inner_text('main')
        shot(page, '12-aged')
        page.goto(URL + '#/banking'); page.wait_for_selector('.filters'); shot(page, '13-banking')
        # manage bank accounts: add, rename, remove
        page.click('#bk-manage'); page.wait_for_selector('.mb-name')
        page.click('#mb-add-bank'); page.wait_for_selector('.mb-name >> nth=2')
        # new bank accounts sit with the other banks, above credit cards, so find it by name
        names = page.locator('.mb-name')
        new_row = [i for i in range(names.count()) if names.nth(i).input_value() == 'Bank 2']
        assert len(new_row) == 1, [names.nth(i).input_value() for i in range(names.count())]
        names.nth(new_row[0]).fill('Term Deposit')
        page.click('#modal [data-x=yes]'); page.wait_for_selector('.tabs >> text=Term Deposit')
        page.click('#bk-manage'); page.wait_for_selector('.mb-name')
        page.locator('tr', has=page.locator('input[value="Term Deposit"]')).locator('.mb-del').click()
        page.click('#modal [data-x=yes]'); page.wait_for_timeout(500)
        assert page.locator('input[value="Term Deposit"]').count() == 0
        page.click('#modal [data-x=no]'); page.wait_for_timeout(300)
        assert 'Term Deposit' not in page.inner_text('.tabs')
        page.goto(URL + '#/purchases/new'); page.wait_for_selector('#f-contact'); shot(page, '14-bill')
        page.goto(URL + '#/settings'); page.wait_for_selector('#st-lock'); page.click('#st-lock-last'); page.wait_for_timeout(400)
        page.goto(URL + '#/journal/new'); page.wait_for_selector('#t-jlines'); shot(page, '15-journal')
        for h in ['#/contacts', '#/accounts', '#/journal', '#/audit', '#/opening', '#/reports/tb', '#/reports/aged-ap', '#/purchases', '#/sales?status=all', '#/banking/transfer', '#/dashboard']:
            page.goto(URL + h); page.wait_for_timeout(350)
            assert 'notice bad' not in page.content() or h == '#/reports/bs', h
        shot(page, '16-dashboard-after')
        b.close()
finally:
    srv.terminate()
    out = srv.stdout.read().decode()
    if 'Traceback' in out: print(out)
    shutil.rmtree(data)
print('console errors:', errors)
print('UI TEST DONE')
