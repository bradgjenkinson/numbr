"""End-to-end UI test in headless Chromium (needs: pip install playwright && playwright install chromium).
Walks setup, invoices, payments, recurring, CSV reconciliation and reports. Screenshots go to tests/screenshots/.
Run: python tests/test_ui.py"""
import os, subprocess, sys, time, tempfile, shutil
from datetime import date, timedelta
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
        assert '1-1100' not in page.inner_text('main'), 'codes are hidden'
        def ob(name, v):
            aid = page.evaluate(f"S.accounts.find(a=>a.name=={name!r}).id")
            page.fill(f'.ob[data-id="{aid}"]', v)
        ob('Westpac Business One', '8,000'); ob('Accounts Receivable', '2750'); ob('Credit Card 1', '320'); ob("Owner's Capital", '10430')
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
        assert page.is_hidden('#m-saved'), 'first import has no saved column choices'
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
        # account groups: make one from an account, add a second account, check the subtotals
        page.goto(URL + '#/accounts'); page.wait_for_selector('tr[data-id]')
        page.click(f'tr[data-id="{sw}"]'); page.wait_for_selector('#am-group')
        assert page.is_hidden('#am-gname')
        page.select_option('#am-group', 'new'); page.fill('#am-gname', 'Overheads')
        page.click('#modal [data-x=yes]'); page.wait_for_selector('tr.ghead')
        gid = page.evaluate("S.groups.find(g=>g.name=='Overheads').id")
        fees = page.evaluate("S.accounts.find(a=>a.code=='6-1200').id")
        page.click(f'tr[data-id="{fees}"]'); page.wait_for_selector('#am-group')
        page.select_option('#am-group', str(gid)); page.click('#modal [data-x=yes]'); page.wait_for_timeout(500)
        assert page.locator('tr.ghead', has_text='Overheads').count() == 1, page.inner_text('main')
        # a subgroup made from the group form
        page.click('#g-new'); page.wait_for_selector('#gm-name')
        page.fill('#gm-name', 'Software'); page.select_option('#gm-type', 'expense'); page.select_option('#gm-parent', str(gid))
        page.check(f'#gm-list input[value="{sw}"]'); page.click('#modal [data-x=yes]'); page.wait_for_timeout(500)
        assert page.locator('tr.ghead').count() == 2 and page.locator('tr.ghead.gl1', has_text='Software').count() == 1
        shot(page, '10b-groups')
        # chart of accounts: no codes, a search box, and unique names
        assert '6-1200' not in page.inner_text('main') and page.locator('th', has_text='Code').count() == 0
        page.fill('#a-find', 'bank fe'); page.wait_for_timeout(200)
        vis = [t.strip() for t in page.locator('tr[data-name]:visible').all_inner_texts()]
        assert len(vis) == 2 and 'Overheads' in vis[0] and 'Bank Fees' in vis[1], vis
        page.fill('#a-find', 'zzzz'); page.wait_for_timeout(200); assert page.is_visible('#a-none')
        page.fill('#a-find', ''); page.wait_for_timeout(200)
        page.click('#a-new'); page.wait_for_selector('#am-name')
        assert page.locator('#am-code').count() == 0
        page.fill('#am-name', 'bank FEES'); page.select_option('#am-type', 'expense'); page.click('#modal [data-x=yes]'); page.wait_for_timeout(400)
        assert 'already an account called' in toast_text(page), toast_text(page)
        page.fill('#am-name', 'Parking'); page.click('#modal [data-x=yes]'); page.wait_for_timeout(500)
        assert page.evaluate("S.accounts.find(a=>a.name=='Parking').code").startswith('6-')
        page.goto(URL + '#/reports/pl'); page.wait_for_selector('.report')
        want = page.evaluate(f"""(async () => {{ const d = await GET('/api/reports/pl?from=2026-07&to=2026-10');
            return fmt(d.sections.flatMap(s => s.rows).filter(r => [{sw}, {fees}].includes(r.account_id)).reduce((a, r) => a + r.total, 0)); }})()""")
        txt = page.inner_text('.report')
        head = page.locator('tr.ghead', has_text='Overheads').inner_text()
        assert want in head and page.locator('tr.ghead.gl1', has_text='Software').count() == 1 and '4,630.04' in txt, (want, head, txt)
        shot(page, '10c-pl-groups')
        page.check('#f-gonly'); page.wait_for_timeout(500)
        assert page.locator('tr.ghead').count() == 1 and 'Software' not in page.inner_text('.report')
        assert want in page.locator('tr.ghead').inner_text()
        shot(page, '10d-pl-groups-only')
        page.uncheck('#f-gonly'); page.wait_for_selector('tr.ghead')
        page.goto(URL + '#/reports/bs'); page.wait_for_selector('.report')
        # defaults to this financial year: one month-end column per month, same headings as the P&L
        heads = page.locator('.rep-grid thead th').all_inner_texts()
        assert heads[1].strip().upper() == page.evaluate("monthLabel(fyStartYM(S.today))").upper(), heads
        page.fill('#f-from', '2026-08'); page.fill('#f-to', '2026-10'); page.locator('#f-to').dispatch_event('change'); page.wait_for_timeout(400)
        assert page.locator('.rep-grid thead th').count() == 4
        assert 'doesn\'t balance' not in page.inner_text('main')
        cells = [t.strip() for t in page.locator('.rep-grid td.num').all_inner_texts()]
        assert '0.00' not in cells and '–' in cells, cells
        # header row and account column are frozen
        assert page.evaluate("getComputedStyle(document.querySelector('.rep-grid thead th')).position") == 'sticky'
        assert page.evaluate("getComputedStyle(document.querySelector('.rep-grid tbody td')).position") == 'sticky'
        shot(page, '11-bs')
        # scrolled in a small window: months stay at the top, accounts stay at the left
        vp = page.viewport_size
        page.set_viewport_size({'width': 900, 'height': 500})
        page.click('[data-r] >> text=This financial year'); page.wait_for_timeout(400)
        page.evaluate("const g = document.querySelector('.rep-grid'); g.scrollIntoView(); g.scrollTop = 120; g.scrollLeft = 400;")
        page.wait_for_timeout(150)
        th = page.locator('.rep-grid thead th').nth(0).bounding_box(); grid = page.locator('.rep-grid').bounding_box()
        assert abs(th['y'] - grid['y']) < 2 and abs(th['x'] - grid['x']) < 2, (th, grid)
        shot(page, '11b-bs-scrolled')
        # scrolled right to the bottom of the table and the page: the month row is still on screen
        for rep in ('bs', 'pl'):
            page.goto(URL + f'#/reports/{rep}'); page.wait_for_selector('.rep-grid')
            page.evaluate("const g = document.querySelector('.rep-grid'); g.scrollTop = g.scrollHeight;"
                          "window.scrollTo(0, document.documentElement.scrollHeight); document.body.scrollTop = document.body.scrollHeight;")
            page.wait_for_timeout(150)
            th = page.locator('.rep-grid thead th').nth(1).bounding_box()
            assert -1 <= th['y'] and th['y'] + th['height'] <= 500, (rep, th)
        shot(page, '11c-pl-scrolled-bottom')
        page.set_viewport_size(vp)
        # account picker: type to find an account, Enter picks it
        page.goto(URL + '#/banking/spend'); page.wait_for_selector('.acct-pick-input')
        pick = page.locator('.acct-pick-input').first
        pick.click(); assert page.locator('.acct-pick-list .ap-head').first.inner_text().strip().upper() == 'ASSETS'
        pick.fill('ren'); page.wait_for_timeout(150)
        items = page.locator('.acct-pick-list .ap-item').all_inner_texts()
        assert items == ['Rent'], items
        pick.press('Enter')
        rent_id = page.evaluate("String(S.accounts.find(a=>a.name=='Rent').id)")
        assert page.locator('[data-f=account_id]').first.input_value() == rent_id and pick.input_value() == 'Rent'
        pick.click(); pick.fill('expenses tele'); page.wait_for_timeout(150)
        items = page.locator('.acct-pick-list .ap-item').all_inner_texts()
        assert items == ['Telephone & Internet'], items
        pick.press('Escape'); assert pick.input_value() == 'Rent' and page.is_hidden('.acct-pick-list')
        assert page.is_visible('main'), 'Escape in the picker does not leave the page'
        shot(page, '11b-picker')
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
        # shared bill: 50% of the rent is the user's
        sid = page.evaluate("(async () => { const r = await api('POST', '/api/contacts', {name: 'Shared landlord', kind: 'supplier'}); await refreshState(); return r.id; })()")
        page.goto(URL + '#/purchases'); page.wait_for_selector('#pay-hist')
        page.goto(URL + '#/purchases/new'); page.wait_for_selector('#f-share')
        assert page.input_value('#f-share') == '100'
        page.select_option('#f-contact', str(sid))
        rent = page.evaluate("S.accounts.find(a=>a.code=='6-1600').id")
        page.select_option('[data-f=account_id]', str(rent)); page.fill('[data-f=description]', 'Rent'); page.fill('[data-f=amount]', '2000')
        page.fill('#f-share', '50')
        page.click('#f-save'); page.wait_for_selector('text=Balance due')
        assert '50% is yours' in page.inner_text('main') and '$1,000.00 of the $2,000.00' in page.inner_text('main'), page.inner_text('main')
        shot(page, '14b-shared-bill')
        page.goto(URL + '#/recurring/new'); page.wait_for_selector('#r-name')
        page.select_option('#r-kind', 'spend'); assert page.locator('.r-share').is_hidden()
        page.select_option('#r-kind', 'bill'); assert not page.locator('.r-share').is_hidden()
        page.select_option('#r-kind', 'partner')
        assert page.locator('.r-share').is_hidden() and page.locator('#r-lines').is_hidden() and not page.locator('.r-amount').is_hidden()
        # partner contribution: add one, then mark it paid
        pid = page.evaluate("(async () => { const r = await api('POST', '/api/contacts', {name: 'Pat Partner', kind: 'customer'}); await refreshState(); return r.id; })()")
        page.goto(URL + '#/partner/new'); page.wait_for_selector('#f-amount')
        assert page.locator('#f-share').count() == 0 and page.locator('#f-lines .lines').count() == 0
        page.select_option('#f-contact', str(pid)); page.fill('#f-amount', '1,075'); page.fill('#f-desc', 'Weekly shared expenses')
        page.click('#f-save'); page.wait_for_selector('text=Balance due')
        assert 'CONTRIBUTION' in page.inner_text('main') and '$1,075.00' in page.inner_text('main'), page.inner_text('main')
        shot(page, '14c-contribution')
        page.click('main a.btn.primary >> text=Mark paid'); page.wait_for_selector('.p-alloc')
        assert page.input_value('.p-alloc') == '1,075.00', page.input_value('.p-alloc')
        page.click('#p-save'); page.wait_for_selector('text=Applied to')
        assert 'Partner contribution received' in page.inner_text('main')
        page.goto(URL + '#/partner?status=paid'); page.wait_for_selector('.tabs')
        assert 'Pat Partner' in page.inner_text('main')
        # an old-style invoice to the partner can be converted from its page
        sales = page.evaluate("S.accounts.find(a=>a.code=='4-1000').id")
        oid = page.evaluate(f"(async () => (await api('POST', '/api/invoices', {{kind: 'sale', contact_id: {pid}, date: S.today, lines: [{{account_id: {sales}, amount: 5000}}]}})).id)()")
        page.goto(URL + f'#/sales/{oid}'); page.wait_for_selector('#iv-partner')
        page.click('#iv-partner'); page.click('#modal [data-x=yes]'); page.wait_for_url(f'**/#/partner/{oid}'); page.wait_for_selector('text=Balance due')
        assert page.url.endswith(f'#/partner/{oid}'), page.url
        # bank rules: build two rules with the pattern builder, then import a statement
        def make_rule(name, aop, amt, code, mode):
            page.goto(URL + '#/rules/new'); page.wait_for_selector('#ru-name')
            page.fill('#ru-name', name)
            page.fill('.tk-v', 'BEAN CAFE')
            page.click('.ru-add[data-t=number]'); page.wait_for_selector('.token-chip >> nth=1')
            page.select_option('#ru-aop', aop); page.fill('#ru-amt', amt)
            page.select_option('#ru-target', str(page.evaluate(f"S.accounts.find(a=>a.code=='{code}').id")))
            page.check(f'input[name=ru-mode][value={mode}]')
            page.fill('#ru-sample', 'BEAN CAFE 1234 SYDNEY'); page.wait_for_selector('#ru-result .notice.good')
            page.fill('#ru-sample', 'BEAN CAFE SYDNEY'); page.wait_for_selector('#ru-result .notice.warn')
            shot(page, f'17-rule-{name.lower()}')
            page.click('#ru-save'); page.wait_for_selector(f'#main td >> text={name}')
        make_rule('Coffee', 'lt', '10', '6-9000', 'recommend')
        make_rule('Lunch', 'gt', '10', '6-1900', 'auto')
        assert 'Money out under $10.00' in page.inner_text('main'), page.inner_text('main')
        # a transfer rule: pick "Transfer" and the other bank account instead of a category
        page.goto(URL + '#/rules/new'); page.wait_for_selector('#ru-name')
        assert page.is_visible('#ru-target') and page.is_hidden('#ru-to')
        page.fill('#ru-name', 'Savings sweep'); page.fill('.tk-v', 'SWEEP TO SAVINGS')
        page.select_option('#ru-kind', 'xfer')
        assert page.is_hidden('#ru-target') and page.is_visible('#ru-to')
        page.select_option('#ru-to', index=2)
        page.click('#ru-save'); page.wait_for_selector('#main td >> text=Savings sweep')
        assert 'Transfer: ' in page.locator('#main tr', has=page.locator('text=Savings sweep')).inner_text()
        shot(page, '18-rules')
        d = (date.today() - timedelta(days=1)).strftime('%d/%m/%Y')
        today = date.today().strftime('%d/%m/%Y')
        csv2 = os.path.join(data, 'stmt2.csv')
        with open(csv2, 'w') as f:
            f.write(f'Date,Description,Debit,Credit\n{d},BEAN CAFE 1234 SYDNEY,4.50,\n{d},BEAN CAFE 1234 SYDNEY,18.00,\n{d},MYSTERY SHOP,7.00,\n{today},PENDING TODAY,9.00,\n')
        page.goto(URL + '#/reconcile'); page.wait_for_selector('#rc-bal')
        page.set_input_files('#rc-file', csv2); page.wait_for_selector('#m-acc')
        assert 'Import into Westpac Business One' in page.inner_text('#modal [data-x=yes]')
        # lines dated today are left out (banks often re-date them)
        prev = page.inner_text('#m-prev')
        assert '3 lines read' in prev and '1 dated today left out' in prev and 'PENDING TODAY' not in prev, prev
        page.click('#modal [data-x=yes]'); page.wait_for_selector('#rc-csv h2')
        head = page.inner_text('#rc-csv h2')
        assert '1 auto-applied' in head and '1 to review' in head and '1 unmatched' in head, head
        assert 'ticked' in page.locator('#rc-body tr', has=page.locator('text=Auto · Lunch')).get_attribute('class')
        assert 'ticked' not in page.locator('#rc-body tr', has=page.locator('text=Review · Coffee')).get_attribute('class')
        assert 'General Expenses' in page.inner_text('#rc-csv') and '6-9000' not in page.inner_text('#rc-csv')
        shot(page, '19-reconcile-rules')
        page.goto(URL + '#/banking'); page.wait_for_selector('#r-rule')
        txt = page.inner_text('main')
        assert 'Auto · Lunch' in txt and 'Review · Coffee' in txt, txt
        page.select_option('#r-rule', 'review')
        visible = [t for t in page.locator('tr[data-q]').all() if t.is_visible()]
        assert len(visible) == 1 and 'Coffee' in visible[0].inner_text(), [v.inner_text() for v in visible]
        shot(page, '20-banking-rules')
        # re-importing the same statement creates nothing new
        page.goto(URL + '#/reconcile'); page.wait_for_selector('#rc-bal')
        page.set_input_files('#rc-file', csv2); page.wait_for_selector('#m-acc')
        assert page.is_visible('#m-saved'), 'saved column choices are used on the next import'
        # changing a choice then cancelling leaves the saved choices as they were
        page.select_option('#m-df', 'mdy'); page.click('#modal [data-x=no]')
        page.reload(); page.wait_for_selector('#rc-bal')
        page.set_input_files('#rc-file', csv2); page.wait_for_selector('#m-acc')
        assert page.input_value('#m-df') == 'dmy', 'cancelling the dialog does not save column choices'
        page.click('#modal [data-x=yes]'); page.wait_for_selector('#rc-csv h2'); page.wait_for_timeout(300)
        assert '2 already in numbr' in toast_text(page), toast_text(page)
        assert page.locator('#rc-body tr', has=page.locator('text=Coffee')).count() == 1
        # making a rule from an unmatched line prefills the builder
        page.click('[data-rule]'); page.wait_for_selector('#ru-name')
        assert page.input_value('.tk-v') == 'MYSTERY SHOP' and page.input_value('#ru-sample') == 'MYSTERY SHOP'
        page.click('#ru-cancel'); page.wait_for_selector('#rc-bal')
        # an unmatched line can be entered as a transfer to another bank account
        page.click('[data-xfer]'); page.wait_for_selector('#q-other')
        assert page.input_value('#q-kind') == 'xfer-out' and page.is_hidden('#q-acct') and page.is_visible('#q-other')
        page.select_option('#q-other', label='Credit Card 1')
        page.click('#modal [data-x=yes]'); page.wait_for_timeout(600); page.wait_for_selector('#rc-csv h2')
        assert '0 unmatched' in page.inner_text('#rc-csv h2'), page.inner_text('#rc-csv h2')
        assert page.locator('#rc-body tr', has=page.locator('text=MYSTERY SHOP')).count() == 1
        # money in: Customer payment and Partner contribution open their forms with the line's details
        csv3 = os.path.join(data, 'stmt3.csv')
        with open(csv3, 'w') as f:
            f.write(f'Date,Description,Debit,Credit\n{d},DEPOSIT FROM SOMEONE,,25.00\n')
        n_err = len(errors)
        for pay, form_title in (('receipt', 'Receive payment'), ('contribution', 'Partner contribution received')):
            page.goto(URL + '#/reconcile'); page.wait_for_selector('#rc-bal')
            page.set_input_files('#rc-file', csv3); page.wait_for_selector('#m-acc')
            page.click('#modal [data-x=yes]'); page.wait_for_selector(f'[data-pay={pay}]')
            page.click(f'[data-pay={pay}]'); page.wait_for_selector('#p-contact'); page.wait_for_timeout(300)
            assert form_title in page.inner_text('main') and page.input_value('#p-amount') == '25.00'
            page.select_option('#p-contact', index=1); page.wait_for_timeout(300)
        assert len(errors) == n_err and 'Cannot set' not in toast_text(page), errors

        # budgets: build one through the wizard, with a two-step monthly amount, then edit a figure by hand
        page.goto(URL + '#/budgets'); page.wait_for_selector('a[href="#/budgets/new"]')
        page.click('a[href="#/budgets/new"]'); page.wait_for_selector('#bb-fy')
        page.click('#bb-next'); page.wait_for_selector('.bb-table')
        sales = page.evaluate("S.accounts.find(a=>a.code=='4-1000').id")
        assert page.input_value(f'.bb-table tr[data-a="{sales}"] .bb-method') == 'average', 'accounts with history start at their average'
        rent = page.evaluate("S.accounts.find(a=>a.name=='Rent').id")
        row = f'.bb-table tr[data-a="{rent}"]'
        page.select_option(f'{row} .bb-method', 'schedule'); page.wait_for_selector('#oa-body .seg')
        page.fill('.s-amt', '100')
        page.select_option('.s-to', index=3)
        page.click('#oa-add'); page.locator('.s-amt').nth(1).fill('110')
        page.wait_for_timeout(700)
        assert '1,280.00' in page.inner_text('#oa-prev'), page.inner_text('#oa-prev')  # 4 x 100 + 8 x 110
        shot(page, '21b-budget-own-amounts')
        page.click('#oa-ok'); page.wait_for_timeout(600)
        assert '1,280.00' in page.inner_text(row) and 'a month' in page.inner_text(row), page.inner_text(row)
        # a total for the year, spread evenly
        ins = page.evaluate("S.accounts.find(a=>a.name=='Insurance').id")
        irow = f'.bb-table tr[data-a="{ins}"]'
        page.select_option(f'{irow} .bb-method', 'schedule'); page.wait_for_selector('#oa-body .seg')
        page.select_option('.s-freq', 'yearly'); page.wait_for_timeout(200)
        page.fill('.s-amt', '1200'); page.wait_for_timeout(700)
        assert page.inner_text('#oa-prev').count('100.00') == 12, page.inner_text('#oa-prev')
        page.click('#oa-ok'); page.wait_for_timeout(600)
        assert 'spread evenly over the year' in page.inner_text(irow) and '1,200.00' in page.inner_text(irow), page.inner_text(irow)
        shot(page, '21c-budget-accounts')
        page.click('#bb-next'); page.wait_for_selector('#bb-name')
        shot(page, '21d-budget-review')
        page.click('#bb-next'); page.wait_for_selector('.bcell input')
        cell = page.locator(f'.bcell input[data-a="{rent}"][data-i="0"]')
        assert cell.input_value() == '100.00', cell.input_value()
        cell.fill('150'); cell.press('Tab')
        assert not page.is_disabled('#bv-save')
        shot(page, '21e-budget-edit')
        page.click('#bv-save'); page.wait_for_timeout(600)
        page.reload(); page.wait_for_selector('.bcell input')
        assert page.locator(f'.bcell input[data-a="{rent}"][data-i="0"]').input_value() == '150.00'
        assert '1,330.00' in page.inner_text(f'tr[data-a="{rent}"]')
        assert len(errors) == n_err, errors

        # budget vs actual on the P&L: YTD accumulative totals, then month by month; refused for months with no budget
        bfy = page.evaluate("fyStartYM(S.today)")
        bend = page.evaluate(f"addMonthsYM('{bfy}', 11)")
        page.goto(URL + '#/reports/pl'); page.wait_for_selector('[data-view="budget"]')
        page.fill('#f-from', bfy); page.dispatch_event('#f-from', 'change'); page.wait_for_timeout(300)
        page.fill('#f-to', bend); page.dispatch_event('#f-to', 'change'); page.wait_for_selector('[data-view="budget"]')
        assert not page.is_disabled('[data-view="budget"]') and not page.is_disabled('[data-view="monthly"]')
        page.click('[data-view="budget"]'); page.wait_for_selector('.report.bva')
        assert 'YTD Accumulative' in page.inner_text('.report-title') and 'Rent' in page.inner_text('.rep-grid')
        want = page.evaluate("(async () => { const d = await GET(`/api/reports/pl?from=" + bfy + "&to=" + bend + "&budget=1`); return [fmt(d.net_profit_total, {paren:true, dashZero:true}), fmt(d.net_profit_budget_total, {paren:true, dashZero:true}), d.net_profit, d.net_profit_budget]; })()")
        np_cells = page.locator('.rep-grid tr.grand td')
        assert [np_cells.nth(1).inner_text(), np_cells.nth(2).inner_text()] == want[:2], want
        shot(page, '22-budget-vs-actual-ytd')
        page.click('[data-view="monthly"]'); page.wait_for_selector('.bva-mm td.mm')
        # each month is Budget | Actual | Difference in whole dollars; difference is positive when better than budget
        np_cells = page.locator('.rep-grid tr.grand td.mm')
        assert np_cells.count() == 39, np_cells.count()
        dol = lambda c: '' if not round(c / 100) else (f'({abs(round(c / 100)):,})' if c < 0 else f'{round(c / 100):,}')
        for i in range(12):
            bt, ac, df = (np_cells.nth(3 * i + k).inner_text() for k in range(3))
            diff = round(want[2][i] / 100) - round(want[3][i] / 100)
            assert (bt, ac) == (dol(want[3][i]), dol(want[2][i])), (i, bt, ac)
            assert df == ('' if not diff else f"{'−' if diff < 0 else ''}{abs(diff):,}"), (i, df, diff)
            assert ('bad' in np_cells.nth(3 * i + 2).get_attribute('class')) == (diff < 0)
        exp_row = page.locator('.rep-grid tr.total', has_text='Total expenses').locator('td.mm')
        eb, ea, ed = (exp_row.nth(k).inner_text() for k in range(3))
        assert ed == '' or (ed.startswith('−') == (int(ea.replace(',', '') or 0) > int(eb.replace(',', '') or 0))), (eb, ea, ed)
        assert 'Month by Month' in page.inner_text('.report-title')
        shot(page, '22b-budget-vs-actual-monthly')
        nxt = page.evaluate(f"addMonthsYM('{bend}', 2)")
        page.fill('#f-to', nxt); page.dispatch_event('#f-to', 'change'); page.wait_for_selector('.seg-hint')
        assert page.is_disabled('[data-view="budget"]') and page.is_disabled('[data-view="monthly"]') and page.locator('.report.bva').count() == 0
        assert 'No budget for' in page.inner_text('.seg-hint')
        page.click('[data-view="actual"]')
        assert len(errors) == n_err, errors

        page.goto(URL + '#/settings'); page.wait_for_selector('#st-lock'); page.click('#st-lock-last'); page.wait_for_timeout(400)
        page.goto(URL + '#/journal/new'); page.wait_for_selector('#t-jlines'); shot(page, '15-journal')
        for h in ['#/contacts', '#/accounts', '#/journal', '#/audit', '#/opening', '#/reports/tb', '#/reports/aged-ap', '#/purchases', '#/sales?status=all', '#/partner', '#/banking/transfer', '#/dashboard']:
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
