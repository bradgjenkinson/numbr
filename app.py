#!/usr/bin/env python3
"""
Ledger - simple local bookkeeping for a small business.

Runs entirely on this computer using only the Python standard library and SQLite.
Start it with start.bat (Windows) or:  python app.py
Your books live in data/books.db. Backups go to data/backups/.
"""
import calendar
import datetime as dt
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import traceback
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('LEDGER_DATA', os.path.join(APP_DIR, 'data'))
DB_PATH = os.path.join(DATA_DIR, 'books.db')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
STATIC_DIR = os.path.join(APP_DIR, 'static')
HOST = '127.0.0.1'
PORT = int(os.environ.get('LEDGER_PORT', '8765'))
KEEP_BACKUPS = 40

# Natural sign: +1 means a debit increases the account's balance.
SIGN = {'asset': 1, 'cos': 1, 'expense': 1, 'liability': -1, 'equity': -1, 'income': -1}
PL_TYPES = ('income', 'cos', 'expense')
FREQS = ('weekly', 'fortnightly', 'monthly', 'quarterly', 'yearly')
SYSTEM_SUBTYPES = ('ar', 'ap', 're', 'hist')
EDITABLE_SOURCES = ('spend', 'receive', 'transfer', 'journal')


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','cos','expense')),
  subtype TEXT NOT NULL DEFAULT 'other',
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'customer' CHECK (kind IN ('customer','supplier','both')),
  email TEXT, phone TEXT, address TEXT,
  terms_days INTEGER NOT NULL DEFAULT 14,
  default_account_id INTEGER REFERENCES accounts(id),
  notes TEXT,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  statement_date TEXT NOT NULL,
  statement_balance INTEGER NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journals (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  memo TEXT,
  reference TEXT,
  source TEXT NOT NULL,
  source_id INTEGER,
  contact_id INTEGER REFERENCES contacts(id),
  recurring_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_journals_date ON journals(date);
CREATE TABLE IF NOT EXISTS journal_lines (
  id INTEGER PRIMARY KEY,
  journal_id INTEGER NOT NULL REFERENCES journals(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount INTEGER NOT NULL,            -- cents: positive = debit, negative = credit
  description TEXT,
  contact_id INTEGER REFERENCES contacts(id),
  role TEXT,                          -- 'bank' | 'control' | NULL
  recon_id INTEGER REFERENCES reconciliations(id)
);
CREATE INDEX IF NOT EXISTS ix_lines_account ON journal_lines(account_id);
CREATE INDEX IF NOT EXISTS ix_lines_journal ON journal_lines(journal_id);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('sale','bill')),
  contact_id INTEGER NOT NULL REFERENCES contacts(id),
  number TEXT,
  date TEXT NOT NULL,
  due_date TEXT NOT NULL,
  memo TEXT,
  total INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','void')),
  historical INTEGER NOT NULL DEFAULT 0,
  journal_id INTEGER,
  recurring_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  description TEXT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('receipt','payment')),
  contact_id INTEGER NOT NULL REFERENCES contacts(id),
  date TEXT NOT NULL,
  bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount INTEGER NOT NULL,
  reference TEXT,
  journal_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
  id INTEGER PRIMARY KEY,
  payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  amount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('sale','bill','spend','receive')),
  contact_id INTEGER REFERENCES contacts(id),
  bank_account_id INTEGER REFERENCES accounts(id),
  frequency TEXT NOT NULL,
  next_date TEXT NOT NULL,
  anchor_day INTEGER NOT NULL,
  end_date TEXT,
  terms_days INTEGER NOT NULL DEFAULT 14,
  memo TEXT,
  lines TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id INTEGER,
  detail TEXT
);
"""

DEFAULT_ACCOUNTS = [
    ('1-1100', 'Business Bank Account', 'asset', 'bank'),
    ('1-1200', 'Savings Account', 'asset', 'bank'),
    ('1-1300', 'Accounts Receivable', 'asset', 'ar'),
    ('1-2000', 'Equipment', 'asset', 'other'),
    ('1-2100', 'Less Accumulated Depreciation', 'asset', 'other'),
    ('2-1100', 'Business Credit Card', 'liability', 'bank'),
    ('2-1200', 'Accounts Payable', 'liability', 'ap'),
    ('2-1300', 'Income Tax Payable', 'liability', 'other'),
    ('2-2000', 'Loans', 'liability', 'other'),
    ('3-1000', "Owner's Capital", 'equity', 'other'),
    ('3-2000', "Owner's Drawings", 'equity', 'other'),
    ('3-8000', 'Retained Earnings', 'equity', 're'),
    ('3-9999', 'Historical Balancing', 'equity', 'hist'),
    ('4-1000', 'Sales', 'income', 'other'),
    ('4-2000', 'Other Income', 'income', 'other'),
    ('4-3000', 'Interest Income', 'income', 'other'),
    ('5-1000', 'Cost of Sales', 'cos', 'other'),
    ('6-1000', 'Advertising & Marketing', 'expense', 'other'),
    ('6-1100', 'Accounting & Legal', 'expense', 'other'),
    ('6-1200', 'Bank Fees', 'expense', 'other'),
    ('6-1300', 'Insurance', 'expense', 'other'),
    ('6-1400', 'Motor Vehicle', 'expense', 'other'),
    ('6-1500', 'Office Supplies', 'expense', 'other'),
    ('6-1600', 'Rent', 'expense', 'other'),
    ('6-1700', 'Software & Subscriptions', 'expense', 'other'),
    ('6-1800', 'Telephone & Internet', 'expense', 'other'),
    ('6-1900', 'Travel', 'expense', 'other'),
    ('6-2000', 'Utilities', 'expense', 'other'),
    ('6-2100', 'Wages & Contractors', 'expense', 'other'),
    ('6-2200', 'Depreciation', 'expense', 'other'),
    ('6-9000', 'General Expenses', 'expense', 'other'),
]

DEFAULT_SETTINGS = {
    'business_name': '',
    'conversion_date': '',
    'fy_start_month': '7',
    'lock_date': '',
    'next_invoice_number': '1001',
    'setup_done': '0',
    'last_recurring_run': '',
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def now():
    return dt.datetime.now().isoformat(timespec='seconds')


def today():
    return dt.date.today().isoformat()


def D(s):
    return dt.date.fromisoformat(s)


def req(body, key, label=None):
    v = body.get(key)
    if v is None or (isinstance(v, str) and v.strip() == ''):
        raise ApiError(f'{label or key} is required')
    return v


def as_date(v, label='Date'):
    try:
        return dt.date.fromisoformat(str(v).strip()).isoformat()
    except Exception:
        raise ApiError(f'{label} is not a valid date')


def as_int(v, label='Value'):
    try:
        if isinstance(v, bool):
            raise ValueError
        return int(v)
    except Exception:
        raise ApiError(f'{label} is not valid')


def as_cents(v, label='Amount'):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(f'{label} is not a valid amount')
    if abs(v) > 10 ** 13:
        raise ApiError(f'{label} is too large')
    return v


def text(v):
    return (v or '').strip() if isinstance(v, str) else ('' if v is None else str(v))


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def scalar(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return r[0] if r else None


def money(c):
    return f"{'-' if c < 0 else ''}${abs(c) / 100:,.2f}"


def add_months(d, n, anchor):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(anchor, calendar.monthrange(y, m)[1]))


def advance(date_s, freq, anchor):
    d = D(date_s)
    if freq == 'weekly':
        return (d + dt.timedelta(days=7)).isoformat()
    if freq == 'fortnightly':
        return (d + dt.timedelta(days=14)).isoformat()
    if freq == 'monthly':
        return add_months(d, 1, anchor).isoformat()
    if freq == 'quarterly':
        return add_months(d, 3, anchor).isoformat()
    if freq == 'yearly':
        return add_months(d, 12, anchor).isoformat()
    raise ApiError('Unknown frequency')


def month_start(ym):
    y, m = map(int, ym.split('-'))
    return dt.date(y, m, 1)


def month_end(ym):
    y, m = map(int, ym.split('-'))
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def months_between(f, t):
    try:
        y, m = map(int, f.split('-'))
        ty, tm = map(int, t.split('-'))
    except Exception:
        raise ApiError('Months must look like 2026-07')
    out = []
    while (y, m) <= (ty, tm):
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            m, y = 1, y + 1
        if len(out) > 36:
            raise ApiError('Choose a range of 36 months or less')
    if not out:
        raise ApiError('The "from" month is after the "to" month')
    return out


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = connect()
    db.executescript(SCHEMA)
    db.execute('BEGIN')
    for k, v in DEFAULT_SETTINGS.items():
        db.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', (k, v))
    if scalar(db, 'SELECT COUNT(*) FROM accounts') == 0:
        db.executemany('INSERT INTO accounts(code, name, type, subtype) VALUES (?,?,?,?)', DEFAULT_ACCOUNTS)
    db.execute('COMMIT')
    db.close()


def backup(label='manual'):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    fn = os.path.join(BACKUP_DIR, f"books-{dt.datetime.now():%Y%m%d-%H%M%S}-{label}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(fn)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('books-') and f.endswith('.db'))
    for f in files[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass
    return fn


def get_setting(db, k, default=None):
    v = scalar(db, 'SELECT value FROM settings WHERE key=?', (k,))
    return default if v is None else v


def set_setting(db, k, v):
    db.execute('INSERT INTO settings(key, value) VALUES (?, ?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, '' if v is None else str(v)))


def all_settings(db):
    return {r['key']: r['value'] for r in db.execute('SELECT key, value FROM settings')}


def audit(db, action, entity=None, entity_id=None, detail=None):
    db.execute('INSERT INTO audit_log(ts, action, entity, entity_id, detail) VALUES (?,?,?,?,?)',
               (now(), action, entity, entity_id, json.dumps(detail) if detail is not None else None))


def check_lock(db, date):
    lock = get_setting(db, 'lock_date')
    if lock and date <= lock:
        raise ApiError(f'Transactions dated on or before {lock} are locked. '
                       'Change the lock date in Settings if you need to make changes there.')


def is_historical(db, date):
    conv = get_setting(db, 'conversion_date')
    return bool(conv and date < conv)


def account(db, aid):
    a = one(db.execute('SELECT * FROM accounts WHERE id=?', (aid,)))
    if not a:
        raise ApiError('Account not found')
    return a


def system_account(db, subtype):
    a = one(db.execute('SELECT * FROM accounts WHERE subtype=? ORDER BY id LIMIT 1', (subtype,)))
    if not a:
        raise ApiError(f'No "{subtype}" system account exists')
    return a


def bank_account(db, aid):
    a = account(db, as_int(aid, 'Bank account'))
    if a['subtype'] != 'bank':
        raise ApiError('Choose a bank or credit card account')
    if a['archived']:
        raise ApiError(f'{a["name"]} is archived')
    return a


def contact(db, cid):
    c = one(db.execute('SELECT * FROM contacts WHERE id=?', (as_int(cid, 'Contact'),)))
    if not c:
        raise ApiError('Contact not found')
    return c


# ----------------------------------------------------------------------------
# Journals (the ledger)
# ----------------------------------------------------------------------------

def check_lines(db, lines):
    lines = [l for l in lines if l['amount'] != 0]
    if len(lines) < 2:
        raise ApiError('A transaction needs at least one non-zero amount')
    if sum(l['amount'] for l in lines) != 0:
        raise ApiError('Transaction does not balance (debits must equal credits)')
    for l in lines:
        a = account(db, l['account_id'])
        if a['archived']:
            raise ApiError(f'Account {a["code"]} {a["name"]} is archived')
    return lines


def insert_lines(db, jid, lines, contact_id):
    for l in lines:
        db.execute('INSERT INTO journal_lines(journal_id, account_id, amount, description, contact_id, role) '
                   'VALUES (?,?,?,?,?,?)',
                   (jid, l['account_id'], l['amount'], l.get('description') or None,
                    l.get('contact_id', contact_id), l.get('role')))


def post_journal(db, date, source, lines, memo=None, reference=None, contact_id=None,
                 source_id=None, recurring_id=None, skip_lock=False):
    if not skip_lock:
        check_lock(db, date)
    lines = check_lines(db, lines)
    cur = db.execute('INSERT INTO journals(date, memo, reference, source, source_id, contact_id, recurring_id, created_at) '
                     'VALUES (?,?,?,?,?,?,?,?)',
                     (date, memo or None, reference or None, source, source_id, contact_id, recurring_id, now()))
    jid = cur.lastrowid
    insert_lines(db, jid, lines, contact_id)
    return jid


def assert_journal_editable(db, jid):
    j = one(db.execute('SELECT * FROM journals WHERE id=?', (jid,)))
    if not j:
        raise ApiError('Transaction not found', 404)
    if db.execute('SELECT 1 FROM journal_lines WHERE journal_id=? AND recon_id IS NOT NULL', (jid,)).fetchone():
        raise ApiError('This transaction has been reconciled. Undo that reconciliation first if you need to change it.')
    check_lock(db, j['date'])
    return j


def delete_journal(db, jid):
    assert_journal_editable(db, jid)
    db.execute('DELETE FROM journals WHERE id=?', (jid,))


def parse_item_lines(db, lines, allow_negative=True):
    if not isinstance(lines, list) or not lines:
        raise ApiError('Add at least one line')
    out = []
    for i, l in enumerate(lines, 1):
        amt = as_cents(l.get('amount', 0), f'Line {i} amount')
        desc = text(l.get('description'))
        if amt == 0 and not l.get('account_id'):
            continue
        aid = as_int(req(l, 'account_id', f'Line {i} account'), f'Line {i} account')
        a = account(db, aid)
        if a['subtype'] in ('ar', 'ap'):
            raise ApiError(f'Line {i}: Accounts Receivable/Payable can\'t be used directly. Use invoices, bills and payments.')
        if a['archived']:
            raise ApiError(f'Line {i}: {a["name"]} is archived')
        if amt < 0 and not allow_negative:
            raise ApiError(f'Line {i}: amount can\'t be negative')
        out.append({'account_id': aid, 'description': desc, 'amount': amt})
    if not out:
        raise ApiError('Add at least one line')
    return out


# ----------------------------------------------------------------------------
# Settings, state, setup
# ----------------------------------------------------------------------------

ROUTES = []


def route(method, pattern):
    rx = re.compile('^' + re.sub(r':(\w+)', r'(?P<\1>[^/]+)', pattern) + '$')

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


def list_accounts(db):
    accts = rows(db.execute('SELECT * FROM accounts ORDER BY code'))
    bal = {r[0]: r[1] for r in db.execute('SELECT account_id, SUM(amount) FROM journal_lines GROUP BY account_id')}
    used = {r[0] for r in db.execute('SELECT DISTINCT account_id FROM invoice_lines')} | \
           {r[0] for r in db.execute('SELECT DISTINCT account_id FROM journal_lines')}
    for a in accts:
        a['balance'] = SIGN[a['type']] * (bal.get(a['id']) or 0)
        a['used'] = a['id'] in used
        a['system'] = a['subtype'] in SYSTEM_SUBTYPES
    return accts


def list_contacts(db):
    cs = rows(db.execute('SELECT * FROM contacts ORDER BY name COLLATE NOCASE'))
    outstanding = {}
    for r in db.execute("""SELECT i.contact_id, i.kind,
                                  SUM(i.total - COALESCE((SELECT SUM(amount) FROM allocations a WHERE a.invoice_id=i.id),0))
                           FROM invoices i WHERE i.status='open' GROUP BY i.contact_id, i.kind"""):
        outstanding[(r[0], r[1])] = r[2]
    for c in cs:
        c['receivable'] = outstanding.get((c['id'], 'sale'), 0) or 0
        c['payable'] = outstanding.get((c['id'], 'bill'), 0) or 0
    return cs


@route('GET', '/api/state')
def api_state(db, q, body):
    s = all_settings(db)
    generated = []
    if s.get('setup_done') == '1' and s.get('last_recurring_run') != today():
        generated = run_recurring(db)
        s = all_settings(db)
    return {'settings': s, 'accounts': list_accounts(db), 'contacts': list_contacts(db),
            'today': today(), 'generated': generated}


@route('PUT', '/api/settings')
def api_settings(db, q, body):
    s = all_settings(db)
    if 'business_name' in body:
        set_setting(db, 'business_name', text(body['business_name']))
    if 'fy_start_month' in body:
        m = as_int(body['fy_start_month'], 'Financial year start month')
        if not 1 <= m <= 12:
            raise ApiError('Financial year start month must be 1-12')
        set_setting(db, 'fy_start_month', m)
    if 'next_invoice_number' in body:
        n = as_int(body['next_invoice_number'], 'Next invoice number')
        set_setting(db, 'next_invoice_number', n)
    if 'conversion_date' in body:
        conv = as_date(body['conversion_date'], 'Start date')
        if conv != s.get('conversion_date'):
            others = scalar(db, "SELECT COUNT(*) FROM journals WHERE source<>'opening'")
            invs = scalar(db, 'SELECT COUNT(*) FROM invoices')
            if others or invs:
                raise ApiError('The start date can only be changed before any invoices, bills or transactions are entered.')
            set_setting(db, 'conversion_date', conv)
            od = (D(conv) - dt.timedelta(days=1)).isoformat()
            db.execute("UPDATE journals SET date=? WHERE source='opening'", (od,))
            db.execute("UPDATE reconciliations SET statement_date=? WHERE note='opening'", (od,))
            old_conv, old_lock = s.get('conversion_date'), s.get('lock_date')
            default_old = (D(old_conv) - dt.timedelta(days=1)).isoformat() if old_conv else None
            if not old_lock or old_lock == default_old:
                set_setting(db, 'lock_date', od)
    if 'lock_date' in body:
        ld = text(body['lock_date'])
        if ld:
            ld = as_date(ld, 'Lock date')
            conv = get_setting(db, 'conversion_date')
            if conv and ld < (D(conv) - dt.timedelta(days=1)).isoformat():
                raise ApiError('The lock date can\'t be earlier than the day before your start date')
        set_setting(db, 'lock_date', ld)
        audit(db, 'lock_date', detail={'lock_date': ld})
    if 'setup_done' in body:
        set_setting(db, 'setup_done', '1' if body['setup_done'] else '0')
    return all_settings(db)


@route('GET', '/api/opening')
def api_opening_get(db, q, body):
    conv = get_setting(db, 'conversion_date')
    j = one(db.execute("SELECT * FROM journals WHERE source='opening'"))
    balances = {}
    hist = 0
    if j:
        for l in rows(db.execute('SELECT l.*, a.type, a.subtype FROM journal_lines l JOIN accounts a ON a.id=l.account_id '
                                 'WHERE journal_id=?', (j['id'],))):
            nat = SIGN[l['type']] * l['amount']
            if l['subtype'] == 'hist':
                hist += nat
            else:
                balances[str(l['account_id'])] = balances.get(str(l['account_id']), 0) + nat
    ar = system_account(db, 'ar')
    ap = system_account(db, 'ap')
    hist_sales = scalar(db, "SELECT COALESCE(SUM(total),0) FROM invoices WHERE kind='sale' AND historical=1 AND status='open'")
    hist_bills = scalar(db, "SELECT COALESCE(SUM(total),0) FROM invoices WHERE kind='bill' AND historical=1 AND status='open'")
    locked = bool(scalar(db, "SELECT COUNT(*) FROM reconciliations WHERE note IS NOT 'opening'"))
    return {'conversion_date': conv, 'balances': balances, 'historical_balancing': hist,
            'ar_account_id': ar['id'], 'ap_account_id': ap['id'],
            'ar_opening': balances.get(str(ar['id']), 0), 'ap_opening': balances.get(str(ap['id']), 0),
            'historical_sales': hist_sales, 'historical_bills': hist_bills, 'locked': locked}


@route('PUT', '/api/opening')
def api_opening_put(db, q, body):
    conv = get_setting(db, 'conversion_date')
    if not conv:
        raise ApiError('Set your start date first')
    if scalar(db, "SELECT COUNT(*) FROM reconciliations WHERE note IS NOT 'opening'"):
        raise ApiError('Opening balances can\'t be changed after you\'ve reconciled a bank account. '
                       'Undo the reconciliations first.')
    od = (D(conv) - dt.timedelta(days=1)).isoformat()
    old = scalar(db, "SELECT id FROM journals WHERE source='opening'")
    if old:
        db.execute('DELETE FROM journals WHERE id=?', (old,))
    db.execute("DELETE FROM reconciliations WHERE note='opening'")
    balances = body.get('balances') or {}
    lines = []
    for aid, nat in balances.items():
        nat = as_cents(nat, 'Opening balance')
        if nat == 0:
            continue
        a = account(db, as_int(aid, 'Account'))
        if a['subtype'] == 'hist':
            continue
        lines.append({'account_id': a['id'], 'amount': SIGN[a['type']] * nat, 'role': 'bank' if a['subtype'] == 'bank' else None})
    diff = -sum(l['amount'] for l in lines)
    if diff:
        lines.append({'account_id': system_account(db, 'hist')['id'], 'amount': diff})
    if len(lines) >= 2:
        jid = post_journal(db, od, 'opening', lines, memo='Opening balances', skip_lock=True)
        for l in rows(db.execute("SELECT l.id, l.account_id, l.amount, a.type FROM journal_lines l "
                                 "JOIN accounts a ON a.id=l.account_id WHERE l.journal_id=? AND l.role='bank'", (jid,))):
            cur = db.execute("INSERT INTO reconciliations(account_id, statement_date, statement_balance, note, created_at) "
                             "VALUES (?,?,?,'opening',?)", (l['account_id'], od, SIGN[l['type']] * l['amount'], now()))
            db.execute('UPDATE journal_lines SET recon_id=? WHERE id=?', (cur.lastrowid, l['id']))
    audit(db, 'opening_balances', detail={'balances': balances})
    return api_opening_get(db, q, body)


# ----------------------------------------------------------------------------
# Accounts & contacts
# ----------------------------------------------------------------------------

@route('GET', '/api/accounts')
def api_accounts(db, q, body):
    return list_accounts(db)


def account_payload(db, body, existing=None):
    code = text(body.get('code', existing and existing['code']))
    name = text(body.get('name', existing and existing['name']))
    if not code or not name:
        raise ApiError('Code and name are required')
    typ = body.get('type', existing and existing['type'])
    if typ not in SIGN:
        raise ApiError('Choose an account type')
    sub = body.get('subtype', existing and existing['subtype']) or 'other'
    if existing and existing['subtype'] in SYSTEM_SUBTYPES:
        sub = existing['subtype']
        if typ != existing['type']:
            raise ApiError('The type of a system account can\'t be changed')
    elif sub not in ('bank', 'other'):
        raise ApiError('Invalid account kind')
    if sub == 'bank' and typ not in ('asset', 'liability'):
        raise ApiError('Bank/credit card accounts must be an asset or liability')
    dup = scalar(db, 'SELECT id FROM accounts WHERE code=? AND id<>?', (code, existing['id'] if existing else 0))
    if dup:
        raise ApiError(f'Account code {code} is already used')
    return code, name, typ, sub


@route('POST', '/api/accounts')
def api_account_create(db, q, body):
    code, name, typ, sub = account_payload(db, body)
    cur = db.execute('INSERT INTO accounts(code, name, type, subtype) VALUES (?,?,?,?)', (code, name, typ, sub))
    audit(db, 'account_create', 'account', cur.lastrowid, body)
    return {'id': cur.lastrowid}


@route('PUT', '/api/accounts/:aid')
def api_account_update(db, q, body, aid):
    a = account(db, as_int(aid))
    code, name, typ, sub = account_payload(db, body, a)
    used = scalar(db, 'SELECT COUNT(*) FROM journal_lines WHERE account_id=?', (a['id'],))
    if used and (typ != a['type']):
        if (typ in PL_TYPES) != (a['type'] in PL_TYPES):
            raise ApiError('This account has transactions; it can\'t move between the balance sheet and P&L')
    if used and sub != a['subtype'] and 'bank' in (sub, a['subtype']):
        raise ApiError('This account has transactions; it can\'t be switched to or from a bank account')
    archived = 1 if body.get('archived', a['archived']) else 0
    if archived and a['subtype'] in SYSTEM_SUBTYPES:
        raise ApiError('System accounts can\'t be archived')
    if archived and not a['archived']:
        bal = scalar(db, 'SELECT COALESCE(SUM(amount),0) FROM journal_lines WHERE account_id=?', (a['id'],))
        if bal:
            raise ApiError('Only accounts with a zero balance can be archived')
    db.execute('UPDATE accounts SET code=?, name=?, type=?, subtype=?, archived=? WHERE id=?',
               (code, name, typ, sub, archived, a['id']))
    audit(db, 'account_update', 'account', a['id'], body)
    return {'id': a['id']}


@route('DELETE', '/api/accounts/:aid')
def api_account_delete(db, q, body, aid):
    a = account(db, as_int(aid))
    if a['subtype'] in SYSTEM_SUBTYPES:
        raise ApiError('System accounts can\'t be deleted')
    if (scalar(db, 'SELECT COUNT(*) FROM journal_lines WHERE account_id=?', (a['id'],)) or
            scalar(db, 'SELECT COUNT(*) FROM invoice_lines WHERE account_id=?', (a['id'],))):
        raise ApiError('This account has been used. Archive it instead.')
    for r in db.execute('SELECT lines FROM recurring'):
        if any(int(l['account_id']) == a['id'] for l in json.loads(r[0])):
            raise ApiError('This account is used by a recurring transaction')
    db.execute('UPDATE contacts SET default_account_id=NULL WHERE default_account_id=?', (a['id'],))
    db.execute('DELETE FROM accounts WHERE id=?', (a['id'],))
    audit(db, 'account_delete', 'account', a['id'], a)
    return {'ok': True}


def contact_payload(db, body):
    name = text(body.get('name'))
    if not name:
        raise ApiError('Name is required')
    kind = body.get('kind') or 'customer'
    if kind not in ('customer', 'supplier', 'both'):
        raise ApiError('Invalid contact type')
    terms = as_int(body.get('terms_days', 14) or 0, 'Payment terms')
    if terms < 0 or terms > 365:
        raise ApiError('Payment terms must be between 0 and 365 days')
    da = body.get('default_account_id') or None
    if da:
        account(db, as_int(da))
    return (name, kind, text(body.get('email')), text(body.get('phone')), text(body.get('address')),
            terms, da, text(body.get('notes')), 1 if body.get('archived') else 0)


@route('GET', '/api/contacts')
def api_contacts(db, q, body):
    return list_contacts(db)


@route('POST', '/api/contacts')
def api_contact_create(db, q, body):
    p = contact_payload(db, body)
    cur = db.execute('INSERT INTO contacts(name, kind, email, phone, address, terms_days, default_account_id, notes, archived) '
                     'VALUES (?,?,?,?,?,?,?,?,?)', p)
    audit(db, 'contact_create', 'contact', cur.lastrowid, body)
    return {'id': cur.lastrowid}


@route('PUT', '/api/contacts/:cid')
def api_contact_update(db, q, body, cid):
    c = contact(db, cid)
    p = contact_payload(db, body)
    db.execute('UPDATE contacts SET name=?, kind=?, email=?, phone=?, address=?, terms_days=?, default_account_id=?, '
               'notes=?, archived=? WHERE id=?', p + (c['id'],))
    audit(db, 'contact_update', 'contact', c['id'], body)
    return {'id': c['id']}


@route('DELETE', '/api/contacts/:cid')
def api_contact_delete(db, q, body, cid):
    c = contact(db, cid)
    if (scalar(db, 'SELECT COUNT(*) FROM invoices WHERE contact_id=?', (c['id'],)) or
            scalar(db, 'SELECT COUNT(*) FROM journals WHERE contact_id=?', (c['id'],)) or
            scalar(db, 'SELECT COUNT(*) FROM recurring WHERE contact_id=?', (c['id'],))):
        raise ApiError('This contact has transactions. Archive it instead.')
    db.execute('DELETE FROM contacts WHERE id=?', (c['id'],))
    return {'ok': True}


# ----------------------------------------------------------------------------
# Invoices (sales) and bills (purchases)
# ----------------------------------------------------------------------------

def invoice_paid(db, iid, as_at=None):
    if as_at:
        return scalar(db, 'SELECT COALESCE(SUM(a.amount),0) FROM allocations a JOIN payments p ON p.id=a.payment_id '
                          'WHERE a.invoice_id=? AND p.date<=?', (iid, as_at))
    return scalar(db, 'SELECT COALESCE(SUM(amount),0) FROM allocations WHERE invoice_id=?', (iid,))


def next_invoice_number(db):
    n = as_int(get_setting(db, 'next_invoice_number', '1001'))
    while scalar(db, "SELECT 1 FROM invoices WHERE kind='sale' AND number=?", (str(n),)):
        n += 1
    set_setting(db, 'next_invoice_number', n + 1)
    return str(n)


def bump_invoice_number(db, number):
    if number.isdigit():
        n = as_int(get_setting(db, 'next_invoice_number', '1001'))
        if int(number) >= n:
            set_setting(db, 'next_invoice_number', int(number) + 1)


def invoice_payload(db, body, kind):
    c = contact(db, req(body, 'contact_id', 'Contact'))
    date = as_date(req(body, 'date'))
    if body.get('due_date'):
        due = as_date(body['due_date'], 'Due date')
    else:
        due = (D(date) + dt.timedelta(days=c['terms_days'])).isoformat()
    if due < date:
        raise ApiError('The due date is before the date')
    lines = parse_item_lines(db, body.get('lines'))
    total = sum(l['amount'] for l in lines)
    if total <= 0:
        raise ApiError('The total must be more than zero')
    return {'kind': kind, 'contact_id': c['id'], 'contact_name': c['name'], 'number': text(body.get('number')),
            'date': date, 'due_date': due, 'memo': text(body.get('memo')), 'lines': lines, 'total': total}


def invoice_journal_lines(db, p):
    label = f"{'Invoice' if p['kind'] == 'sale' else 'Bill'} {p['number']}".strip() + f" - {p['contact_name']}"
    if p['kind'] == 'sale':
        ctl = system_account(db, 'ar')
        lines = [{'account_id': ctl['id'], 'amount': p['total'], 'role': 'control', 'description': label}]
        lines += [{'account_id': l['account_id'], 'amount': -l['amount'], 'description': l['description']} for l in p['lines']]
    else:
        ctl = system_account(db, 'ap')
        lines = [{'account_id': l['account_id'], 'amount': l['amount'], 'description': l['description']} for l in p['lines']]
        lines += [{'account_id': ctl['id'], 'amount': -p['total'], 'role': 'control', 'description': label}]
    return lines, label


def check_number_unique(db, p, exclude_id=0):
    if p['kind'] == 'sale' and p['number']:
        if scalar(db, "SELECT 1 FROM invoices WHERE kind='sale' AND number=? AND id<>?", (p['number'], exclude_id)):
            raise ApiError(f'Invoice number {p["number"]} is already used')


def create_invoice(db, body, kind, recurring_id=None):
    p = invoice_payload(db, body, kind)
    if kind == 'sale':
        if p['number']:
            check_number_unique(db, p)
            bump_invoice_number(db, p['number'])
        else:
            p['number'] = next_invoice_number(db)
    hist = is_historical(db, p['date'])
    cur = db.execute('INSERT INTO invoices(kind, contact_id, number, date, due_date, memo, total, historical, recurring_id, created_at) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (kind, p['contact_id'], p['number'], p['date'], p['due_date'], p['memo'], p['total'],
                      1 if hist else 0, recurring_id, now()))
    iid = cur.lastrowid
    for l in p['lines']:
        db.execute('INSERT INTO invoice_lines(invoice_id, description, account_id, amount) VALUES (?,?,?,?)',
                   (iid, l['description'], l['account_id'], l['amount']))
    if not hist:
        lines, label = invoice_journal_lines(db, p)
        jid = post_journal(db, p['date'], kind, lines, memo=label, reference=p['number'],
                           contact_id=p['contact_id'], source_id=iid, recurring_id=recurring_id)
        db.execute('UPDATE invoices SET journal_id=? WHERE id=?', (jid, iid))
    audit(db, f'{kind}_create', 'invoice', iid, {'number': p['number'], 'total': p['total'], 'historical': hist})
    return iid


def get_invoice(db, iid):
    inv = one(db.execute('SELECT i.*, c.name AS contact_name, c.email AS contact_email, c.address AS contact_address '
                         'FROM invoices i JOIN contacts c ON c.id=i.contact_id WHERE i.id=?', (as_int(iid),)))
    if not inv:
        raise ApiError('Not found', 404)
    inv['lines'] = rows(db.execute('SELECT l.*, a.code AS account_code, a.name AS account_name FROM invoice_lines l '
                                   'JOIN accounts a ON a.id=l.account_id WHERE invoice_id=? ORDER BY l.id', (inv['id'],)))
    inv['payments'] = rows(db.execute('SELECT a.amount, p.id AS payment_id, p.date, p.reference, b.name AS bank_name '
                                      'FROM allocations a JOIN payments p ON p.id=a.payment_id '
                                      'JOIN accounts b ON b.id=p.bank_account_id WHERE a.invoice_id=? ORDER BY p.date',
                                      (inv['id'],)))
    inv['paid'] = sum(p['amount'] for p in inv['payments'])
    inv['balance'] = inv['total'] - inv['paid'] if inv['status'] == 'open' else 0
    return inv


@route('GET', '/api/invoices')
def api_invoices(db, q, body):
    kind = q.get('kind', 'sale')
    args = [kind]
    sql = ('SELECT i.*, c.name AS contact_name, '
           'COALESCE((SELECT SUM(amount) FROM allocations a WHERE a.invoice_id=i.id),0) AS paid '
           'FROM invoices i JOIN contacts c ON c.id=i.contact_id WHERE i.kind=?')
    if q.get('contact_id'):
        sql += ' AND i.contact_id=?'
        args.append(as_int(q['contact_id']))
    sql += ' ORDER BY i.date DESC, i.id DESC'
    out = []
    t = today()
    status = q.get('status', 'open')
    for r in rows(db.execute(sql, args)):
        r['balance'] = r['total'] - r['paid'] if r['status'] == 'open' else 0
        r['state'] = 'void' if r['status'] == 'void' else ('paid' if r['balance'] == 0 else
                                                            ('overdue' if r['due_date'] < t else 'open'))
        if status == 'open' and r['state'] not in ('open', 'overdue'):
            continue
        if status in ('paid', 'void', 'overdue') and r['state'] != status:
            continue
        out.append(r)
    return out


@route('GET', '/api/invoices/:iid')
def api_invoice_get(db, q, body, iid):
    return get_invoice(db, iid)


@route('POST', '/api/invoices')
def api_invoice_create(db, q, body):
    kind = body.get('kind')
    if kind not in ('sale', 'bill'):
        raise ApiError('Invalid kind')
    return {'id': create_invoice(db, body, kind)}


@route('PUT', '/api/invoices/:iid')
def api_invoice_update(db, q, body, iid):
    inv = get_invoice(db, iid)
    if inv['status'] == 'void':
        raise ApiError('This has been voided')
    if inv['payments']:
        raise ApiError('This has payments recorded against it. Delete the payments first to edit it.')
    if inv['journal_id']:
        delete_journal(db, inv['journal_id'])
    p = invoice_payload(db, body, inv['kind'])
    if inv['kind'] == 'sale':
        p['number'] = p['number'] or inv['number']
        check_number_unique(db, p, inv['id'])
    hist = is_historical(db, p['date'])
    db.execute('UPDATE invoices SET contact_id=?, number=?, date=?, due_date=?, memo=?, total=?, historical=?, journal_id=NULL '
               'WHERE id=?', (p['contact_id'], p['number'], p['date'], p['due_date'], p['memo'], p['total'],
                              1 if hist else 0, inv['id']))
    db.execute('DELETE FROM invoice_lines WHERE invoice_id=?', (inv['id'],))
    for l in p['lines']:
        db.execute('INSERT INTO invoice_lines(invoice_id, description, account_id, amount) VALUES (?,?,?,?)',
                   (inv['id'], l['description'], l['account_id'], l['amount']))
    if not hist:
        lines, label = invoice_journal_lines(db, p)
        jid = post_journal(db, p['date'], inv['kind'], lines, memo=label, reference=p['number'],
                           contact_id=p['contact_id'], source_id=inv['id'], recurring_id=inv['recurring_id'])
        db.execute('UPDATE invoices SET journal_id=? WHERE id=?', (jid, inv['id']))
    audit(db, f"{inv['kind']}_update", 'invoice', inv['id'], {'before': {k: inv[k] for k in ('number', 'date', 'total')},
                                                              'after': {'number': p['number'], 'date': p['date'], 'total': p['total']}})
    return {'id': inv['id']}


@route('POST', '/api/invoices/:iid/void')
def api_invoice_void(db, q, body, iid):
    inv = get_invoice(db, iid)
    if inv['status'] == 'void':
        return {'id': inv['id']}
    if inv['payments']:
        raise ApiError('This has payments recorded against it. Delete the payments first.')
    if inv['journal_id']:
        delete_journal(db, inv['journal_id'])
    db.execute("UPDATE invoices SET status='void', journal_id=NULL WHERE id=?", (inv['id'],))
    audit(db, f"{inv['kind']}_void", 'invoice', inv['id'], {'number': inv['number'], 'total': inv['total']})
    return {'id': inv['id']}


# ----------------------------------------------------------------------------
# Payments (customer receipts and supplier payments)
# ----------------------------------------------------------------------------

@route('GET', '/api/payments')
def api_payments(db, q, body):
    sql = ('SELECT p.*, c.name AS contact_name, b.name AS bank_name, '
           'EXISTS(SELECT 1 FROM journal_lines l WHERE l.journal_id=p.journal_id AND l.recon_id IS NOT NULL) AS reconciled '
           'FROM payments p JOIN contacts c ON c.id=p.contact_id JOIN accounts b ON b.id=p.bank_account_id WHERE 1=1')
    args = []
    if q.get('kind'):
        sql += ' AND p.kind=?'
        args.append(q['kind'])
    sql += ' ORDER BY p.date DESC, p.id DESC LIMIT 500'
    return rows(db.execute(sql, args))


@route('GET', '/api/payments/:pid')
def api_payment_get(db, q, body, pid):
    p = one(db.execute('SELECT p.*, c.name AS contact_name, b.name AS bank_name FROM payments p '
                       'JOIN contacts c ON c.id=p.contact_id JOIN accounts b ON b.id=p.bank_account_id WHERE p.id=?',
                       (as_int(pid),)))
    if not p:
        raise ApiError('Not found', 404)
    p['allocations'] = rows(db.execute('SELECT a.amount, i.id AS invoice_id, i.number, i.date, i.total FROM allocations a '
                                       'JOIN invoices i ON i.id=a.invoice_id WHERE a.payment_id=?', (p['id'],)))
    p['reconciled'] = bool(scalar(db, 'SELECT 1 FROM journal_lines WHERE journal_id=? AND recon_id IS NOT NULL',
                                  (p['journal_id'],)))
    return p


def create_payment(db, body):
    kind = body.get('kind')
    if kind not in ('receipt', 'payment'):
        raise ApiError('Invalid payment type')
    c = contact(db, req(body, 'contact_id', 'Contact'))
    date = as_date(req(body, 'date'))
    bank = bank_account(db, req(body, 'bank_account_id', 'Bank account'))
    inv_kind = 'sale' if kind == 'receipt' else 'bill'
    total = 0
    clean = []
    for a in body.get('allocations') or []:
        amt = as_cents(a.get('amount', 0))
        if amt == 0:
            continue
        if amt < 0:
            raise ApiError('Payment amounts can\'t be negative')
        inv = get_invoice(db, a.get('invoice_id'))
        if inv['kind'] != inv_kind or inv['contact_id'] != c['id']:
            raise ApiError('That invoice doesn\'t belong to this contact')
        if inv['status'] != 'open':
            raise ApiError(f'{inv["number"] or "That item"} is void')
        if amt > inv['balance']:
            raise ApiError(f'{money(amt)} is more than the {money(inv["balance"])} owing on {inv["number"] or "that bill"}')
        if inv['date'] > date:
            raise ApiError(f'The payment date is before the date of {inv["number"] or "that bill"}')
        clean.append((inv['id'], amt))
        total += amt
    if total <= 0:
        raise ApiError('Enter an amount against at least one invoice')
    ref = text(body.get('reference'))
    cur = db.execute('INSERT INTO payments(kind, contact_id, date, bank_account_id, amount, reference, created_at) '
                     'VALUES (?,?,?,?,?,?,?)', (kind, c['id'], date, bank['id'], total, ref, now()))
    pid = cur.lastrowid
    for iid, amt in clean:
        db.execute('INSERT INTO allocations(payment_id, invoice_id, amount) VALUES (?,?,?)', (pid, iid, amt))
    if kind == 'receipt':
        ctl = system_account(db, 'ar')
        memo = f'Payment from {c["name"]}'
        lines = [{'account_id': bank['id'], 'amount': total, 'role': 'bank', 'description': memo},
                 {'account_id': ctl['id'], 'amount': -total, 'role': 'control', 'description': memo}]
    else:
        ctl = system_account(db, 'ap')
        memo = f'Payment to {c["name"]}'
        lines = [{'account_id': ctl['id'], 'amount': total, 'role': 'control', 'description': memo},
                 {'account_id': bank['id'], 'amount': -total, 'role': 'bank', 'description': memo}]
    jid = post_journal(db, date, kind, lines, memo=memo, reference=ref, contact_id=c['id'], source_id=pid)
    db.execute('UPDATE payments SET journal_id=? WHERE id=?', (jid, pid))
    audit(db, f'{kind}_create', 'payment', pid, {'total': total, 'allocations': clean})
    return pid


@route('POST', '/api/payments')
def api_payment_create(db, q, body):
    return {'id': create_payment(db, body)}


@route('DELETE', '/api/payments/:pid')
def api_payment_delete(db, q, body, pid):
    p = api_payment_get(db, q, body, pid)
    if p['journal_id']:
        delete_journal(db, p['journal_id'])
    db.execute('DELETE FROM payments WHERE id=?', (p['id'],))
    audit(db, f"{p['kind']}_delete", 'payment', p['id'], {'amount': p['amount'], 'date': p['date']})
    return {'ok': True}


# ----------------------------------------------------------------------------
# Banking: spend money, receive money, transfers, general journals
# ----------------------------------------------------------------------------

def build_txn(db, source, body):
    date = as_date(req(body, 'date'))
    memo = text(body.get('memo'))
    ref = text(body.get('reference'))
    cid = None
    if body.get('contact_id'):
        cid = contact(db, body['contact_id'])['id']
    if source in ('spend', 'receive'):
        bank = bank_account(db, req(body, 'bank_account_id', 'Bank account'))
        items = parse_item_lines(db, body.get('lines'))
        for l in items:
            if l['account_id'] == bank['id']:
                raise ApiError('A line can\'t use the same bank account. Use a transfer instead.')
        total = sum(l['amount'] for l in items)
        if total <= 0:
            raise ApiError('The total must be more than zero')
        s = 1 if source == 'spend' else -1
        if not memo:
            memo = items[0]['description'] or ('Spend money' if source == 'spend' else 'Receive money')
        lines = [{'account_id': bank['id'], 'amount': -s * total, 'role': 'bank', 'description': memo}]
        lines += [{'account_id': l['account_id'], 'amount': s * l['amount'], 'description': l['description']} for l in items]
    elif source == 'transfer':
        fa = bank_account(db, req(body, 'from_account_id', 'From account'))
        ta = bank_account(db, req(body, 'to_account_id', 'To account'))
        if fa['id'] == ta['id']:
            raise ApiError('Choose two different accounts')
        amt = as_cents(req(body, 'amount', 'Amount'))
        if amt <= 0:
            raise ApiError('The amount must be more than zero')
        memo = memo or f'Transfer from {fa["name"]} to {ta["name"]}'
        lines = [{'account_id': ta['id'], 'amount': amt, 'role': 'bank', 'description': memo},
                 {'account_id': fa['id'], 'amount': -amt, 'role': 'bank', 'description': memo}]
    elif source == 'journal':
        lines = []
        for i, l in enumerate(body.get('lines') or [], 1):
            dr = as_cents(l.get('debit', 0) or 0, f'Line {i} debit')
            cr = as_cents(l.get('credit', 0) or 0, f'Line {i} credit')
            if dr < 0 or cr < 0:
                raise ApiError(f'Line {i}: use positive numbers in the debit or credit column')
            if dr == 0 and cr == 0:
                continue
            a = account(db, as_int(req(l, 'account_id', f'Line {i} account')))
            if a['subtype'] in ('ar', 'ap'):
                raise ApiError(f'Line {i}: Accounts Receivable/Payable can\'t be used in a journal. Use invoices and bills.')
            lines.append({'account_id': a['id'], 'amount': dr - cr, 'description': text(l.get('description')),
                          'role': 'bank' if a['subtype'] == 'bank' else None})
        if not memo:
            raise ApiError('Add a memo explaining the journal')
    else:
        raise ApiError('Unknown transaction type')
    return {'date': date, 'memo': memo, 'reference': ref, 'contact_id': cid, 'lines': lines}


def create_txn(db, source, body, recurring_id=None):
    p = build_txn(db, source, body)
    jid = post_journal(db, p['date'], source, p['lines'], memo=p['memo'], reference=p['reference'],
                       contact_id=p['contact_id'], recurring_id=recurring_id)
    audit(db, f'{source}_create', 'journal', jid, {'date': p['date'], 'memo': p['memo']})
    return jid


def get_txn(db, jid):
    j = one(db.execute('SELECT j.*, c.name AS contact_name FROM journals j LEFT JOIN contacts c ON c.id=j.contact_id '
                       'WHERE j.id=?', (as_int(jid),)))
    if not j:
        raise ApiError('Transaction not found', 404)
    lines = rows(db.execute('SELECT l.*, a.code AS account_code, a.name AS account_name, a.type AS account_type '
                            'FROM journal_lines l JOIN accounts a ON a.id=l.account_id WHERE journal_id=? ORDER BY l.id',
                            (j['id'],)))
    j['lines'] = lines
    j['reconciled'] = any(l['recon_id'] for l in lines)
    j['locked'] = bool(get_setting(db, 'lock_date') and j['date'] <= get_setting(db, 'lock_date'))
    j['editable'] = j['source'] in EDITABLE_SOURCES and not j['reconciled'] and not j['locked']
    if j['source'] in ('spend', 'receive'):
        s = 1 if j['source'] == 'spend' else -1
        bank = next((l for l in lines if l['role'] == 'bank'), None)
        j['bank_account_id'] = bank['account_id'] if bank else None
        j['items'] = [{'account_id': l['account_id'], 'description': l['description'] or '', 'amount': s * l['amount']}
                      for l in lines if l['role'] != 'bank']
        j['total'] = sum(i['amount'] for i in j['items'])
    elif j['source'] == 'transfer':
        for l in lines:
            if l['amount'] < 0:
                j['from_account_id'] = l['account_id']
            else:
                j['to_account_id'] = l['account_id']
                j['amount'] = l['amount']
        j['total'] = j.get('amount', 0)
    else:
        j['total'] = sum(l['amount'] for l in lines if l['amount'] > 0)
    return j


@route('GET', '/api/journals')
def api_journals(db, q, body):
    sources = [s for s in (q.get('source') or ','.join(EDITABLE_SOURCES)).split(',') if s]
    sql = ('SELECT j.*, c.name AS contact_name, '
           '(SELECT SUM(amount) FROM journal_lines WHERE journal_id=j.id AND amount>0) AS total, '
           "(SELECT GROUP_CONCAT(a.name, ', ') FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
           " WHERE l.journal_id=j.id AND l.role='bank') AS bank_names, "
           'EXISTS(SELECT 1 FROM journal_lines WHERE journal_id=j.id AND recon_id IS NOT NULL) AS reconciled '
           'FROM journals j LEFT JOIN contacts c ON c.id=j.contact_id WHERE j.source IN (%s)' % ','.join('?' * len(sources)))
    args = list(sources)
    if q.get('from'):
        sql += ' AND j.date>=?'
        args.append(as_date(q['from']))
    if q.get('to'):
        sql += ' AND j.date<=?'
        args.append(as_date(q['to']))
    sql += ' ORDER BY j.date DESC, j.id DESC LIMIT 1000'
    return rows(db.execute(sql, args))


@route('GET', '/api/journals/:jid')
def api_journal_get(db, q, body, jid):
    return get_txn(db, jid)


@route('POST', '/api/txn/:source')
def api_txn_create(db, q, body, source):
    if source not in EDITABLE_SOURCES:
        raise ApiError('Unknown transaction type')
    return {'id': create_txn(db, source, body)}


@route('PUT', '/api/journals/:jid')
def api_journal_update(db, q, body, jid):
    j = assert_journal_editable(db, as_int(jid))
    if j['source'] not in EDITABLE_SOURCES:
        raise ApiError('Edit this from its invoice, bill or payment instead')
    p = build_txn(db, j['source'], body)
    check_lock(db, p['date'])
    lines = check_lines(db, p['lines'])
    db.execute('UPDATE journals SET date=?, memo=?, reference=?, contact_id=? WHERE id=?',
               (p['date'], p['memo'] or None, p['reference'] or None, p['contact_id'], j['id']))
    db.execute('DELETE FROM journal_lines WHERE journal_id=?', (j['id'],))
    insert_lines(db, j['id'], lines, p['contact_id'])
    audit(db, f"{j['source']}_update", 'journal', j['id'], {'before_date': j['date'], 'date': p['date']})
    return {'id': j['id']}


@route('DELETE', '/api/journals/:jid')
def api_journal_delete(db, q, body, jid):
    j = assert_journal_editable(db, as_int(jid))
    if j['source'] not in EDITABLE_SOURCES:
        raise ApiError('Delete this from its invoice, bill or payment instead')
    db.execute('DELETE FROM journals WHERE id=?', (j['id'],))
    audit(db, f"{j['source']}_delete", 'journal', j['id'], {'date': j['date'], 'memo': j['memo']})
    return {'ok': True}


@route('GET', '/api/register')
def api_register(db, q, body):
    a = account(db, as_int(req(q, 'account_id', 'Account')))
    s = SIGN[a['type']]
    frm, to = q.get('from'), q.get('to')
    opening = 0
    if frm:
        frm = as_date(frm)
        opening = s * scalar(db, 'SELECT COALESCE(SUM(l.amount),0) FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
                                 'WHERE l.account_id=? AND j.date<?', (a['id'], frm))
    sql = ('SELECT l.id, l.amount, l.description, l.recon_id, j.id AS journal_id, j.date, j.memo, j.reference, '
           'j.source, j.source_id, c.name AS contact_name FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
           'LEFT JOIN contacts c ON c.id=COALESCE(l.contact_id, j.contact_id) WHERE l.account_id=?')
    args = [a['id']]
    if frm:
        sql += ' AND j.date>=?'
        args.append(frm)
    if to:
        sql += ' AND j.date<=?'
        args.append(as_date(to))
    sql += ' ORDER BY j.date, j.id, l.id'
    out = rows(db.execute(sql, args))
    bal = opening
    for r in out:
        bal += s * r['amount']
        r['balance'] = bal
    return {'account': a, 'opening': opening, 'closing': bal, 'lines': out}


# ----------------------------------------------------------------------------
# Bank reconciliation
# ----------------------------------------------------------------------------

@route('GET', '/api/reconcile')
def api_reconcile_get(db, q, body):
    a = bank_account(db, req(q, 'account_id', 'Account'))
    s = SIGN[a['type']]
    last = one(db.execute('SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 1',
                          (a['id'],)))
    cleared = s * scalar(db, 'SELECT COALESCE(SUM(amount),0) FROM journal_lines WHERE account_id=? AND recon_id IS NOT NULL',
                         (a['id'],))
    lines = rows(db.execute('SELECT l.id, l.amount, l.description, j.id AS journal_id, j.date, j.memo, j.reference, j.source, '
                            'j.source_id, c.name AS contact_name FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
                            'LEFT JOIN contacts c ON c.id=COALESCE(l.contact_id, j.contact_id) '
                            'WHERE l.account_id=? AND l.recon_id IS NULL ORDER BY j.date, j.id', (a['id'],)))
    history = rows(db.execute('SELECT r.*, (SELECT COUNT(*) FROM journal_lines WHERE recon_id=r.id) AS line_count '
                              'FROM reconciliations r WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 24',
                              (a['id'],)))
    return {'account': a, 'sign': s, 'last': last, 'cleared_balance': cleared, 'lines': lines, 'history': history}


@route('POST', '/api/reconcile')
def api_reconcile_post(db, q, body):
    a = bank_account(db, req(body, 'account_id', 'Account'))
    s = SIGN[a['type']]
    sdate = as_date(req(body, 'statement_date', 'Statement date'), 'Statement date')
    sbal = as_cents(req(body, 'statement_balance', 'Statement balance'), 'Statement balance')
    last = one(db.execute('SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 1',
                          (a['id'],)))
    if last and sdate < last['statement_date']:
        raise ApiError(f'The statement date is before the last reconciliation ({last["statement_date"]})')
    ids = [as_int(i) for i in body.get('line_ids') or []]
    selected = 0
    for lid in ids:
        l = one(db.execute('SELECT l.*, j.date FROM journal_lines l JOIN journals j ON j.id=l.journal_id WHERE l.id=?', (lid,)))
        if not l or l['account_id'] != a['id']:
            raise ApiError('A selected line doesn\'t belong to this account')
        if l['recon_id']:
            raise ApiError('A selected line is already reconciled')
        if l['date'] > sdate:
            raise ApiError(f'A selected line is dated {l["date"]}, after the statement date')
        selected += l['amount']
    cleared = s * scalar(db, 'SELECT COALESCE(SUM(amount),0) FROM journal_lines WHERE account_id=? AND recon_id IS NOT NULL',
                         (a['id'],))
    calc = cleared + s * selected
    if calc != sbal:
        raise ApiError(f'Out of balance by {money(sbal - calc)}. Calculated {money(calc)} vs statement {money(sbal)}.')
    cur = db.execute('INSERT INTO reconciliations(account_id, statement_date, statement_balance, created_at) VALUES (?,?,?,?)',
                     (a['id'], sdate, sbal, now()))
    rid = cur.lastrowid
    if ids:
        db.execute('UPDATE journal_lines SET recon_id=? WHERE id IN (%s)' % ','.join('?' * len(ids)), [rid] + ids)
    audit(db, 'reconcile', 'account', a['id'], {'statement_date': sdate, 'statement_balance': sbal, 'lines': len(ids)})
    return {'id': rid}


@route('POST', '/api/reconcile/undo')
def api_reconcile_undo(db, q, body):
    a = bank_account(db, req(body, 'account_id', 'Account'))
    last = one(db.execute('SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 1',
                          (a['id'],)))
    if not last or last['note'] == 'opening':
        raise ApiError('There is no reconciliation to undo')
    db.execute('UPDATE journal_lines SET recon_id=NULL WHERE recon_id=?', (last['id'],))
    db.execute('DELETE FROM reconciliations WHERE id=?', (last['id'],))
    audit(db, 'reconcile_undo', 'account', a['id'], last)
    return {'ok': True}


# ----------------------------------------------------------------------------
# Recurring transactions
# ----------------------------------------------------------------------------

def recurring_payload(db, body):
    name = text(body.get('name'))
    if not name:
        raise ApiError('Give the recurring transaction a name')
    kind = body.get('kind')
    if kind not in ('sale', 'bill', 'spend', 'receive'):
        raise ApiError('Choose what kind of transaction this is')
    freq = body.get('frequency')
    if freq not in FREQS:
        raise ApiError('Choose how often it repeats')
    nd = as_date(req(body, 'next_date', 'Next date'), 'Next date')
    ed = as_date(body['end_date'], 'End date') if text(body.get('end_date')) else None
    if ed and ed < nd:
        raise ApiError('The end date is before the next date')
    cid = contact(db, body['contact_id'])['id'] if body.get('contact_id') else None
    bid = None
    if kind in ('sale', 'bill') and not cid:
        raise ApiError('Choose a customer or supplier')
    if kind in ('spend', 'receive'):
        bid = bank_account(db, req(body, 'bank_account_id', 'Bank account'))['id']
    lines = parse_item_lines(db, body.get('lines'))
    if sum(l['amount'] for l in lines) <= 0:
        raise ApiError('The total must be more than zero')
    terms = as_int(body.get('terms_days', 14) or 0, 'Payment terms')
    anchor = as_int(body.get('anchor_day') or D(nd).day)
    if body.get('last_day_of_month'):
        anchor = 31
    return (name, kind, cid, bid, freq, nd, anchor, ed, terms, text(body.get('memo')), json.dumps(lines),
            1 if body.get('active', True) else 0)


@route('GET', '/api/recurring')
def api_recurring(db, q, body):
    out = rows(db.execute('SELECT r.*, c.name AS contact_name, b.name AS bank_name FROM recurring r '
                          'LEFT JOIN contacts c ON c.id=r.contact_id LEFT JOIN accounts b ON b.id=r.bank_account_id '
                          'ORDER BY r.active DESC, r.next_date'))
    for r in out:
        r['lines'] = json.loads(r['lines'])
        r['total'] = sum(l['amount'] for l in r['lines'])
        r['generated_count'] = scalar(db, 'SELECT COUNT(*) FROM journals WHERE recurring_id=?', (r['id'],)) + \
            scalar(db, 'SELECT COUNT(*) FROM invoices WHERE recurring_id=? AND historical=1', (r['id'],))
    return out


@route('GET', '/api/recurring/:rid')
def api_recurring_get(db, q, body, rid):
    r = one(db.execute('SELECT * FROM recurring WHERE id=?', (as_int(rid),)))
    if not r:
        raise ApiError('Not found', 404)
    r['lines'] = json.loads(r['lines'])
    return r


@route('POST', '/api/recurring')
def api_recurring_create(db, q, body):
    p = recurring_payload(db, body)
    cur = db.execute('INSERT INTO recurring(name, kind, contact_id, bank_account_id, frequency, next_date, anchor_day, end_date, '
                     'terms_days, memo, lines, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', p + (now(),))
    audit(db, 'recurring_create', 'recurring', cur.lastrowid, body)
    generated = run_recurring(db) if get_setting(db, 'setup_done') == '1' else []
    return {'id': cur.lastrowid, 'generated': generated}


@route('PUT', '/api/recurring/:rid')
def api_recurring_update(db, q, body, rid):
    r = api_recurring_get(db, q, body, rid)
    p = recurring_payload(db, body)
    db.execute('UPDATE recurring SET name=?, kind=?, contact_id=?, bank_account_id=?, frequency=?, next_date=?, anchor_day=?, '
               'end_date=?, terms_days=?, memo=?, lines=?, active=? WHERE id=?', p + (r['id'],))
    audit(db, 'recurring_update', 'recurring', r['id'], body)
    generated = run_recurring(db) if get_setting(db, 'setup_done') == '1' else []
    return {'id': r['id'], 'generated': generated}


@route('POST', '/api/recurring/:rid/skip')
def api_recurring_skip(db, q, body, rid):
    r = api_recurring_get(db, q, body, rid)
    nd = advance(r['next_date'], r['frequency'], r['anchor_day'])
    db.execute('UPDATE recurring SET next_date=? WHERE id=?', (nd, r['id']))
    audit(db, 'recurring_skip', 'recurring', r['id'], {'skipped': r['next_date']})
    return {'next_date': nd}


@route('DELETE', '/api/recurring/:rid')
def api_recurring_delete(db, q, body, rid):
    r = api_recurring_get(db, q, body, rid)
    db.execute('DELETE FROM recurring WHERE id=?', (r['id'],))
    audit(db, 'recurring_delete', 'recurring', r['id'], {'name': r['name']})
    return {'ok': True}


@route('POST', '/api/recurring/run')
def api_recurring_run(db, q, body):
    return {'generated': run_recurring(db)}


def generate_one(db, t, date):
    lines = json.loads(t['lines'])
    if t['kind'] in ('sale', 'bill'):
        due = (D(date) + dt.timedelta(days=t['terms_days'])).isoformat()
        iid = create_invoice(db, {'contact_id': t['contact_id'], 'date': date, 'due_date': due, 'memo': t['memo'],
                                  'lines': lines}, t['kind'], recurring_id=t['id'])
        return {'type': t['kind'], 'id': iid}
    jid = create_txn(db, t['kind'], {'bank_account_id': t['bank_account_id'], 'date': date, 'contact_id': t['contact_id'],
                                     'memo': t['memo'] or t['name'], 'reference': '', 'lines': lines}, recurring_id=t['id'])
    return {'type': t['kind'], 'id': jid}


def run_recurring(db, upto=None):
    upto = upto or today()
    out = []
    for t in rows(db.execute('SELECT * FROM recurring WHERE active=1 ORDER BY next_date, id')):
        n = 0
        nd = t['next_date']
        while nd <= upto and (not t['end_date'] or nd <= t['end_date']) and n < 400:
            db.execute('SAVEPOINT gen')
            try:
                res = generate_one(db, t, nd)
                db.execute('RELEASE gen')
                total = sum(l['amount'] for l in json.loads(t['lines']))
                item = {'name': t['name'], 'date': nd, 'total': total, **res}
                audit(db, 'recurring_generated', 'recurring', t['id'], item)
                out.append(item)
            except ApiError as e:
                db.execute('ROLLBACK TO gen')
                db.execute('RELEASE gen')
                item = {'name': t['name'], 'date': nd, 'skipped': True, 'reason': str(e)}
                audit(db, 'recurring_skipped', 'recurring', t['id'], item)
                out.append(item)
            nd = advance(nd, t['frequency'], t['anchor_day'])
            n += 1
        active = 0 if (t['end_date'] and nd > t['end_date']) else 1
        db.execute('UPDATE recurring SET next_date=?, active=? WHERE id=?', (nd, active, t['id']))
    set_setting(db, 'last_recurring_run', upto)
    return out


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------

def balances_as_at(db, date):
    return {r[0]: r[1] for r in db.execute('SELECT l.account_id, SUM(l.amount) FROM journal_lines l '
                                           'JOIN journals j ON j.id=l.journal_id WHERE j.date<=? GROUP BY l.account_id',
                                           (date,))}


def profit_between(db, frm, to):
    """Net profit (natural sign) for frm <= date <= to. frm may be None."""
    sql = ('SELECT COALESCE(SUM(l.amount),0) FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
           'JOIN accounts a ON a.id=l.account_id WHERE a.type IN (\'income\',\'cos\',\'expense\') AND j.date<=?')
    args = [to]
    if frm:
        sql += ' AND j.date>=?'
        args.append(frm)
    return -scalar(db, sql, args)


def fy_start(db, date_s):
    m0 = as_int(get_setting(db, 'fy_start_month', '7'))
    d = D(date_s)
    y = d.year if d.month >= m0 else d.year - 1
    return dt.date(y, m0, 1).isoformat()


@route('GET', '/api/reports/pl')
def api_report_pl(db, q, body):
    t = today()[:7]
    months = months_between(q.get('from') or t, q.get('to') or t)
    start, end = month_start(months[0]).isoformat(), month_end(months[-1]).isoformat()
    idx = {m: i for i, m in enumerate(months)}
    accts = {a['id']: a for a in rows(db.execute("SELECT * FROM accounts WHERE type IN ('income','cos','expense') ORDER BY code"))}
    data = {}
    for aid, ym, amt in db.execute(
            'SELECT l.account_id, substr(j.date,1,7), SUM(l.amount) FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
            'JOIN accounts a ON a.id=l.account_id WHERE a.type IN (\'income\',\'cos\',\'expense\') AND j.date BETWEEN ? AND ? '
            'GROUP BY l.account_id, substr(j.date,1,7)', (start, end)):
        data.setdefault(aid, [0] * len(months))[idx[ym]] += SIGN[accts[aid]['type']] * amt
    n = len(months)

    def section(typ, title):
        rs = []
        for aid, a in accts.items():
            if a['type'] == typ and aid in data and any(data[aid]):
                vals = data[aid]
                rs.append({'account_id': aid, 'code': a['code'], 'name': a['name'], 'values': vals, 'total': sum(vals)})
        tot = [sum(r['values'][i] for r in rs) for i in range(n)]
        return {'type': typ, 'title': title, 'rows': rs, 'totals': tot, 'total': sum(tot)}

    inc, cos, exp = section('income', 'Income'), section('cos', 'Cost of Sales'), section('expense', 'Expenses')
    gp = [inc['totals'][i] - cos['totals'][i] for i in range(n)]
    np_ = [gp[i] - exp['totals'][i] for i in range(n)]
    return {'months': months, 'from': start, 'to': end, 'sections': [inc, cos, exp],
            'gross_profit': gp, 'gross_profit_total': sum(gp), 'net_profit': np_, 'net_profit_total': sum(np_),
            'business_name': get_setting(db, 'business_name')}


@route('GET', '/api/reports/bs')
def api_report_bs(db, q, body):
    end_m = q.get('to') or today()[:7]
    count = max(1, min(as_int(q.get('count') or 1, 'Columns'), 13))
    last = month_start(end_m)
    months = [add_months(last, -(count - 1 - i), 1).strftime('%Y-%m') for i in range(count)]
    dates = [month_end(m).isoformat() for m in months]
    accts = rows(db.execute("SELECT * FROM accounts WHERE type IN ('asset','liability','equity') ORDER BY code"))
    cols = [balances_as_at(db, d) for d in dates]
    n = len(dates)
    re_vals, cye_vals = [], []
    for d in dates:
        fs = fy_start(db, d)
        prior = profit_between(db, None, (D(fs) - dt.timedelta(days=1)).isoformat())
        re_vals.append(prior)
        cye_vals.append(profit_between(db, fs, d))

    def section(typ, title):
        rs = []
        for a in accts:
            if a['type'] != typ:
                continue
            vals = [SIGN[typ] * (c.get(a['id']) or 0) for c in cols]
            if a['subtype'] == 're':
                vals = [vals[i] + re_vals[i] for i in range(n)]
            if any(vals) or a['subtype'] == 're':
                rs.append({'account_id': a['id'], 'code': a['code'], 'name': a['name'], 'values': vals,
                           'subtype': a['subtype']})
        if typ == 'equity':
            rs.append({'account_id': None, 'code': '', 'name': 'Current Year Earnings', 'values': cye_vals, 'computed': True})
        tot = [sum(r['values'][i] for r in rs) for i in range(n)]
        return {'type': typ, 'title': title, 'rows': rs, 'totals': tot}

    assets, liabs, equity = section('asset', 'Assets'), section('liability', 'Liabilities'), section('equity', 'Equity')
    net = [assets['totals'][i] - liabs['totals'][i] for i in range(n)]
    return {'months': months, 'dates': dates, 'sections': [assets, liabs, equity], 'net_assets': net,
            'balanced': all(net[i] == equity['totals'][i] for i in range(n)),
            'business_name': get_setting(db, 'business_name')}


@route('GET', '/api/reports/aged')
def api_report_aged(db, q, body):
    kind = 'bill' if q.get('kind') == 'bill' else 'sale'
    as_at = as_date(q.get('as_at') or today(), 'As at date')
    invs = rows(db.execute("SELECT i.*, c.name AS contact_name FROM invoices i JOIN contacts c ON c.id=i.contact_id "
                           "WHERE i.kind=? AND i.status='open' AND i.date<=? ORDER BY c.name COLLATE NOCASE, i.due_date",
                           (kind, as_at)))
    buckets = ['current', 'd30', 'd60', 'd90', 'd90plus']
    groups = {}
    totals = {b: 0 for b in buckets}
    totals['total'] = 0
    for inv in invs:
        bal = inv['total'] - invoice_paid(db, inv['id'], as_at)
        if bal <= 0:
            continue
        days = (D(as_at) - D(inv['due_date'])).days
        b = 'current' if days <= 0 else 'd30' if days <= 30 else 'd60' if days <= 60 else 'd90' if days <= 90 else 'd90plus'
        g = groups.setdefault(inv['contact_id'], {'contact_id': inv['contact_id'], 'contact_name': inv['contact_name'],
                                                  'invoices': [], **{k: 0 for k in buckets}, 'total': 0})
        g['invoices'].append({'id': inv['id'], 'number': inv['number'], 'date': inv['date'], 'due_date': inv['due_date'],
                              'total': inv['total'], 'balance': bal, 'days_overdue': max(days, 0), 'bucket': b})
        g[b] += bal
        g['total'] += bal
        totals[b] += bal
        totals['total'] += bal
    ctl = system_account(db, 'ar' if kind == 'sale' else 'ap')
    ctl_bal = SIGN[ctl['type']] * (balances_as_at(db, as_at).get(ctl['id']) or 0)
    hist_open = scalar(db, 'SELECT COALESCE(SUM(total),0) FROM invoices WHERE kind=? AND historical=1 AND status=\'open\'', (kind,))
    hist_paid = scalar(db, "SELECT COALESCE(SUM(a.amount),0) FROM allocations a JOIN invoices i ON i.id=a.invoice_id "
                           "JOIN payments p ON p.id=a.payment_id WHERE i.kind=? AND i.historical=1 AND p.date<=?", (kind, as_at))
    return {'kind': kind, 'as_at': as_at, 'groups': list(groups.values()), 'totals': totals,
            'control_balance': ctl_bal, 'control_name': ctl['name'],
            'historical_unposted': hist_open - hist_paid, 'business_name': get_setting(db, 'business_name')}


@route('GET', '/api/reports/tb')
def api_report_tb(db, q, body):
    as_at = as_date(q.get('as_at') or today(), 'As at date')
    bal = balances_as_at(db, as_at)
    out = []
    for a in rows(db.execute('SELECT * FROM accounts ORDER BY code')):
        v = bal.get(a['id']) or 0
        if v:
            out.append({'account_id': a['id'], 'code': a['code'], 'name': a['name'], 'type': a['type'],
                        'debit': v if v > 0 else 0, 'credit': -v if v < 0 else 0})
    return {'as_at': as_at, 'rows': out, 'debit': sum(r['debit'] for r in out), 'credit': sum(r['credit'] for r in out),
            'business_name': get_setting(db, 'business_name')}


@route('GET', '/api/dashboard')
def api_dashboard(db, q, body):
    t = today()
    banks = []
    for a in rows(db.execute("SELECT * FROM accounts WHERE subtype='bank' AND archived=0 ORDER BY code")):
        s = SIGN[a['type']]
        bal = s * scalar(db, 'SELECT COALESCE(SUM(amount),0) FROM journal_lines WHERE account_id=?', (a['id'],))
        last = one(db.execute('SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 1',
                              (a['id'],)))
        unc = scalar(db, 'SELECT COUNT(*) FROM journal_lines WHERE account_id=? AND recon_id IS NULL', (a['id'],))
        banks.append({'id': a['id'], 'name': a['name'], 'type': a['type'], 'balance': bal,
                      'last_reconciled': last['statement_date'] if last else None,
                      'last_reconciled_balance': last['statement_balance'] if last else None, 'uncleared': unc})

    def ar_ap(kind):
        tot = over = cnt = 0
        for r in db.execute("SELECT i.total - COALESCE((SELECT SUM(amount) FROM allocations a WHERE a.invoice_id=i.id),0), "
                            "i.due_date FROM invoices i WHERE i.kind=? AND i.status='open'", (kind,)):
            if r[0] > 0:
                tot += r[0]
                cnt += 1
                if r[1] < t:
                    over += r[0]
        return {'total': tot, 'overdue': over, 'count': cnt}

    this_m = t[:7]
    last_m = add_months(month_start(this_m), -1, 1).strftime('%Y-%m')

    def month_pl(ym):
        s, e = month_start(ym).isoformat(), month_end(ym).isoformat()
        inc = -scalar(db, "SELECT COALESCE(SUM(l.amount),0) FROM journal_lines l JOIN journals j ON j.id=l.journal_id "
                          "JOIN accounts a ON a.id=l.account_id WHERE a.type='income' AND j.date BETWEEN ? AND ?", (s, e))
        exp = scalar(db, "SELECT COALESCE(SUM(l.amount),0) FROM journal_lines l JOIN journals j ON j.id=l.journal_id "
                         "JOIN accounts a ON a.id=l.account_id WHERE a.type IN ('cos','expense') AND j.date BETWEEN ? AND ?", (s, e))
        return {'month': ym, 'income': inc, 'expenses': exp, 'profit': inc - exp}

    recent = []
    for r in rows(db.execute("SELECT * FROM audit_log WHERE action IN ('recurring_generated','recurring_skipped') "
                             "ORDER BY id DESC LIMIT 8")):
        d = json.loads(r['detail'] or '{}')
        recent.append(d)
    upcoming = rows(db.execute("SELECT r.id, r.name, r.kind, r.next_date, r.lines, c.name AS contact_name FROM recurring r "
                               "LEFT JOIN contacts c ON c.id=r.contact_id WHERE r.active=1 AND r.next_date<=? "
                               "ORDER BY r.next_date LIMIT 8", ((D(t) + dt.timedelta(days=30)).isoformat(),)))
    for u in upcoming:
        u['total'] = sum(l['amount'] for l in json.loads(u.pop('lines')))
    return {'banks': banks, 'receivable': ar_ap('sale'), 'payable': ar_ap('bill'),
            'this_month': month_pl(this_m), 'last_month': month_pl(last_m),
            'recent_recurring': recent, 'upcoming_recurring': upcoming}


@route('GET', '/api/audit')
def api_audit(db, q, body):
    return rows(db.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 300'))


@route('POST', '/api/backup')
def api_backup(db, q, body):
    fn = backup('manual')
    return {'file': fn}


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'Ledger/1.0'

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode('utf-8'), 'application/json; charset=utf-8')

    def host_ok(self):
        host = (self.headers.get('Host') or '').split(':')[0]
        return host in ('localhost', '127.0.0.1')

    def handle_api(self, method):
        if not self.host_ok() or self.headers.get('X-Ledger') != '1':
            return self.send_json({'error': 'Forbidden'}, 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = {}
        if method in ('POST', 'PUT'):
            n = int(self.headers.get('Content-Length') or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n).decode('utf-8'))
                except Exception:
                    return self.send_json({'error': 'Invalid request'}, 400)
        for m, rx, fn in ROUTES:
            if m != method:
                continue
            mt = rx.match(u.path)
            if not mt:
                continue
            db = connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                res = fn(db, q, body, **mt.groupdict())
                db.execute('COMMIT')
                return self.send_json(res if res is not None else {'ok': True})
            except ApiError as e:
                db.execute('ROLLBACK')
                return self.send_json({'error': str(e)}, e.status)
            except Exception as e:
                db.execute('ROLLBACK')
                traceback.print_exc()
                return self.send_json({'error': f'Unexpected error: {e}'}, 500)
            finally:
                db.close()
        return self.send_json({'error': 'Not found'}, 404)

    def do_GET(self):
        if self.path.startswith('/api/'):
            return self.handle_api('GET')
        if not self.host_ok():
            return self._send(403, b'Forbidden', 'text/plain')
        path = urlparse(self.path).path
        if path in ('', '/'):
            path = '/index.html'
        fp = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip('/')))
        if not fp.startswith(STATIC_DIR + os.sep) or not os.path.isfile(fp):
            return self._send(404, b'Not found', 'text/plain')
        ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript',):
            ctype += '; charset=utf-8'
        with open(fp, 'rb') as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        self.handle_api('POST')

    def do_PUT(self):
        self.handle_api('PUT')

    def do_DELETE(self):
        self.handle_api('DELETE')


def main():
    mimetypes.add_type('application/javascript', '.js')
    mimetypes.add_type('text/css', '.css')
    fresh = not os.path.exists(DB_PATH)
    init_db()
    if not fresh:
        backup('startup')
    db = connect()
    db.execute('BEGIN IMMEDIATE')
    gen = run_recurring(db) if get_setting(db, 'setup_done') == '1' else []
    db.execute('COMMIT')
    db.close()
    url = f'http://localhost:{PORT}/'
    try:
        srv = HTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f'Ledger already seems to be running. Opening {url}')
        webbrowser.open(url)
        return
    print('=' * 60)
    print(f'  Ledger is running at {url}')
    print(f'  Your books: {DB_PATH}')
    if gen:
        print(f'  Created {len([g for g in gen if not g.get("skipped")])} recurring transaction(s)')
    print('  Leave this window open while you work. Close it to stop.')
    print('=' * 60)
    if '--no-browser' not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
