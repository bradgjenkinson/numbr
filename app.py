#!/usr/bin/env python3
"""
numbr - simple local bookkeeping for a small business.

Runs entirely on this computer using only the Python standard library and SQLite.
Start it with start.bat (Windows) or:  python app.py
Your books live in data/books.db. Backups go to data/backups/.
"""
import calendar
import datetime as dt
import hashlib
import itertools
import json
import mimetypes
import os
import re
import socketserver
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


def code_stamp():
    """Fingerprint of app.py on disk. Lets a running server tell that numbr has been updated since it started."""
    try:
        with open(os.path.abspath(__file__), 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ''


STARTED_STAMP = code_stamp()

# Natural sign: +1 means a debit increases the account's balance.
SIGN = {'asset': 1, 'cos': 1, 'expense': 1, 'liability': -1, 'equity': -1, 'income': -1}
PL_TYPES = ('income', 'cos', 'expense')
FREQS = ('weekly', 'fortnightly', 'monthly', 'quarterly', 'yearly')
SYSTEM_SUBTYPES = ('ar', 'ap', 're', 'hist', 'partner')
# Account codes are backend-only (users see names). New codes start with the MYOB-style type digit.
CODE_PREFIX = {'asset': '1', 'liability': '2', 'equity': '3', 'income': '4', 'cos': '5', 'expense': '6'}
TYPE_ORDER_SQL = ("CASE type WHEN 'asset' THEN 1 WHEN 'liability' THEN 2 WHEN 'equity' THEN 3 "
                  "WHEN 'income' THEN 4 WHEN 'cos' THEN 5 ELSE 6 END")
EDITABLE_SOURCES = ('spend', 'receive', 'transfer', 'journal')
# For a bank line a rule created: the account(s) on the other side, so the user can see how it was categorised.
RULE_CATEGORY_SQL = ("CASE WHEN j.rule_id IS NOT NULL THEN (CASE WHEN j.source='transfer' THEN 'Transfer: ' ELSE '' END) || "
                     "(SELECT GROUP_CONCAT(a2.name, ', ') "
                     'FROM journal_lines l2 JOIN accounts a2 ON a2.id=l2.account_id '
                     'WHERE l2.journal_id=j.id AND l2.id<>l.id) END AS category')
SHARE_FULL = 10000          # share_bp of an account the user owns outright
PARTNER = 0                 # pseudo account id for other owners' share of shared bank accounts
PARTNER_NAME = "Other owners' share"
# The 'partner' system account holds money other owners put into accounts the user tracks (e.g. a partner's
# deposits into a joint account). Reports fold it into the PARTNER line, so it never touches the P&L.
PARTNER_ACCOUNT = ('3-9990', 'Partner Contributions')


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
  archived INTEGER NOT NULL DEFAULT 0,
  share_bp INTEGER NOT NULL DEFAULT 10000,  -- shared bank accounts: user's share in basis points (10000 = 100%)
  csv_map TEXT,                             -- bank accounts: JSON column choices from the last statement CSV import
  group_id INTEGER REFERENCES account_groups(id)  -- optional group for subtotals (presentation only)
);
-- Account groups only change how the chart and reports are laid out (headings and subtotals); they never
-- affect balances. A group holds accounts of one type and may sit inside another group of the same type.
CREATE TABLE IF NOT EXISTS account_groups (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','cos','expense')),
  parent_id INTEGER REFERENCES account_groups(id)
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
  share_bp INTEGER NOT NULL DEFAULT 10000,  -- user's share of a shared invoice/bill in basis points (10000 = 100%)
  partner INTEGER NOT NULL DEFAULT 0,       -- 1 = partner contribution (kind 'sale'): posts no journal, payments credit the partner account
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
  share_bp INTEGER NOT NULL DEFAULT 10000,  -- sale/bill templates: user's share of each invoice/bill it creates
  partner INTEGER NOT NULL DEFAULT 0,       -- 1 = creates partner contributions (kind 'sale')
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_rules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  priority INTEGER NOT NULL,                -- lower runs first; the first matching rule wins
  mode TEXT NOT NULL CHECK (mode IN ('auto','recommend')),
  text_op TEXT CHECK (text_op IN ('contains','equals')),  -- NULL = no text condition
  text_tokens TEXT,                         -- JSON pattern tokens from the rule builder (compiled to a regex)
  direction TEXT NOT NULL DEFAULT 'any' CHECK (direction IN ('any','out','in')),
  amount_op TEXT CHECK (amount_op IN ('eq','lt','gt')),   -- NULL = no amount condition; compares the size of the amount
  amount INTEGER,
  account_ids TEXT,                         -- JSON list of bank account ids; NULL = all bank accounts
  target_account_id INTEGER NOT NULL REFERENCES accounts(id),
  contact_id INTEGER REFERENCES contacts(id),
  memo TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  archived INTEGER NOT NULL DEFAULT 0,      -- deleted rules that created transactions are archived so history keeps the name
  created_at TEXT NOT NULL
);
-- Budgets are planning data only: they never touch journals, balances or the lock date.
-- One budget per financial year (12 months from fy_start).
CREATE TABLE IF NOT EXISTS budgets (
  id INTEGER PRIMARY KEY,
  fy_start TEXT NOT NULL UNIQUE,            -- 'YYYY-MM', first month of the financial year
  name TEXT NOT NULL,
  history_from TEXT,                        -- 'YYYY-MM' months of actuals the builder used (NULL = no history)
  history_to TEXT,
  created_at TEXT NOT NULL
);
-- How the user chose to build each account's budget. Only used to set up next year's builder;
-- it is never re-applied to a budget that has been built.
CREATE TABLE IF NOT EXISTS budget_methods (
  id INTEGER PRIMARY KEY,
  budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  method TEXT NOT NULL CHECK (method IN ('average','last','schedule','manual','none')),
  config TEXT,                              -- JSON: schedule segments, or {"amounts": [12 cents]} for manual
  UNIQUE (budget_id, account_id)
);
-- The budget itself: natural-sign cents per account per month (zero months aren't stored). Edited by hand.
CREATE TABLE IF NOT EXISTS budget_amounts (
  budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  month TEXT NOT NULL,
  amount INTEGER NOT NULL,
  PRIMARY KEY (budget_id, account_id, month)
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
    ('1-1100', 'Bank 1', 'asset', 'bank'),
    ('1-1300', 'Accounts Receivable', 'asset', 'ar'),
    ('1-2000', 'Equipment', 'asset', 'other'),
    ('1-2100', 'Less Accumulated Depreciation', 'asset', 'other'),
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
    cols = {r[1] for r in db.execute('PRAGMA table_info(accounts)')}
    if 'share_bp' not in cols:
        db.execute('ALTER TABLE accounts ADD COLUMN share_bp INTEGER NOT NULL DEFAULT 10000')
    if 'csv_map' not in cols:
        db.execute('ALTER TABLE accounts ADD COLUMN csv_map TEXT')
    if 'group_id' not in cols:
        db.execute('ALTER TABLE accounts ADD COLUMN group_id INTEGER REFERENCES account_groups(id)')
    for table in ('invoices', 'recurring'):
        tcols = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        if 'share_bp' not in tcols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN share_bp INTEGER NOT NULL DEFAULT 10000')
        if 'partner' not in tcols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN partner INTEGER NOT NULL DEFAULT 0')
    jcols = {r[1] for r in db.execute('PRAGMA table_info(journals)')}
    if 'rule_id' not in jcols:
        db.execute('ALTER TABLE journals ADD COLUMN rule_id INTEGER')
    if 'rule_mode' not in jcols:
        db.execute('ALTER TABLE journals ADD COLUMN rule_mode TEXT')
    if 'reviewed' not in jcols:
        db.execute('ALTER TABLE journals ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0')
    lcols = {r[1] for r in db.execute('PRAGMA table_info(journal_lines)')}
    if 'bank_text' not in lcols:
        db.execute('ALTER TABLE journal_lines ADD COLUMN bank_text TEXT')
    if 'bank_date' not in lcols:  # the statement line's date, when it differs from the transaction date
        db.execute('ALTER TABLE journal_lines ADD COLUMN bank_date TEXT')
    db.execute('BEGIN')
    for k, v in DEFAULT_SETTINGS.items():
        db.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', (k, v))
    if scalar(db, 'SELECT COUNT(*) FROM accounts') == 0:
        db.executemany('INSERT INTO accounts(code, name, type, subtype) VALUES (?,?,?,?)', DEFAULT_ACCOUNTS)
    if not scalar(db, "SELECT COUNT(*) FROM accounts WHERE subtype='partner'"):
        code, name = PARTNER_ACCOUNT
        n = 0
        while scalar(db, 'SELECT 1 FROM accounts WHERE code=?', (code,)):
            n += 1
            code = f'3-99{90 + n}' if n < 9 else f'3-P{n}'
        db.execute("INSERT INTO accounts(code, name, type, subtype) VALUES (?,?,'equity','partner')", (code, name))
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
            raise ApiError(f'The account “{a["name"]}” is archived. Un-archive it in the chart of accounts, or choose another account.')
    return lines


def insert_lines(db, jid, lines, contact_id):
    for l in lines:
        db.execute('INSERT INTO journal_lines(journal_id, account_id, amount, description, contact_id, role, bank_text, bank_date) '
                   'VALUES (?,?,?,?,?,?,?,?)',
                   (jid, l['account_id'], l['amount'], l.get('description') or None,
                    l.get('contact_id', contact_id), l.get('role'), l.get('bank_text') or None, l.get('bank_date') or None))


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
    accts = rows(db.execute('SELECT * FROM accounts ORDER BY name COLLATE NOCASE'))
    bal = {r[0]: r[1] for r in db.execute('SELECT account_id, SUM(amount) FROM journal_lines GROUP BY account_id')}
    used = {r[0] for r in db.execute('SELECT DISTINCT account_id FROM invoice_lines')} | \
           {r[0] for r in db.execute('SELECT DISTINCT account_id FROM journal_lines')}
    for a in accts:
        a['balance'] = SIGN[a['type']] * (bal.get(a['id']) or 0)
        a['used'] = a['id'] in used
        a['system'] = a['subtype'] in SYSTEM_SUBTYPES
        try:
            a['csv_map'] = json.loads(a['csv_map']) if a.get('csv_map') else None
        except ValueError:
            a['csv_map'] = None
    return accts


def list_contacts(db):
    cs = rows(db.execute('SELECT * FROM contacts ORDER BY name COLLATE NOCASE'))
    outstanding = {}
    for r in db.execute("""SELECT i.contact_id, CASE WHEN i.partner=1 THEN 'partner' ELSE i.kind END,
                                  SUM(i.total - COALESCE((SELECT SUM(amount) FROM allocations a WHERE a.invoice_id=i.id),0))
                           FROM invoices i WHERE i.status='open' GROUP BY 1, 2"""):
        outstanding[(r[0], r[1])] = r[2]
    for c in cs:
        c['receivable'] = outstanding.get((c['id'], 'sale'), 0) or 0
        c['payable'] = outstanding.get((c['id'], 'bill'), 0) or 0
        c['contribution_due'] = outstanding.get((c['id'], 'partner'), 0) or 0
    return cs


@route('GET', '/api/state')
def api_state(db, q, body):
    s = all_settings(db)
    generated = []
    if s.get('setup_done') == '1' and s.get('last_recurring_run') != today():
        generated = run_recurring(db)
        s = all_settings(db)
    return {'settings': s, 'accounts': list_accounts(db), 'contacts': list_contacts(db),
            'groups': rows(db.execute('SELECT * FROM account_groups ORDER BY name COLLATE NOCASE')),
            'today': today(), 'generated': generated, 'server_current': code_stamp() == STARTED_STAMP}


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
    hist_sales = scalar(db, "SELECT COALESCE(SUM(total),0) FROM invoices WHERE kind='sale' AND partner=0 AND historical=1 "
                            "AND status='open'")
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


def share_from_body(body, default=SHARE_FULL):
    """Read body['share_pct'] (the user's share as a percentage) and return it in basis points."""
    if body.get('share_pct') in (None, ''):
        return default
    try:
        pct = float(body['share_pct'])
    except (TypeError, ValueError):
        raise ApiError('Your share must be a percentage, e.g. 50')
    if not 0 < pct <= 100:
        raise ApiError('Your share must be more than 0% and no more than 100%')
    return int(round(pct * 100))


def name_key(name):
    """Account names compare ignoring case and extra spaces, so 'Bank  fees' clashes with 'Bank Fees'."""
    return ' '.join((name or '').split()).casefold()


def next_account_code(db, typ):
    """A free code for a new account: the type digit, then 10 past the highest number in use."""
    p = CODE_PREFIX[typ]
    used = {r[0] for r in db.execute('SELECT code FROM accounts')}
    nums = [int(c[2:]) for c in used if re.fullmatch(p + r'-\d{4}', c)]
    n = max(nums, default=990) + 10
    while n <= 9999:
        if f'{p}-{n:04d}' not in used:
            return f'{p}-{n:04d}'
        n += 1
    k = 1
    while f'{p}-X{k}' in used:
        k += 1
    return f'{p}-X{k}'


def account_payload(db, body, existing=None):
    name = ' '.join(text(body.get('name', existing and existing['name'])).split())
    if not name:
        raise ApiError('Give the account a name')
    typ = body.get('type', existing and existing['type'])
    if typ not in SIGN:
        raise ApiError('Choose an account type')
    code = text(body.get('code')) or (existing and existing['code']) or next_account_code(db, typ)
    # Users pick accounts by name, so two accounts can't share one. Existing duplicates are left alone
    # until one is renamed, so they don't block other edits.
    if not existing or name_key(name) != name_key(existing['name']):
        for other in db.execute('SELECT id, name, archived FROM accounts WHERE id<>?', (existing['id'] if existing else 0,)):
            if name_key(other[1]) == name_key(name):
                msg = f"There's already an account called “{other[1]}”. Choose a different name so the two can't be mixed up."
                if other[2]:
                    msg += ' That account is archived; you could un-archive it instead.'
                raise ApiError(msg)
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
        raise ApiError(f'Account code {code} is already used. Leave the code out and numbr will choose one.')
    share = SHARE_FULL
    if sub == 'bank':
        share = share_from_body(body, existing['share_bp'] if existing else SHARE_FULL)
    if 'group_id' in body:
        gid = body.get('group_id')
        gid = None if gid in (None, '') else as_int(gid, 'Group')
        if gid is not None and account_group(db, gid)['type'] != typ:
            raise ApiError('That group holds a different type of account. Choose a group of the same type, or no group.')
    else:
        gid = existing and existing['group_id']
        if gid and account_group(db, gid)['type'] != typ:
            gid = None  # the account changed type, so it leaves a group of the old type
    if body.get('new_group'):  # "+ New group…" in the account form: create it in the same transaction
        ng = body['new_group'] if isinstance(body['new_group'], dict) else {}
        gname, _, gpid, _ = group_payload(db, {'name': ng.get('name'), 'type': typ, 'parent_id': ng.get('parent_id')})
        gid = db.execute('INSERT INTO account_groups(name, type, parent_id) VALUES (?,?,?)', (gname, typ, gpid)).lastrowid
        audit(db, 'group_create', 'account_group', gid, {'name': gname, 'type': typ, 'parent_id': gpid})
    return code, name, typ, sub, share, gid


@route('POST', '/api/accounts')
def api_account_create(db, q, body):
    code, name, typ, sub, share, gid = account_payload(db, body)
    cur = db.execute('INSERT INTO accounts(code, name, type, subtype, share_bp, group_id) VALUES (?,?,?,?,?,?)',
                     (code, name, typ, sub, share, gid))
    audit(db, 'account_create', 'account', cur.lastrowid, body)
    return {'id': cur.lastrowid}


@route('PUT', '/api/accounts/:aid')
def api_account_update(db, q, body, aid):
    a = account(db, as_int(aid))
    code, name, typ, sub, share, gid = account_payload(db, body, a)
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
            raise ApiError('Only accounts with a zero balance can be archived. Move the balance to another account first.')
    db.execute('UPDATE accounts SET code=?, name=?, type=?, subtype=?, archived=?, share_bp=?, group_id=? WHERE id=?',
               (code, name, typ, sub, archived, share, gid, a['id']))
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
    if (scalar(db, 'SELECT COUNT(*) FROM payments WHERE bank_account_id=?', (a['id'],)) or
            scalar(db, 'SELECT COUNT(*) FROM reconciliations WHERE account_id=?', (a['id'],))):
        raise ApiError('This account has been used. Archive it instead.')
    if scalar(db, 'SELECT COUNT(*) FROM recurring WHERE bank_account_id=?', (a['id'],)):
        raise ApiError('This account is used by a recurring transaction. Change or delete that first.')
    for r in db.execute('SELECT lines FROM recurring'):
        if any(int(l['account_id']) == a['id'] for l in json.loads(r[0])):
            raise ApiError('This account is used by a recurring transaction')
    if (scalar(db, 'SELECT COUNT(*) FROM budget_amounts WHERE account_id=?', (a['id'],)) or
            scalar(db, "SELECT COUNT(*) FROM budget_methods WHERE account_id=? AND method<>'none'", (a['id'],))):
        raise ApiError('This account is in a budget. Archive it instead.')
    db.execute('DELETE FROM budget_methods WHERE account_id=?', (a['id'],))
    db.execute('UPDATE contacts SET default_account_id=NULL WHERE default_account_id=?', (a['id'],))
    db.execute('DELETE FROM accounts WHERE id=?', (a['id'],))
    audit(db, 'account_delete', 'account', a['id'], a)
    return {'ok': True}


# Account groups: headings with subtotals in the chart of accounts and reports. Presentation only.
GROUP_MAX_DEPTH = 4


def account_group(db, gid):
    g = one(db.execute('SELECT * FROM account_groups WHERE id=?', (gid,)))
    if not g:
        raise ApiError('Group not found. Refresh the page and try again.')
    return g


def group_depth(db, gid):
    """Levels from this group up to the top (a top-level group is 1)."""
    n = 0
    while gid is not None:
        n += 1
        gid = scalar(db, 'SELECT parent_id FROM account_groups WHERE id=?', (gid,))
    return n


def group_height(db, gid):
    """Levels from this group down to its deepest subgroup (a group with no subgroups is 1)."""
    kids = [r[0] for r in db.execute('SELECT id FROM account_groups WHERE parent_id=?', (gid,))]
    return 1 + max((group_height(db, k) for k in kids), default=0)


def sibling_named(db, typ, pid, name, exclude=0):
    return scalar(db, 'SELECT id FROM account_groups WHERE type=? AND parent_id IS ? AND name=? COLLATE NOCASE '
                      'AND id<>?', (typ, pid, name, exclude))


def group_payload(db, body, existing=None):
    name = text(body.get('name', existing and existing['name']))
    if not name:
        raise ApiError('Give the group a name')
    typ = existing['type'] if existing else body.get('type')
    if typ not in SIGN:
        raise ApiError('Choose an account type for the group')
    if 'parent_id' in body:
        pid = body.get('parent_id')
        pid = None if pid in (None, '') else as_int(pid, 'Parent group')
    else:
        pid = existing and existing['parent_id']
    if pid is not None:
        if account_group(db, pid)['type'] != typ:
            raise ApiError('A group can only sit inside a group of the same account type')
        if existing:
            up = pid
            while up is not None:
                if up == existing['id']:
                    raise ApiError("A group can't sit inside itself or one of its own subgroups")
                up = scalar(db, 'SELECT parent_id FROM account_groups WHERE id=?', (up,))
        height = group_height(db, existing['id']) if existing else 1
        if group_depth(db, pid) + height > GROUP_MAX_DEPTH:
            raise ApiError(f'Groups can only go {GROUP_MAX_DEPTH} levels deep. Choose a higher-level group to put it in.')
    if sibling_named(db, typ, pid, name, existing['id'] if existing else 0):
        raise ApiError(f'There is already a group called "{name}" here. Choose another name.')
    ids = None
    if 'account_ids' in body:
        ids = []
        for v in body.get('account_ids') or []:
            a = account(db, as_int(v, 'Account'))
            if a['type'] != typ:
                raise ApiError(f"{a['name']} is a different type of account, so it can't go in this group")
            ids.append(a['id'])
    return name, typ, pid, ids


def set_group_accounts(db, gid, ids):
    db.execute('UPDATE accounts SET group_id=NULL WHERE group_id=?', (gid,))
    for aid in ids:
        db.execute('UPDATE accounts SET group_id=? WHERE id=?', (gid, aid))


@route('POST', '/api/groups')
def api_group_create(db, q, body):
    name, typ, pid, ids = group_payload(db, body)
    cur = db.execute('INSERT INTO account_groups(name, type, parent_id) VALUES (?,?,?)', (name, typ, pid))
    if ids is not None:
        set_group_accounts(db, cur.lastrowid, ids)
    audit(db, 'group_create', 'account_group', cur.lastrowid, body)
    return {'id': cur.lastrowid}


@route('PUT', '/api/groups/:gid')
def api_group_update(db, q, body, gid):
    g = account_group(db, as_int(gid))
    name, typ, pid, ids = group_payload(db, body, g)
    db.execute('UPDATE account_groups SET name=?, parent_id=? WHERE id=?', (name, pid, g['id']))
    if ids is not None:
        set_group_accounts(db, g['id'], ids)
    audit(db, 'group_update', 'account_group', g['id'], body)
    return {'id': g['id']}


@route('DELETE', '/api/groups/:gid')
def api_group_delete(db, q, body, gid):
    """Remove a group. Its accounts and subgroups move up into its parent (or become ungrouped)."""
    g = account_group(db, as_int(gid))
    for sub_id, sub_name in db.execute('SELECT id, name FROM account_groups WHERE parent_id=?', (g['id'],)).fetchall():
        if sibling_named(db, g['type'], g['parent_id'], sub_name, g['id']):
            raise ApiError(f'A group called "{sub_name}" already exists one level up. Rename one of them first.')
    db.execute('UPDATE accounts SET group_id=? WHERE group_id=?', (g['parent_id'], g['id']))
    db.execute('UPDATE account_groups SET parent_id=? WHERE parent_id=?', (g['parent_id'], g['id']))
    db.execute('DELETE FROM account_groups WHERE id=?', (g['id'],))
    audit(db, 'group_delete', 'account_group', g['id'], g)
    return {'ok': True}


def contact_payload(db, body):
    name = text(body.get('name'))
    if not name:
        raise ApiError('Name is required')
    kind = body.get('kind') or 'customer'
    if kind not in ('customer', 'supplier', 'both'):
        raise ApiError('Invalid contact type')
    da = body.get('default_account_id') or None
    if da:
        account(db, as_int(da))
    return (name, kind, text(body.get('phone')), text(body.get('address')), da, text(body.get('notes')), 1 if body.get('archived') else 0)


@route('GET', '/api/contacts')
def api_contacts(db, q, body):
    return list_contacts(db)


@route('POST', '/api/contacts')
def api_contact_create(db, q, body):
    p = contact_payload(db, body)
    cur = db.execute('INSERT INTO contacts(name, kind, phone, address, default_account_id, notes, archived) '
                     'VALUES (?,?,?,?,?,?,?)', p)
    audit(db, 'contact_create', 'contact', cur.lastrowid, body)
    return {'id': cur.lastrowid}


@route('PUT', '/api/contacts/:cid')
def api_contact_update(db, q, body, cid):
    c = contact(db, cid)
    p = contact_payload(db, body)
    db.execute('UPDATE contacts SET name=?, kind=?, phone=?, address=?, default_account_id=?, '
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


def partner_amount(body):
    """The amount of a partner contribution: body['amount'], or the total of body['lines'] (recurring templates)."""
    if body.get('amount') not in (None, ''):
        return as_cents(body['amount'])
    return sum(as_cents(l.get('amount', 0)) for l in body.get('lines') or [])


def invoice_payload(db, body, kind, partner=False):
    c = contact(db, req(body, 'contact_id', 'Contact'))
    date = as_date(req(body, 'date'))
    due = as_date(body['due_date'], 'Due date') if body.get('due_date') else date
    if due < date:
        raise ApiError('The due date is before the date')
    if partner:
        # A partner contribution has one line in the partner account. It's only a reminder until it's paid.
        total = partner_amount(body)
        lines = [{'account_id': system_account(db, 'partner')['id'], 'amount': total,
                  'description': text(body.get('description')) or text(body.get('memo')) or 'Contribution'}]
    else:
        lines = parse_item_lines(db, body.get('lines'))
        total = sum(l['amount'] for l in lines)
    if total <= 0:
        raise ApiError('The total must be more than zero')
    return {'kind': kind, 'contact_id': c['id'], 'contact_name': c['name'], 'number': text(body.get('number')),
            'date': date, 'due_date': due, 'memo': text(body.get('memo')), 'lines': lines, 'total': total,
            'share_bp': SHARE_FULL if partner else share_from_body(body), 'partner': 1 if partner else 0}


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
    if p['kind'] == 'sale' and p['number'] and not p['partner']:
        if scalar(db, "SELECT 1 FROM invoices WHERE kind='sale' AND number=? AND id<>?", (p['number'], exclude_id)):
            raise ApiError(f'Invoice number {p["number"]} is already used')


def create_invoice(db, body, kind, recurring_id=None):
    """kind is 'sale', 'bill' or 'partner' (a partner contribution, stored as a 'sale' with partner=1)."""
    partner = kind == 'partner'
    if partner:
        kind = 'sale'
    p = invoice_payload(db, body, kind, partner)
    if partner:
        check_lock(db, p['date'])
    elif kind == 'sale':
        if p['number']:
            check_number_unique(db, p)
            bump_invoice_number(db, p['number'])
        else:
            p['number'] = next_invoice_number(db)
    hist = is_historical(db, p['date']) and not partner
    cur = db.execute('INSERT INTO invoices(kind, contact_id, number, date, due_date, memo, total, historical, recurring_id, '
                     'share_bp, partner, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (kind, p['contact_id'], p['number'], p['date'], p['due_date'], p['memo'], p['total'],
                      1 if hist else 0, recurring_id, p['share_bp'], p['partner'], now()))
    iid = cur.lastrowid
    for l in p['lines']:
        db.execute('INSERT INTO invoice_lines(invoice_id, description, account_id, amount) VALUES (?,?,?,?)',
                   (iid, l['description'], l['account_id'], l['amount']))
    if partner:
        audit(db, 'partner_create', 'invoice', iid, {'number': p['number'], 'total': p['total']})
        return iid
    if not hist:
        lines, label = invoice_journal_lines(db, p)
        jid = post_journal(db, p['date'], kind, lines, memo=label, reference=p['number'],
                           contact_id=p['contact_id'], source_id=iid, recurring_id=recurring_id)
        db.execute('UPDATE invoices SET journal_id=? WHERE id=?', (jid, iid))
    audit(db, f'{kind}_create', 'invoice', iid, {'number': p['number'], 'total': p['total'], 'historical': hist,
                                                  'share_bp': p['share_bp']})
    return iid


def get_invoice(db, iid):
    inv = one(db.execute('SELECT i.*, c.name AS contact_name, c.address AS contact_address '
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
    args = ['sale', 1] if kind == 'partner' else [kind, 0]
    sql = ('SELECT i.*, c.name AS contact_name, '
           'COALESCE((SELECT SUM(amount) FROM allocations a WHERE a.invoice_id=i.id),0) AS paid '
           'FROM invoices i JOIN contacts c ON c.id=i.contact_id WHERE i.kind=? AND i.partner=?')
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
    if kind not in ('sale', 'bill', 'partner'):
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
    partner = bool(inv['partner'])
    p = invoice_payload(db, {'share_pct': inv['share_bp'] / 100, **body}, inv['kind'], partner)
    if partner:
        check_lock(db, inv['date'])
        check_lock(db, p['date'])
    elif inv['kind'] == 'sale':
        p['number'] = p['number'] or inv['number']
        check_number_unique(db, p, inv['id'])
    hist = is_historical(db, p['date']) and not partner
    db.execute('UPDATE invoices SET contact_id=?, number=?, date=?, due_date=?, memo=?, total=?, historical=?, share_bp=?, '
               'journal_id=NULL WHERE id=?', (p['contact_id'], p['number'], p['date'], p['due_date'], p['memo'], p['total'],
                                              1 if hist else 0, p['share_bp'], inv['id']))
    db.execute('DELETE FROM invoice_lines WHERE invoice_id=?', (inv['id'],))
    for l in p['lines']:
        db.execute('INSERT INTO invoice_lines(invoice_id, description, account_id, amount) VALUES (?,?,?,?)',
                   (inv['id'], l['description'], l['account_id'], l['amount']))
    if not hist and not partner:
        lines, label = invoice_journal_lines(db, p)
        jid = post_journal(db, p['date'], inv['kind'], lines, memo=label, reference=p['number'],
                           contact_id=p['contact_id'], source_id=inv['id'], recurring_id=inv['recurring_id'])
        db.execute('UPDATE invoices SET journal_id=? WHERE id=?', (jid, inv['id']))
    audit(db, f"{inv['kind']}_update", 'invoice', inv['id'], {'before': {k: inv[k] for k in ('number', 'date', 'total')},
                                                              'after': {'number': p['number'], 'date': p['date'], 'total': p['total'],
                                                                        'share_bp': p['share_bp']}})
    return {'id': inv['id']}


@route('POST', '/api/invoices/:iid/void')
def api_invoice_void(db, q, body, iid):
    inv = get_invoice(db, iid)
    if inv['status'] == 'void':
        return {'id': inv['id']}
    if inv['payments']:
        raise ApiError('This has payments recorded against it. Delete the payments first.')
    if inv['partner']:
        check_lock(db, inv['date'])
    if inv['journal_id']:
        delete_journal(db, inv['journal_id'])
    db.execute("UPDATE invoices SET status='void', journal_id=NULL WHERE id=?", (inv['id'],))
    audit(db, f"{inv['kind']}_void", 'invoice', inv['id'], {'number': inv['number'], 'total': inv['total']})
    return {'id': inv['id']}


def convert_to_partner(db, inv):
    """Turn a customer invoice into a partner contribution: remove the invoice's journal and move its payments'
    Accounts Receivable lines to the partner account. Bank lines aren't touched, so reconciliations stay valid."""
    if inv['kind'] != 'sale' or inv['partner']:
        raise ApiError('Only customer invoices can be converted')
    if inv['status'] != 'open':
        raise ApiError('This invoice is void')
    if inv['historical']:
        raise ApiError('This is an opening item, so it is part of your opening Accounts Receivable balance and '
                       "can't be converted. Record the partner's amount as a new partner contribution instead.")
    check_lock(db, inv['date'])
    pacct = system_account(db, 'partner')
    for pay in inv['payments']:
        if scalar(db, 'SELECT COUNT(*) FROM allocations WHERE payment_id=? AND invoice_id<>?',
                  (pay['payment_id'], inv['id'])):
            raise ApiError(f'The payment dated {pay["date"]} also pays other invoices. '
                           'Delete it and record a separate payment for each invoice first.')
        check_lock(db, pay['date'])
    if inv['journal_id']:
        # Invoice journals have no bank lines, so they are never reconciled.
        delete_journal(db, inv['journal_id'])
    memo = f'Contribution from {inv["contact_name"]}'
    for pay in inv['payments']:
        jid = scalar(db, 'SELECT journal_id FROM payments WHERE id=?', (pay['payment_id'],))
        db.execute("UPDATE journal_lines SET account_id=?, description=? WHERE journal_id=? AND role='control'",
                   (pacct['id'], memo, jid))
        db.execute("UPDATE journal_lines SET description=? WHERE journal_id=? AND role='bank'", (memo, jid))
        db.execute('UPDATE journals SET memo=? WHERE id=?', (memo, jid))
    desc = ' / '.join(l['description'] for l in inv['lines'] if l['description']) or 'Contribution'
    db.execute('DELETE FROM invoice_lines WHERE invoice_id=?', (inv['id'],))
    db.execute('INSERT INTO invoice_lines(invoice_id, description, account_id, amount) VALUES (?,?,?,?)',
               (inv['id'], desc, pacct['id'], inv['total']))
    db.execute('UPDATE invoices SET partner=1, share_bp=?, historical=0, journal_id=NULL WHERE id=?',
               (SHARE_FULL, inv['id']))
    audit(db, 'sale_to_partner', 'invoice', inv['id'],
          {'number': inv['number'], 'total': inv['total'], 'journal_id': inv['journal_id'],
           'lines': [{k: l[k] for k in ('account_id', 'description', 'amount')} for l in inv['lines']],
           'share_bp': inv['share_bp'],
           'payments': [p['payment_id'] for p in inv['payments']]})


@route('POST', '/api/invoices/:iid/to-partner')
def api_invoice_to_partner(db, q, body, iid):
    inv = get_invoice(db, iid)
    convert_to_partner(db, inv)
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
    partner_sql = ('EXISTS(SELECT 1 FROM allocations a JOIN invoices i ON i.id=a.invoice_id '
                   'WHERE a.payment_id=p.id AND i.partner=1)')
    if q.get('kind') == 'contribution':
        sql += f" AND p.kind='receipt' AND {partner_sql}"
    elif q.get('kind'):
        sql += ' AND p.kind=?'
        args.append(q['kind'])
        if q['kind'] == 'receipt':
            sql += f' AND NOT {partner_sql}'
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
    p['partner'] = bool(scalar(db, 'SELECT 1 FROM allocations a JOIN invoices i ON i.id=a.invoice_id '
                                   'WHERE a.payment_id=? AND i.partner=1', (p['id'],)))
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
    partner = set()
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
        partner.add(bool(inv['partner']))
        total += amt
    if total <= 0:
        raise ApiError('Enter an amount against at least one invoice')
    if len(partner) > 1:
        raise ApiError('Record partner contributions separately from customer invoice payments')
    partner = True in partner
    ref = text(body.get('reference'))
    cur = db.execute('INSERT INTO payments(kind, contact_id, date, bank_account_id, amount, reference, created_at) '
                     'VALUES (?,?,?,?,?,?,?)', (kind, c['id'], date, bank['id'], total, ref, now()))
    pid = cur.lastrowid
    for iid, amt in clean:
        db.execute('INSERT INTO allocations(payment_id, invoice_id, amount) VALUES (?,?,?)', (pid, iid, amt))
    if kind == 'receipt':
        ctl = system_account(db, 'partner' if partner else 'ar')
        memo = f'{"Contribution" if partner else "Payment"} from {c["name"]}'
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
    j = one(db.execute('SELECT j.*, c.name AS contact_name, r.name AS rule_name FROM journals j '
                       'LEFT JOIN contacts c ON c.id=j.contact_id LEFT JOIN bank_rules r ON r.id=j.rule_id '
                       'WHERE j.id=?', (as_int(jid),)))
    if not j:
        raise ApiError('Transaction not found', 404)
    lines = rows(db.execute('SELECT l.*, a.code AS account_code, a.name AS account_name, a.type AS account_type '
                            'FROM journal_lines l JOIN accounts a ON a.id=l.account_id WHERE journal_id=? ORDER BY l.id',
                            (j['id'],)))
    j['lines'] = lines
    j['bank_text'] = next((l['bank_text'] for l in lines if l['bank_text']), None)
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
    # Keep the statement text an imported line came with, so re-importing the statement still sees it as a duplicate.
    # Matched by bank account, so both sides of a transfer keep their own text.
    btext = {r[0]: (r[1], r[2]) for r in db.execute("SELECT account_id, bank_text, bank_date FROM journal_lines "
                                                    "WHERE journal_id=? AND role='bank' AND bank_text IS NOT NULL", (j['id'],))}
    for l in lines:
        if l.get('role') == 'bank' and l['account_id'] in btext:
            l['bank_text'], l['bank_date'] = btext.pop(l['account_id'])
    if len(btext) == 1:  # the bank account itself changed: keep the text on the new bank line
        bl = [l for l in lines if l.get('role') == 'bank' and not l.get('bank_text')]
        if len(bl) == 1:
            bl[0]['bank_text'], bl[0]['bank_date'] = btext.popitem()[1]
    db.execute('UPDATE journals SET date=?, memo=?, reference=?, contact_id=?, '
               'reviewed=CASE WHEN rule_id IS NULL THEN reviewed ELSE 1 END WHERE id=?',
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
    sql = ('SELECT l.id, l.amount, l.description, l.recon_id, l.bank_text, j.id AS journal_id, j.date, j.memo, j.reference, '
           'j.source, j.source_id, j.rule_id, j.rule_mode, j.reviewed, r.name AS rule_name, c.name AS contact_name, '
           f'{RULE_CATEGORY_SQL} '
           'FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
           'LEFT JOIN contacts c ON c.id=COALESCE(l.contact_id, j.contact_id) LEFT JOIN bank_rules r ON r.id=j.rule_id '
           'WHERE l.account_id=?')
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
    res = {'account': a, 'opening': opening, 'closing': bal, 'lines': out, 'shared': False}
    shared = shared_accounts(db)
    docs = shared_documents(db)
    if shared or docs:
        # The user's share of each line, as the reports see it (see report_lines). Shown only when it differs.
        share = {}
        for jid in {r['journal_id'] for r in out}:
            jl = db.execute('SELECT l.id, l.account_id, a.subtype, l.amount, j.source FROM journal_lines l '
                            'JOIN journals j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id '
                            'WHERE l.journal_id=? ORDER BY l.id', (jid,)).fetchall()
            amts, _ = share_journal([(x[1], x[2], x[3]) for x in jl], jl[0][4], shared, docs.get(jid, SHARE_FULL))
            share.update({x[0]: amt for x, amt in zip(jl, amts)})
        sopen = 0
        if frm:
            sopen = s * sum(amt for aid, _, _, amt in report_lines(db, to=(D(frm) - dt.timedelta(days=1)).isoformat())
                            if aid == a['id'])
        sbal = sopen
        for r in out:
            r['share'] = share[r['id']]
            sbal += s * r['share']
            r['share_balance'] = sbal
        if sopen != opening or any(r['share'] != r['amount'] for r in out):
            res.update(shared=True, share_opening=sopen, share_closing=sbal)
    return res


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
    lines = rows(db.execute('SELECT l.id, l.amount, l.description, l.bank_text, j.id AS journal_id, j.date, j.memo, j.reference, '
                            'j.source, j.source_id, j.rule_id, j.rule_mode, j.reviewed, r.name AS rule_name, '
                            f'{RULE_CATEGORY_SQL}, '
                            'c.name AS contact_name FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
                            'LEFT JOIN contacts c ON c.id=COALESCE(l.contact_id, j.contact_id) '
                            'LEFT JOIN bank_rules r ON r.id=j.rule_id '
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
        db.execute('UPDATE journals SET reviewed=1 WHERE rule_id IS NOT NULL AND id IN '
                   '(SELECT journal_id FROM journal_lines WHERE recon_id=?)', (rid,))
    # Statement text of the CSV lines these items were matched to. A later import of the same statement
    # then recognises them as already in numbr.
    # Each value is the text, or {text, date} with the statement line's date.
    texts = body.get('bank_text') or {}
    if isinstance(texts, dict):
        idset = set(ids)
        for k, v in texts.items():
            lid = as_int(k, 'Line')
            t = text(v.get('text') if isinstance(v, dict) else v)[:500]
            d = as_date(v['date'], 'Statement date') if isinstance(v, dict) and v.get('date') else None
            if lid in idset and t:
                db.execute('UPDATE journal_lines SET bank_text=?, bank_date=? WHERE id=? AND bank_text IS NULL', (t, d, lid))
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
# Bank rules: categorise imported statement lines
# ----------------------------------------------------------------------------

RULE_MAX_TOKENS = 12
DATE_PARTS = (('YYYY', r'\d{4}'), ('MMM', r'[A-Za-z]{3}'), ('YY', r'\d{2}'), ('DD', r'\d{1,2}'), ('MM', r'\d{1,2}'),
              ('D', r'\d{1,2}'), ('M', r'\d{1,2}'))


def norm_text(t):
    return ' '.join((t or '').split()).lower()


def date_regex(fmt):
    f = (fmt or '').strip().upper()
    if not f or len(f) > 20 or not any(c in f for c in 'DMY'):
        raise ApiError('Choose a date format such as DD/MM/YYYY')
    out, i = '', 0
    while i < len(f):
        for tok, rx in DATE_PARTS:
            if f.startswith(tok, i):
                out += rx
                i += len(tok)
                break
        else:
            out += r'\s*' if f[i].isspace() else re.escape(f[i])
            i += 1
    return out


def token_regex(t, i):
    if not isinstance(t, dict):
        raise ApiError(f'Pattern part {i} is not valid')
    kind = t.get('t')
    if kind == 'text':
        v = text(t.get('v'))
        if not v:
            raise ApiError(f'Pattern part {i}: type the text to look for')
        if len(v) > 100:
            raise ApiError(f'Pattern part {i}: the text is too long (100 characters at most)')
        return r'\s+'.join(re.escape(w) for w in v.split())
    if kind in ('number', 'letters'):
        n = t.get('count')
        base = r'\d' if kind == 'number' else '[A-Za-z]'
        if n in (None, '', 0):
            return base + '+'
        n = as_int(n, f'Pattern part {i} count')
        if not 1 <= n <= 30:
            raise ApiError(f'Pattern part {i}: the count must be between 1 and 30')
        return base + '{%d}' % n
    if kind == 'date':
        return date_regex(t.get('fmt'))
    if kind == 'any':
        return '.*?'
    raise ApiError(f'Pattern part {i} is not a known type')


def compile_rule_regex(text_op, tokens):
    """Turn the rule builder's pattern parts into a regex. Parts follow each other, with optional spaces between."""
    if not text_op:
        return None
    if not isinstance(tokens, list) or not tokens:
        raise ApiError('Add at least one part to the bank text pattern')
    if len(tokens) > RULE_MAX_TOKENS:
        raise ApiError(f'A pattern can have at most {RULE_MAX_TOKENS} parts')
    body = r'\s*'.join(token_regex(t, i) for i, t in enumerate(tokens, 1))
    if all(t.get('t') == 'any' for t in tokens):
        raise ApiError('The pattern needs some text, a number, letters or a date, not just "anything"')
    rx = r'^\s*' + body + r'\s*$' if text_op == 'equals' else body
    try:
        return re.compile(rx, re.IGNORECASE)
    except re.error:
        raise ApiError('That pattern could not be used. Try simplifying it.')


def load_rule(r):
    r = dict(r)
    r['text_tokens'] = json.loads(r['text_tokens']) if r.get('text_tokens') else []
    r['account_ids'] = json.loads(r['account_ids']) if r.get('account_ids') else []
    return r


def rule_regex(r):
    return compile_rule_regex(r['text_op'], r['text_tokens'])


def rule_matches(r, rx, bank_id, amount, desc):
    if r['account_ids'] and bank_id not in r['account_ids']:
        return False
    if r.get('target_account_id') == bank_id:  # a transfer rule never applies to the account it transfers to
        return False
    if (r['direction'] == 'out' and amount >= 0) or (r['direction'] == 'in' and amount <= 0):
        return False
    if r['amount_op']:
        size = abs(amount)
        if ((r['amount_op'] == 'eq' and size != r['amount']) or (r['amount_op'] == 'lt' and size >= r['amount'])
                or (r['amount_op'] == 'gt' and size <= r['amount'])):
            return False
    if rx is not None and not rx.search(desc or ''):
        return False
    return True


def rule_payload(db, body):
    name = text(body.get('name'))
    if not name:
        raise ApiError('Give the rule a name')
    mode = body.get('mode')
    if mode not in ('auto', 'recommend'):
        raise ApiError('Choose whether the rule auto-applies or recommends')
    text_op = body.get('text_op') or None
    if text_op not in (None, 'contains', 'equals'):
        raise ApiError('Choose how the bank text should match')
    tokens = (body.get('text_tokens') or []) if text_op else []
    compile_rule_regex(text_op, tokens)
    tokens = [{k: v for k, v in t.items() if k in ('t', 'v', 'count', 'fmt')} for t in tokens]
    direction = body.get('direction') or 'any'
    if direction not in ('any', 'out', 'in'):
        raise ApiError('Choose money in, money out or either')
    amount_op = body.get('amount_op') or None
    if amount_op not in (None, 'eq', 'lt', 'gt'):
        raise ApiError('Choose how the amount should compare')
    amt = None
    if amount_op:
        amt = as_cents(req(body, 'amount', 'Rule amount'), 'Rule amount')
        if amt <= 0:
            raise ApiError('The rule amount must be more than zero')
    if not text_op and not amount_op:
        raise ApiError('A rule needs a bank text pattern, an amount condition, or both')
    ids = []
    for aid in body.get('account_ids') or []:
        b = bank_account(db, aid)
        if b['id'] not in ids:
            ids.append(b['id'])
    target = account(db, as_int(req(body, 'target_account_id', 'Category account'), 'Category account'))
    if target['subtype'] in ('ar', 'ap', 're', 'hist'):
        raise ApiError(f'{target["name"]} can\'t be used by a rule. Choose an income, expense or other account, '
                       'or a bank account to transfer to.')
    if target['subtype'] == 'bank' and target['id'] in ids and len(ids) == 1:
        raise ApiError(f'This rule only applies to {target["name"]}, so it can\'t transfer to it. '
                       'Choose a different account to transfer to.')
    if target['archived']:
        raise ApiError(f'{target["name"]} is archived')
    cid = contact(db, body['contact_id'])['id'] if body.get('contact_id') else None
    return {'name': name, 'mode': mode, 'text_op': text_op, 'text_tokens': json.dumps(tokens) if text_op else None,
            'direction': direction, 'amount_op': amount_op, 'amount': amt, 'account_ids': json.dumps(ids) if ids else None,
            'target_account_id': target['id'], 'contact_id': cid, 'memo': text(body.get('memo')) or None,
            'active': 0 if body.get('active') is False else 1}


def get_rule(db, rid):
    r = one(db.execute('SELECT * FROM bank_rules WHERE id=? AND archived=0', (as_int(rid, 'Rule'),)))
    if not r:
        raise ApiError('Rule not found', 404)
    return load_rule(r)


def regex_text(r):
    try:
        rx = rule_regex(r)
    except ApiError:
        return ''
    return rx.pattern if rx else ''


@route('GET', '/api/rules')
def api_rules(db, q, body):
    out = []
    for r in db.execute('SELECT r.*, a.code AS target_code, a.name AS target_name, c.name AS contact_name, '
                        '(SELECT COUNT(*) FROM journals WHERE rule_id=r.id) AS used FROM bank_rules r '
                        'JOIN accounts a ON a.id=r.target_account_id LEFT JOIN contacts c ON c.id=r.contact_id '
                        'WHERE r.archived=0 ORDER BY r.priority, r.id'):
        r = load_rule(r)
        r['regex'] = regex_text(r)
        out.append(r)
    return out


@route('GET', '/api/rules/:rid')
def api_rule_get(db, q, body, rid):
    # Deleted (archived) rules can still be viewed, so a transaction's rule badge always leads somewhere.
    r = one(db.execute('SELECT * FROM bank_rules WHERE id=?', (as_int(rid, 'Rule'),)))
    if not r:
        raise ApiError('Rule not found', 404)
    r = load_rule(r)
    r['regex'] = regex_text(r)
    r['used'] = scalar(db, 'SELECT COUNT(*) FROM journals WHERE rule_id=?', (r['id'],))
    return r


@route('POST', '/api/rules')
def api_rule_create(db, q, body):
    p = rule_payload(db, body)
    p['priority'] = (scalar(db, 'SELECT MAX(priority) FROM bank_rules WHERE archived=0') or 0) + 1
    p['created_at'] = now()
    cur = db.execute('INSERT INTO bank_rules(%s) VALUES (%s)' % (','.join(p), ','.join('?' * len(p))), list(p.values()))
    audit(db, 'rule_create', 'rule', cur.lastrowid, {'name': p['name']})
    return {'id': cur.lastrowid}


@route('PUT', '/api/rules/:rid')
def api_rule_update(db, q, body, rid):
    r = get_rule(db, rid)
    p = rule_payload(db, body)
    db.execute('UPDATE bank_rules SET %s WHERE id=?' % ','.join(f'{k}=?' for k in p), list(p.values()) + [r['id']])
    audit(db, 'rule_update', 'rule', r['id'], {'name': p['name']})
    return {'id': r['id']}


@route('DELETE', '/api/rules/:rid')
def api_rule_delete(db, q, body, rid):
    r = get_rule(db, rid)
    if scalar(db, 'SELECT 1 FROM journals WHERE rule_id=?', (r['id'],)):
        # Keep it (hidden) so transactions it created still show which rule made them.
        db.execute('UPDATE bank_rules SET archived=1, active=0 WHERE id=?', (r['id'],))
    else:
        db.execute('DELETE FROM bank_rules WHERE id=?', (r['id'],))
    audit(db, 'rule_delete', 'rule', r['id'], {'name': r['name']})
    return {'ok': True}


@route('POST', '/api/rules/reorder')
def api_rules_reorder(db, q, body):
    ids = [as_int(i, 'Rule') for i in body.get('ids') or []]
    for n, rid in enumerate(ids, 1):
        db.execute('UPDATE bank_rules SET priority=? WHERE id=? AND archived=0', (n, rid))
    audit(db, 'rule_reorder', 'rule', None, {'ids': ids})
    return {'ok': True}


@route('POST', '/api/rules/test')
def api_rules_test(db, q, body):
    """Preview a draft rule: the regex it compiles to, whether a sample matches, and past statement lines it would catch."""
    text_op = body.get('text_op') or None
    rx = compile_rule_regex(text_op, (body.get('text_tokens') or []) if text_op else [])
    amount_op = body.get('amount_op') or None
    amt = body.get('amount')
    if amount_op and (not isinstance(amt, int) or isinstance(amt, bool) or amt <= 0):
        amount_op = None
    draft = {'account_ids': [as_int(i) for i in body.get('account_ids') or []], 'direction': body.get('direction') or 'any',
             'amount_op': amount_op, 'amount': amt}
    sample = text(body.get('sample'))
    res = {'regex': rx.pattern if rx else '', 'sample_match': bool(rx.search(sample)) if (rx and sample) else None,
           'recent': []}
    if not rx and not amount_op:
        return res
    for l in db.execute('SELECT l.account_id, l.amount, l.bank_text, j.date, a.name AS account_name FROM journal_lines l '
                        'JOIN journals j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id '
                        'WHERE l.bank_text IS NOT NULL ORDER BY l.id DESC LIMIT 500'):
        if rule_matches(draft, rx, l['account_id'], l['amount'], l['bank_text']):
            res['recent'].append(dict(l))
            if len(res['recent']) >= 20:
                break
    return res


CSV_MAP_COLS = ('date', 'desc', 'amt', 'dr', 'cr')


def csv_mapping_json(m):
    """Validate the column choices from the CSV import screen and return them as JSON to store on the account.
    Each column is kept by position and by header name, so a bank that reorders its columns still maps."""
    if not isinstance(m, dict):
        raise ApiError('The column choices are not valid')
    cols = m.get('cols') if isinstance(m.get('cols'), dict) else {}
    out = {'head': bool(m.get('head')), 'df': 'mdy' if m.get('df') == 'mdy' else 'dmy',
           'mode': 'split' if m.get('mode') == 'split' else 'single', 'flip': bool(m.get('flip')), 'cols': {}}
    for k in CSV_MAP_COLS:
        c = cols.get(k) if isinstance(cols.get(k), dict) else {}
        i = c.get('i')
        out['cols'][k] = {'i': i if isinstance(i, int) and not isinstance(i, bool) and -1 <= i < 500 else -1,
                          'name': text(c.get('name'))[:100]}
    return json.dumps(out)


@route('POST', '/api/reconcile/import')
def api_reconcile_import(db, q, body):
    """Process statement lines from a CSV for one bank account. For each line, in order:
    1. duplicate: same date, amount and statement text as a line already in this account, so it's skipped
    2. rule: the first matching active rule creates a spend/receive transaction (rules win over hand-entered items;
       a similar hand-entered item is reported as a possible double-up)
    3. none: left for the screen to match against existing items, or for the user to enter."""
    a = bank_account(db, req(body, 'account_id', 'Bank account'))
    raw = body.get('lines')
    if not isinstance(raw, list) or not raw:
        raise ApiError('The statement has no lines to import')
    if len(raw) > 5000:
        raise ApiError('That statement is too big. Import 5,000 lines or fewer at a time.')
    items = []
    for i, l in enumerate(raw, 1):
        if not isinstance(l, dict):
            raise ApiError(f'Statement line {i} is not valid')
        amt = as_cents(l.get('amount'), f'Statement line {i} amount')
        if amt == 0:
            raise ApiError(f'Statement line {i} has a zero amount')
        items.append({'date': as_date(l.get('date'), f'Statement line {i} date'), 'amount': amt,
                      'description': text(l.get('description'))[:500]})
    if body.get('mapping') is not None:
        db.execute('UPDATE accounts SET csv_map=? WHERE id=?', (csv_mapping_json(body['mapping']), a['id']))
    seen = {}
    for r in db.execute('SELECT l.id, COALESCE(l.bank_date, j.date) AS date, l.amount, l.bank_text, l.recon_id FROM journal_lines l '
                        'JOIN journals j ON j.id=l.journal_id WHERE l.account_id=? AND l.bank_text IS NOT NULL '
                        'ORDER BY l.id', (a['id'],)):
        seen.setdefault((r['date'], r['amount'], norm_text(r['bank_text'])), []).append(r)
    rules = []
    for r in db.execute('SELECT * FROM bank_rules WHERE active=1 AND archived=0 ORDER BY priority, id'):
        r = load_rule(r)
        rules.append((r, rule_regex(r)))
    manual = rows(db.execute('SELECT l.id, l.amount, j.date FROM journal_lines l JOIN journals j ON j.id=l.journal_id '
                             'WHERE l.account_id=? AND l.recon_id IS NULL AND l.bank_text IS NULL AND j.rule_id IS NULL',
                             (a['id'],)))
    # The other side of a transfer already in numbr (entered by hand, or made by a transfer rule while importing the
    # other account's statement). A transfer rule claims it instead of creating the transfer a second time.
    pending_transfers = rows(db.execute(
        "SELECT l.id, l.amount, j.date, o.account_id AS other_id FROM journal_lines l JOIN journals j ON j.id=l.journal_id "
        "JOIN journal_lines o ON o.journal_id=j.id AND o.id<>l.id AND o.role='bank' "
        "WHERE l.account_id=? AND j.source='transfer' AND l.recon_id IS NULL AND l.bank_text IS NULL ORDER BY j.date, l.id",
        (a['id'],)))
    bank_ids = {r[0] for r in db.execute("SELECT id FROM accounts WHERE subtype='bank'")}
    flagged = set()
    lock = get_setting(db, 'lock_date')
    out, counts = [], {}
    for it in items:
        res = {'status': 'none'}
        k = (it['date'], it['amount'], norm_text(it['description']))
        rule = next((r for r, rx in rules if rule_matches(r, rx, a['id'], it['amount'], it['description'])), None)
        if seen.get(k):
            # Already in numbr. Report which line, so the screen can still tick it if it isn't reconciled yet.
            dup = seen[k].pop(0)
            res = {'status': 'duplicate', 'line_id': dup['id'], 'reconciled': bool(dup['recon_id'])}
        elif rule:
            res = {'rule_id': rule['id'], 'rule_name': rule['name']}
            transfer = rule['target_account_id'] in bank_ids
            other = None
            if transfer:
                near = [p for p in pending_transfers if p['other_id'] == rule['target_account_id']
                        and p['amount'] == it['amount'] and abs((D(p['date']) - D(it['date'])).days) <= 7]
                other = min(near, key=lambda p: abs((D(p['date']) - D(it['date'])).days)) if near else None
            if other:
                # Already in numbr from the other account's side: treat as a duplicate, and keep this statement text
                # on it so the next import of this statement skips it.
                pending_transfers.remove(other)
                flagged.add(other['id'])  # so it isn't also reported as a possible double-up
                db.execute('UPDATE journal_lines SET bank_text=?, bank_date=? WHERE id=?',
                           (it['description'] or None, it['date'], other['id']))
                res = {'status': 'duplicate', 'line_id': other['id'], 'reconciled': False, 'transfer': True}
            elif lock and it['date'] <= lock:
                res.update(status='locked')
            else:
                desc = it['description'] or rule['name']
                memo = rule['memo'] or desc
                lines = [{'account_id': a['id'], 'amount': it['amount'], 'role': 'bank', 'description': memo,
                          'bank_text': it['description'] or None, 'bank_date': it['date']},
                         {'account_id': rule['target_account_id'], 'amount': -it['amount'], 'description': desc,
                          'role': 'bank' if transfer else None}]
                source = 'transfer' if transfer else 'spend' if it['amount'] < 0 else 'receive'
                db.execute('SAVEPOINT rule_line')
                try:
                    jid = post_journal(db, it['date'], source, lines, memo=memo, contact_id=rule['contact_id'])
                    db.execute('UPDATE journals SET rule_id=?, rule_mode=? WHERE id=?', (rule['id'], rule['mode'], jid))
                    db.execute('RELEASE rule_line')
                except ApiError as e:
                    db.execute('ROLLBACK TO rule_line')
                    db.execute('RELEASE rule_line')
                    res.update(status='error', error=str(e))
                else:
                    lid = scalar(db, "SELECT id FROM journal_lines WHERE journal_id=? AND role='bank' AND account_id=?",
                                 (jid, a['id']))
                    res.update(status=rule['mode'], journal_id=jid, line_id=lid)
                    near = [m for m in manual if m['id'] not in flagged and m['amount'] == it['amount']
                            and abs((D(m['date']) - D(it['date'])).days) <= 7]
                    if near:
                        m = min(near, key=lambda m: abs((D(m['date']) - D(it['date'])).days))
                        flagged.add(m['id'])
                        res.update(possible_dup_line_id=m['id'], possible_dup_date=m['date'])
        counts[res['status']] = counts.get(res['status'], 0) + 1
        out.append(res)
    audit(db, 'bank_import', 'account', a['id'], {'lines': len(items), **counts})
    return {'results': out, 'counts': counts}


# ----------------------------------------------------------------------------
# Recurring transactions
# ----------------------------------------------------------------------------

def recurring_payload(db, body):
    name = text(body.get('name'))
    if not name:
        raise ApiError('Give the recurring transaction a name')
    kind = body.get('kind')
    if kind not in ('sale', 'bill', 'spend', 'receive', 'partner'):
        raise ApiError('Choose what kind of transaction this is')
    partner = kind == 'partner'
    if partner:
        kind = 'sale'
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
    if partner:
        lines = [{'account_id': system_account(db, 'partner')['id'], 'description': name, 'amount': partner_amount(body)}]
    else:
        lines = parse_item_lines(db, body.get('lines'))
    if sum(l['amount'] for l in lines) <= 0:
        raise ApiError('The total must be more than zero')
    terms = as_int(body.get('terms_days', 14) or 0, 'Payment terms')
    anchor = as_int(body.get('anchor_day') or D(nd).day)
    if body.get('last_day_of_month'):
        anchor = 31
    share = share_from_body(body) if kind in ('sale', 'bill') and not partner else SHARE_FULL
    return (name, kind, cid, bid, freq, nd, anchor, ed, terms, text(body.get('memo')), json.dumps(lines),
            1 if body.get('active', True) else 0, share, 1 if partner else 0)


@route('GET', '/api/recurring')
def api_recurring(db, q, body):
    out = rows(db.execute('SELECT r.*, c.name AS contact_name, b.name AS bank_name FROM recurring r '
                          'LEFT JOIN contacts c ON c.id=r.contact_id LEFT JOIN accounts b ON b.id=r.bank_account_id '
                          'ORDER BY r.active DESC, r.next_date'))
    for r in out:
        r['lines'] = json.loads(r['lines'])
        r['total'] = sum(l['amount'] for l in r['lines'])
        r['generated_count'] = scalar(db, 'SELECT COUNT(*) FROM journals WHERE recurring_id=?', (r['id'],)) + \
            scalar(db, 'SELECT COUNT(*) FROM invoices WHERE recurring_id=? AND (historical=1 OR partner=1)', (r['id'],))
        if r['partner']:
            r['kind'] = 'partner'
    return out


@route('GET', '/api/recurring/:rid')
def api_recurring_get(db, q, body, rid):
    r = one(db.execute('SELECT * FROM recurring WHERE id=?', (as_int(rid),)))
    if not r:
        raise ApiError('Not found', 404)
    r['lines'] = json.loads(r['lines'])
    if r['partner']:
        r['kind'] = 'partner'
    return r


@route('POST', '/api/recurring')
def api_recurring_create(db, q, body):
    p = recurring_payload(db, body)
    cur = db.execute('INSERT INTO recurring(name, kind, contact_id, bank_account_id, frequency, next_date, anchor_day, end_date, '
                     'terms_days, memo, lines, active, share_bp, partner, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     p + (now(),))
    audit(db, 'recurring_create', 'recurring', cur.lastrowid, body)
    generated = run_recurring(db) if get_setting(db, 'setup_done') == '1' else []
    return {'id': cur.lastrowid, 'generated': generated}


@route('PUT', '/api/recurring/:rid')
def api_recurring_update(db, q, body, rid):
    r = api_recurring_get(db, q, body, rid)
    p = recurring_payload(db, body)
    db.execute('UPDATE recurring SET name=?, kind=?, contact_id=?, bank_account_id=?, frequency=?, next_date=?, anchor_day=?, '
               'end_date=?, terms_days=?, memo=?, lines=?, active=?, share_bp=?, partner=? WHERE id=?', p + (r['id'],))
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
    if t['partner']:
        due = (D(date) + dt.timedelta(days=t['terms_days'])).isoformat()
        iid = create_invoice(db, {'contact_id': t['contact_id'], 'date': date, 'due_date': due, 'memo': t['memo'],
                                  'description': lines[0]['description'] if lines else t['name'],
                                  'amount': sum(l['amount'] for l in lines)}, 'partner', recurring_id=t['id'])
        return {'type': 'partner', 'id': iid}
    if t['kind'] in ('sale', 'bill'):
        due = (D(date) + dt.timedelta(days=t['terms_days'])).isoformat()
        iid = create_invoice(db, {'contact_id': t['contact_id'], 'date': date, 'due_date': due, 'memo': t['memo'],
                                  'lines': lines, 'share_pct': t['share_bp'] / 100}, t['kind'], recurring_id=t['id'])
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

# Shared bank accounts and shared invoices/bills
# A bank account can be shared with other people (e.g. a joint account). Its share_bp says how much of it
# belongs to the user. The ledger always stores the full amounts (so the account reconciles to the real
# statement); reports show only the user's share:
#   * lines in a shared bank account are cut to that account's share;
#   * in a journal whose bank lines are all in one shared account (a spend, receive or journal from it),
#     the other lines are cut by the same share, except AR/AP, which stay full so aged reports tie;
#   * everything else (transfers to/from unshared accounts, opening balances) stays full.
# An invoice or bill can also be shared (e.g. rent split with someone else). Its share_bp cuts every line
# of its journal except AR/AP, which stay full so the aged reports still tie.
# Whatever no longer balances is the other owners' part and goes to the PARTNER pseudo account, shown
# as a computed equity line, so every report still balances.
# Lines in the 'partner' system account (partner contributions) are wholly the other owners' money: they
# are never cut and are reported as part of the PARTNER line.

def shared_accounts(db):
    return {r[0]: r[1] for r in db.execute("SELECT id, share_bp FROM accounts WHERE subtype='bank' AND share_bp<?",
                                           (SHARE_FULL,))}


def cut(amount, bp):
    """amount * bp / 10000 in whole cents, rounding half away from zero, without floats."""
    q, r = divmod(abs(amount) * bp, SHARE_FULL)
    v = q + (1 if 2 * r >= SHARE_FULL else 0)
    return v if amount >= 0 else -v


def shared_documents(db):
    """{journal_id: share_bp} for posted invoices and bills the user only partly owns."""
    return {r[0]: r[1] for r in db.execute('SELECT journal_id, share_bp FROM invoices WHERE journal_id IS NOT NULL '
                                           'AND share_bp<?', (SHARE_FULL,))}


def share_journal(lines, source, shared, doc_bp=SHARE_FULL):
    """lines: [(account_id, subtype, amount)]. doc_bp is the share of a shared invoice/bill.
    Returns (user's share of each line, PARTNER amount)."""
    if doc_bp >= SHARE_FULL and not any(l[0] in shared for l in lines):
        return [l[2] for l in lines], 0
    banks = {shared.get(aid, SHARE_FULL) for aid, sub, _ in lines if sub == 'bank'}
    if doc_bp < SHARE_FULL:
        rest = doc_bp
    else:
        rest = banks.pop() if len(banks) == 1 and source != 'opening' else SHARE_FULL
    out = []
    for aid, sub, amt in lines:
        if aid in shared:
            out.append(cut(amt, shared[aid]))
        elif sub in ('bank', 'ar', 'ap', 'partner'):
            out.append(amt)
        else:
            out.append(cut(amt, rest))
    return out, -sum(out)


def report_lines(db, to=None, frm=None):
    """Yield (account_id, type, date, amount) for frm <= date <= to, with shared accounts and shared
    invoices/bills cut to the user's share.
    Rows with account_id PARTNER (type 'equity') carry the other owners' part so each journal still balances."""
    shared = shared_accounts(db)
    docs = shared_documents(db)
    sql = ('SELECT l.journal_id, j.date, j.source, l.account_id, a.type, a.subtype, l.amount FROM journal_lines l '
           'JOIN journals j ON j.id=l.journal_id JOIN accounts a ON a.id=l.account_id WHERE 1=1')
    args = []
    if to:
        sql += ' AND j.date<=?'
        args.append(to)
    if frm:
        sql += ' AND j.date>=?'
        args.append(frm)
    if not shared and not docs:
        for r in db.execute(sql, args):
            if r[5] == 'partner':
                yield PARTNER, 'equity', r[1], r[6]
            else:
                yield r[3], r[4], r[1], r[6]
        return
    sql += ' ORDER BY l.journal_id, l.id'
    for _, grp in itertools.groupby(db.execute(sql, args), key=lambda r: r[0]):
        grp = list(grp)
        amts, partner = share_journal([(r[3], r[5], r[6]) for r in grp], grp[0][2], shared,
                                      docs.get(grp[0][0], SHARE_FULL))
        for r, amt in zip(grp, amts):
            if r[5] == 'partner':
                partner += amt
            else:
                yield r[3], r[4], r[1], amt
        if partner:
            yield PARTNER, 'equity', grp[0][1], partner


def balances_as_at(db, date):
    out = {}
    for aid, _, _, amt in report_lines(db, to=date):
        out[aid] = out.get(aid, 0) + amt
    return out


def profit_between(db, frm, to):
    """Net profit (natural sign) for frm <= date <= to. frm may be None."""
    return -sum(amt for _, typ, _, amt in report_lines(db, to=to, frm=frm) if typ in PL_TYPES)


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
    accts = {a['id']: a for a in rows(db.execute("SELECT * FROM accounts WHERE type IN ('income','cos','expense') ORDER BY name COLLATE NOCASE"))}
    data = {}
    for aid, typ, date, amt in report_lines(db, to=end, frm=start):
        if typ in PL_TYPES:
            data.setdefault(aid, [0] * len(months))[idx[date[:7]]] += SIGN[typ] * amt
    n = len(months)
    cover = budget_cover(db, months)
    bud = None
    if q.get('budget') == '1':
        if not cover['available']:
            raise ApiError(f'No budget covers {month_span(cover["missing"])}. Build one under Budgets, '
                           'or choose months that have a budget.')
        # Budgets are planning figures in natural sign: read straight from budget_amounts, never the ledger.
        bud = {}
        marks = ','.join('?' * n)
        for aid, m, amt in db.execute(f'SELECT account_id, month, amount FROM budget_amounts WHERE month IN ({marks})', months):
            if aid in accts:
                bud.setdefault(aid, [0] * n)[idx[m]] += amt

    def section(typ, title):
        rs = []
        for aid, a in accts.items():
            if a['type'] != typ:
                continue
            vals = data.get(aid) or [0] * n
            bvals = (bud or {}).get(aid) or [0] * n
            if not any(vals) and not any(bvals):
                continue
            r = {'account_id': aid, 'code': a['code'], 'name': a['name'], 'values': vals, 'total': sum(vals),
                 'group_id': a['group_id']}
            if bud is not None:
                r['budget'], r['budget_total'] = bvals, sum(bvals)
            rs.append(r)
        tot = [sum(r['values'][i] for r in rs) for i in range(n)]
        s = {'type': typ, 'title': title, 'rows': rs, 'totals': tot, 'total': sum(tot)}
        if bud is not None:
            bt = [sum(r['budget'][i] for r in rs) for i in range(n)]
            s['budget_totals'], s['budget_total'] = bt, sum(bt)
        return s

    inc, cos, exp = section('income', 'Income'), section('cos', 'Cost of Sales'), section('expense', 'Expenses')
    gp = [inc['totals'][i] - cos['totals'][i] for i in range(n)]
    np_ = [gp[i] - exp['totals'][i] for i in range(n)]
    out = {'months': months, 'from': start, 'to': end, 'sections': [inc, cos, exp],
           'gross_profit': gp, 'gross_profit_total': sum(gp), 'net_profit': np_, 'net_profit_total': sum(np_),
           'business_name': get_setting(db, 'business_name'), 'budget_cover': cover}
    if bud is not None:
        bgp = [inc['budget_totals'][i] - cos['budget_totals'][i] for i in range(n)]
        bnp = [bgp[i] - exp['budget_totals'][i] for i in range(n)]
        out.update({'gross_profit_budget': bgp, 'gross_profit_budget_total': sum(bgp),
                    'net_profit_budget': bnp, 'net_profit_budget_total': sum(bnp)})
    return out


def budget_cover(db, months):
    """Which budgets cover these months, and which months have none. Budget vs actual needs every month covered."""
    want, used, covered = set(months), [], set()
    for b in rows(db.execute('SELECT id, name, fy_start FROM budgets ORDER BY fy_start')):
        hit = want & set(budget_months(b['fy_start']))
        if hit:
            covered |= hit
            used.append({'id': b['id'], 'name': b['name'], 'label': fy_label(b['fy_start'])})
    missing = [m for m in months if m not in covered]
    return {'available': not missing, 'missing': missing, 'budgets': used}


def month_span(months):
    """'Jul 2027' or 'Jul 2027 to Sep 2027' for error messages (assumes a run of months)."""
    lbl = lambda m: month_start(m).strftime('%b %Y')
    return lbl(months[0]) if len(months) == 1 else f'{lbl(months[0])} to {lbl(months[-1])}'


@route('GET', '/api/reports/bs')
def api_report_bs(db, q, body):
    end_m = q.get('to') or today()[:7]
    if q.get('from'):
        # One month-end column for each month in the range, like the P&L.
        months = months_between(q['from'], end_m)
    else:
        count = max(1, min(as_int(q.get('count') or 1, 'Columns'), 13))
        last = month_start(end_m)
        months = [add_months(last, -(count - 1 - i), 1).strftime('%Y-%m') for i in range(count)]
    dates = [month_end(m).isoformat() for m in months]
    accts = rows(db.execute("SELECT * FROM accounts WHERE type IN ('asset','liability','equity') ORDER BY name COLLATE NOCASE"))
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
                           'subtype': a['subtype'], 'group_id': a['group_id']})
        if typ == 'equity':
            partner = [-(c.get(PARTNER) or 0) for c in cols]
            if any(partner):
                rs.append({'account_id': None, 'code': '', 'name': PARTNER_NAME, 'values': partner, 'partner': True})
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
                           "WHERE i.kind=? AND i.partner=0 AND i.status='open' AND i.date<=? "
                           "ORDER BY c.name COLLATE NOCASE, i.due_date",
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
    hist_open = scalar(db, "SELECT COALESCE(SUM(total),0) FROM invoices WHERE kind=? AND partner=0 AND historical=1 "
                           "AND status='open'", (kind,))
    hist_paid = scalar(db, "SELECT COALESCE(SUM(a.amount),0) FROM allocations a JOIN invoices i ON i.id=a.invoice_id "
                           "JOIN payments p ON p.id=a.payment_id WHERE i.kind=? AND i.partner=0 AND i.historical=1 "
                           "AND p.date<=?", (kind, as_at))
    return {'kind': kind, 'as_at': as_at, 'groups': list(groups.values()), 'totals': totals,
            'control_balance': ctl_bal, 'control_name': ctl['name'],
            'historical_unposted': hist_open - hist_paid, 'business_name': get_setting(db, 'business_name')}


@route('GET', '/api/reports/tb')
def api_report_tb(db, q, body):
    as_at = as_date(q.get('as_at') or today(), 'As at date')
    bal = balances_as_at(db, as_at)
    out = []
    for a in rows(db.execute('SELECT * FROM accounts ORDER BY ' + TYPE_ORDER_SQL + ', name COLLATE NOCASE')):
        v = bal.get(a['id']) or 0
        if v:
            out.append({'account_id': a['id'], 'code': a['code'], 'name': a['name'], 'type': a['type'],
                        'debit': v if v > 0 else 0, 'credit': -v if v < 0 else 0})
    v = bal.get(PARTNER) or 0
    if v:
        out.append({'account_id': None, 'code': '', 'name': PARTNER_NAME, 'type': 'equity',
                    'debit': v if v > 0 else 0, 'credit': -v if v < 0 else 0})
    return {'as_at': as_at, 'rows': out, 'debit': sum(r['debit'] for r in out), 'credit': sum(r['credit'] for r in out),
            'business_name': get_setting(db, 'business_name')}


# ----------------------------------------------------------------------------
# Budgets
# ----------------------------------------------------------------------------
# A budget covers one financial year of P&L accounts. The builder asks, per account, how to make it
# (average or last month of recent actuals, a schedule of amounts, or each month typed in). Those
# choices go in budget_methods, only to set up next year's builder. The built budget is plain monthly
# figures in budget_amounts, changed by hand from then on and never recalculated from the choices.

BUDGET_METHODS = ('average', 'last', 'schedule', 'manual', 'none')
BUDGET_METHOD_LABEL = {'average': 'Monthly average', 'last': 'Last month', 'schedule': 'Own amounts',
                       'manual': 'Typed each month', 'none': 'Not budgeted'}
STEP_DAYS = {'weekly': 7, 'fortnightly': 14}


def ym_add(ym, n):
    return add_months(month_start(ym), n, 1).strftime('%Y-%m')


def ym_diff(a, b):
    """Months from a to b."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def as_ym(v, label='Month'):
    s = text(v)
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', s):
        raise ApiError(f'{label} is not a valid month')
    return s


def budget_fy(db, v):
    fy = as_ym(v, 'Financial year')
    if int(fy[5:7]) != as_int(get_setting(db, 'fy_start_month', '7')):
        raise ApiError('That month is not the start of a financial year')
    return fy


def budget_months(fy):
    return [ym_add(fy, i) for i in range(12)]


def fy_label(fy):
    end = ym_add(fy, 11)
    return f'FY {fy[:4]}' if fy[:4] == end[:4] else f'FY {fy[:4]}-{end[2:4]}'


def round_div(a, n):
    """a / n in whole cents, rounding half away from zero, without floats."""
    q, r = divmod(abs(a), n)
    v = q + (1 if 2 * r >= n else 0)
    return v if a >= 0 else -v


def history_window(db):
    """(from, to) months of actuals the builder uses: the last 12 complete months, starting no earlier
    than the books do. None when there is no complete month yet."""
    to = ym_add(today()[:7], -1)
    frm = ym_add(to, -11)
    conv = get_setting(db, 'conversion_date')
    first = conv[:7] if conv else scalar(db, "SELECT MIN(date) FROM journals WHERE source<>'opening'")
    if first:
        frm = max(frm, first[:7])
    else:
        return None
    return (frm, to) if frm <= to else None


def actuals_by_month(db, frm, to):
    """{account_id: {month: natural cents}} for P&L accounts, months frm..to, cut to the user's share."""
    out = {}
    for aid, typ, date, amt in report_lines(db, to=month_end(to).isoformat(), frm=month_start(frm).isoformat()):
        if typ in PL_TYPES:
            m = out.setdefault(aid, {})
            m[date[:7]] = m.get(date[:7], 0) + SIGN[typ] * amt
    return out


def history_figures(hist, window):
    """(average per month, last month's value, that month) for one account's actuals."""
    if not window:
        return 0, 0, None
    months = months_between(*window)
    avg = round_div(sum(hist.get(m, 0) for m in months), len(months))
    for m in reversed(months):
        if hist.get(m):
            return avg, hist[m], m
    return avg, 0, None


def budget_config(method, raw, months):
    """Check and tidy the builder's settings for one account. Returns the config to store (or None)."""
    if method not in BUDGET_METHODS:
        raise ApiError('Choose how to budget this account')
    if method == 'manual':
        amts = (raw or {}).get('amounts') if isinstance(raw, dict) else None
        if not isinstance(amts, list) or len(amts) != 12:
            raise ApiError('Enter an amount for each of the 12 months')
        return {'amounts': [as_cents(a or 0) for a in amts]}
    if method != 'schedule':
        return None
    if not isinstance(raw, list) or not raw:
        raise ApiError('Add at least one amount')
    if len(raw) > 50:
        raise ApiError('That is too many amounts for one account')
    fy_first, fy_last = months[0], months[-1]
    out = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise ApiError('Amount details are not valid')
        freq = s.get('freq')
        amount = as_cents(s.get('amount') or 0, f'Amount {i}')
        if freq in ('monthly', 'yearly'):
            frm = as_ym(s.get('from') or fy_first, f'Amount {i} "from" month')
            to = as_ym(s.get('to') or fy_last, f'Amount {i} "to" month')
            if to < frm:
                raise ApiError(f'Amount {i}: the "to" month is before the "from" month')
            if frm > fy_last or to < fy_first:
                raise ApiError(f'Amount {i}: those months are outside this financial year')
            out.append({'freq': freq, 'amount': amount, 'from': frm, 'to': to})
        elif freq in STEP_DAYS:
            if not s.get('first'):
                raise ApiError(f'Amount {i}: enter the first date it happens')
            first = as_date(s.get('first'), f'Amount {i} first date')
            if first > month_end(fy_last).isoformat():
                raise ApiError(f'Amount {i}: the first date is after the end of this financial year')
            seg = {'freq': freq, 'amount': amount, 'first': first}
            if s.get('to'):
                seg['to'] = as_date(s['to'], f'Amount {i} end date')
                if seg['to'] < first:
                    raise ApiError(f'Amount {i}: the end date is before the first date')
            out.append(seg)
        else:
            raise ApiError(f'Amount {i}: choose how often it happens')
    return out


def schedule_amounts(segs, months):
    idx = {m: i for i, m in enumerate(months)}
    vals = [0] * 12
    start, end = month_start(months[0]), month_end(months[-1])
    for s in segs:
        if s['freq'] == 'monthly':
            for m in months:
                if s['from'] <= m <= s['to']:
                    vals[idx[m]] += s['amount']
            continue
        if s['freq'] == 'yearly':
            # A total spread evenly over its months; leftover cents go to the first months so it adds up exactly.
            span = [m for m in months if s['from'] <= m <= s['to']]
            q, r = divmod(abs(s['amount']), len(span))
            sign = -1 if s['amount'] < 0 else 1
            for k, m in enumerate(span):
                vals[idx[m]] += sign * (q + (1 if k < r else 0))
            continue
        step = STEP_DAYS[s['freq']]
        d, stop = D(s['first']), min(end, D(s['to'])) if s.get('to') else end
        if d < start:
            d += dt.timedelta(days=-(-(start - d).days // step) * step)
        while d <= stop:
            vals[idx[d.strftime('%Y-%m')]] += s['amount']
            d += dt.timedelta(days=step)
    return vals


def method_amounts(method, config, months, figures):
    avg, last, _ = figures
    if method == 'average':
        return [avg] * 12
    if method == 'last':
        return [last] * 12
    if method == 'manual':
        return list(config['amounts'])
    if method == 'schedule':
        return schedule_amounts(config, months)
    return [0] * 12


def roll_config(method, config, shift):
    """A previous budget's choice moved forward `shift` months to suit a later year."""
    if method == 'manual':
        return config
    if method != 'schedule':
        return None
    out = []
    for s in config:
        s = dict(s)
        if s['freq'] in ('monthly', 'yearly'):
            s['from'], s['to'] = ym_add(s['from'], shift), ym_add(s['to'], shift)
        else:
            # Keep the same weekday rhythm: the first date in the original series on or after the same
            # date `shift` months later.
            step, first = STEP_DAYS[s['freq']], D(s['first'])
            target = add_months(first, shift, first.day)
            s['first'] = (first + dt.timedelta(days=-(-(target - first).days // step) * step)).isoformat()
            if s.get('to'):
                to = D(s['to'])
                s['to'] = add_months(to, shift, to.day).isoformat()
        out.append(s)
    return out


def budget_accounts(db, include_archived=False):
    sql = "SELECT * FROM accounts WHERE type IN ('income','cos','expense')"
    if not include_archived:
        sql += ' AND archived=0'
    return rows(db.execute(sql + ' ORDER BY ' + TYPE_ORDER_SQL + ', name COLLATE NOCASE'))


def get_budget(db, bid):
    b = one(db.execute('SELECT * FROM budgets WHERE id=?', (as_int(bid),)))
    if not b:
        raise ApiError('Budget not found', 404)
    return b


def budget_rows(db, b):
    """The built budget laid out like the P&L: sections of account rows with 12 monthly values."""
    months = budget_months(b['fy_start'])
    idx = {m: i for i, m in enumerate(months)}
    vals = {}
    for r in db.execute('SELECT account_id, month, amount FROM budget_amounts WHERE budget_id=?', (b['id'],)):
        if r[1] in idx:
            vals.setdefault(r[0], [0] * 12)[idx[r[1]]] = r[2]
    methods = {r[0]: r[1] for r in db.execute('SELECT account_id, method FROM budget_methods WHERE budget_id=?', (b['id'],))}
    show = set(vals) | {aid for aid, m in methods.items() if m != 'none'}
    sections = []
    for typ, title in (('income', 'Income'), ('cos', 'Cost of Sales'), ('expense', 'Expenses')):
        rs = []
        for a in rows(db.execute('SELECT * FROM accounts WHERE type=? ORDER BY name COLLATE NOCASE', (typ,))):
            if a['id'] in show:
                v = vals.get(a['id'], [0] * 12)
                rs.append({'account_id': a['id'], 'name': a['name'], 'group_id': a['group_id'], 'values': v,
                           'total': sum(v), 'method': methods.get(a['id']), 'archived': a['archived']})
        tot = [sum(r['values'][i] for r in rs) for i in range(12)]
        sections.append({'type': typ, 'title': title, 'rows': rs, 'totals': tot, 'total': sum(tot)})
    inc, cos, exp = (s['totals'] for s in sections)
    gp = [inc[i] - cos[i] for i in range(12)]
    np_ = [gp[i] - exp[i] for i in range(12)]
    return {**b, 'label': fy_label(b['fy_start']), 'months': months, 'sections': sections,
            'gross_profit': gp, 'gross_profit_total': sum(gp), 'net_profit': np_, 'net_profit_total': sum(np_)}


def compute_budget(db, body, months, window, strict):
    """Work out 12 monthly amounts for each account the builder sent.
    strict: raise on the first bad account (saving); otherwise report errors per account (preview)."""
    items = body.get('accounts')
    if not isinstance(items, list):
        raise ApiError('No accounts were sent')
    accts = {a['id']: a for a in budget_accounts(db, include_archived=True)}
    hist = actuals_by_month(db, *window) if window else {}
    out, seen = [], set()
    for it in items:
        aid = as_int(it.get('account_id'), 'Account')
        a = accts.get(aid)
        if not a:
            raise ApiError('Budgets can only include income, cost of sales and expense accounts')
        if aid in seen:
            raise ApiError(f'{a["name"]} is in the budget twice')
        seen.add(aid)
        method = it.get('method') or 'none'
        try:
            config = budget_config(method, it.get('config'), months)
            vals = method_amounts(method, config, months, history_figures(hist.get(aid, {}), window))
            out.append({'account_id': aid, 'type': a['type'], 'method': method, 'config': config,
                        'values': vals, 'total': sum(vals)})
        except ApiError as e:
            if strict:
                raise ApiError(f'{a["name"]}: {e}')
            out.append({'account_id': aid, 'type': a['type'], 'method': method, 'error': str(e),
                        'values': [0] * 12, 'total': 0})
    return out


def budget_totals(items):
    tot = {t: [0] * 12 for t in PL_TYPES}
    for it in items:
        for i, v in enumerate(it['values']):
            tot[it['type']][i] += v
    np_ = [tot['income'][i] - tot['cos'][i] - tot['expense'][i] for i in range(12)]
    return {'income': tot['income'], 'cos': tot['cos'], 'expense': tot['expense'], 'net_profit': np_}


@route('GET', '/api/budgets')
def api_budgets(db, q, body):
    out = []
    for b in rows(db.execute('SELECT * FROM budgets ORDER BY fy_start DESC')):
        t = {typ: 0 for typ in PL_TYPES}
        for r in db.execute('SELECT a.type, SUM(b.amount) FROM budget_amounts b JOIN accounts a ON a.id=b.account_id '
                            'WHERE b.budget_id=? GROUP BY a.type', (b['id'],)):
            t[r[0]] = r[1]
        out.append({**b, 'label': fy_label(b['fy_start']), 'income': t['income'],
                    'expenses': t['cos'] + t['expense'], 'net_profit': t['income'] - t['cos'] - t['expense']})
    return out


@route('GET', '/api/budgets/builder')
def api_budget_builder(db, q, body):
    cur = fy_start(db, today())[:7]
    years = []
    for n in (-1, 0, 1, 2):
        fy = ym_add(cur, 12 * n)
        years.append({'fy': fy, 'label': fy_label(fy),
                      'budget_id': scalar(db, 'SELECT id FROM budgets WHERE fy_start=?', (fy,))})
    if q.get('fy'):
        fy = budget_fy(db, q['fy'])
    else:
        fy = next((y['fy'] for y in years[1:] if not y['budget_id']), ym_add(cur, 36))
    if scalar(db, 'SELECT 1 FROM budgets WHERE fy_start=?', (fy,)):
        raise ApiError(f'There is already a budget for {fy_label(fy)}. Open it from Budgets, '
                       'or delete it there to start again.')
    months = budget_months(fy)
    window = history_window(db)
    hist = actuals_by_month(db, *window) if window else {}
    prior = one(db.execute('SELECT * FROM budgets WHERE fy_start<? ORDER BY fy_start DESC LIMIT 1', (fy,)))
    choices = {}
    if prior:
        shift = ym_diff(prior['fy_start'], fy)
        for r in db.execute('SELECT account_id, method, config FROM budget_methods WHERE budget_id=?', (prior['id'],)):
            cfg = json.loads(r[2]) if r[2] else None
            choices[r[0]] = (r[1], roll_config(r[1], cfg, shift))
    accts = []
    for a in budget_accounts(db):
        avg, last, last_m = history_figures(hist.get(a['id'], {}), window)
        has = any(hist.get(a['id'], {}).values())
        method, config = choices.get(a['id'], ('average' if has else 'none', None))
        accts.append({'account_id': a['id'], 'name': a['name'], 'type': a['type'], 'group_id': a['group_id'],
                      'average': avg, 'last': last, 'last_month': last_m, 'has_history': has,
                      'method': method, 'config': config, 'from_prior': a['id'] in choices})
    return {'fy': fy, 'label': fy_label(fy), 'name': f'Budget {fy_label(fy)[3:]}', 'months': months, 'years': years,
            'history': {'from': window[0], 'to': window[1], 'count': len(months_between(*window))} if window else None,
            'prior': {'id': prior['id'], 'name': prior['name'], 'label': fy_label(prior['fy_start'])} if prior else None,
            'accounts': accts}


@route('POST', '/api/budgets/preview')
def api_budget_preview(db, q, body):
    months = budget_months(budget_fy(db, req(body, 'fy', 'Financial year')))
    items = compute_budget(db, body, months, history_window(db), strict=False)
    return {'months': months, 'accounts': items, 'totals': budget_totals(items)}


@route('POST', '/api/budgets')
def api_budget_create(db, q, body):
    fy = budget_fy(db, req(body, 'fy', 'Financial year'))
    if scalar(db, 'SELECT 1 FROM budgets WHERE fy_start=?', (fy,)):
        raise ApiError(f'There is already a budget for {fy_label(fy)}')
    months = budget_months(fy)
    window = history_window(db)
    items = compute_budget(db, body, months, window, strict=True)
    name = text(body.get('name')) or f'Budget {fy_label(fy)[3:]}'
    bid = db.execute('INSERT INTO budgets(fy_start, name, history_from, history_to, created_at) VALUES (?,?,?,?,?)',
                     (fy, name, window[0] if window else None, window[1] if window else None, now())).lastrowid
    for it in items:
        db.execute('INSERT INTO budget_methods(budget_id, account_id, method, config) VALUES (?,?,?,?)',
                   (bid, it['account_id'], it['method'], json.dumps(it['config']) if it['config'] is not None else None))
        db.executemany('INSERT INTO budget_amounts(budget_id, account_id, month, amount) VALUES (?,?,?,?)',
                       [(bid, it['account_id'], m, v) for m, v in zip(months, it['values']) if v])
    audit(db, 'budget_create', 'budget', bid, {'fy_start': fy, 'name': name,
                                                'methods': {it['account_id']: it['method'] for it in items}})
    return {'id': bid}


@route('GET', '/api/budgets/:bid')
def api_budget_get(db, q, body, bid):
    return budget_rows(db, get_budget(db, bid))


@route('PUT', '/api/budgets/:bid')
def api_budget_update(db, q, body, bid):
    """Hand edits to a built budget. Never touches budget_methods (next year's starting choices)."""
    b = get_budget(db, bid)
    months = set(budget_months(b['fy_start']))
    if 'name' in body:
        name = text(body.get('name'))
        if not name:
            raise ApiError('Give the budget a name')
        db.execute('UPDATE budgets SET name=? WHERE id=?', (name, b['id']))
    accts = {a['id'] for a in budget_accounts(db, include_archived=True)}
    changes = body.get('amounts') or []
    if not isinstance(changes, list):
        raise ApiError('Budget amounts are not valid')
    for c in changes:
        aid = as_int(c.get('account_id'), 'Account')
        if aid not in accts:
            raise ApiError('Budgets can only include income, cost of sales and expense accounts')
        m = text(c.get('month'))
        if m not in months:
            raise ApiError('That month is not in this budget')
        amt = as_cents(c.get('amount') or 0)
        if amt:
            db.execute('INSERT INTO budget_amounts(budget_id, account_id, month, amount) VALUES (?,?,?,?) '
                       'ON CONFLICT(budget_id, account_id, month) DO UPDATE SET amount=excluded.amount',
                       (b['id'], aid, m, amt))
        else:
            db.execute('DELETE FROM budget_amounts WHERE budget_id=? AND account_id=? AND month=?', (b['id'], aid, m))
    audit(db, 'budget_update', 'budget', b['id'], {'name': body.get('name'), 'changes': len(changes)})
    return budget_rows(db, get_budget(db, b['id']))


@route('DELETE', '/api/budgets/:bid')
def api_budget_delete(db, q, body, bid):
    b = get_budget(db, bid)
    db.execute('DELETE FROM budget_amounts WHERE budget_id=?', (b['id'],))
    db.execute('DELETE FROM budget_methods WHERE budget_id=?', (b['id'],))
    db.execute('DELETE FROM budgets WHERE id=?', (b['id'],))
    audit(db, 'budget_delete', 'budget', b['id'], b)
    return {'ok': True}


@route('GET', '/api/dashboard')
def api_dashboard(db, q, body):
    t = today()
    banks = []
    for a in rows(db.execute("SELECT * FROM accounts WHERE subtype='bank' AND archived=0 ORDER BY name COLLATE NOCASE")):
        s = SIGN[a['type']]
        bal = s * scalar(db, 'SELECT COALESCE(SUM(amount),0) FROM journal_lines WHERE account_id=?', (a['id'],))
        last = one(db.execute('SELECT * FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, id DESC LIMIT 1',
                              (a['id'],)))
        unc = scalar(db, 'SELECT COUNT(*) FROM journal_lines WHERE account_id=? AND recon_id IS NULL', (a['id'],))
        banks.append({'id': a['id'], 'name': a['name'], 'type': a['type'], 'balance': bal,
                      'share_bp': a['share_bp'], 'my_balance': cut(bal, a['share_bp']),
                      'last_reconciled': last['statement_date'] if last else None,
                      'last_reconciled_balance': last['statement_balance'] if last else None, 'uncleared': unc})

    def ar_ap(kind, partner=0):
        tot = over = cnt = 0
        for r in db.execute("SELECT i.total - COALESCE((SELECT SUM(amount) FROM allocations a WHERE a.invoice_id=i.id),0), "
                            "i.due_date FROM invoices i WHERE i.kind=? AND i.partner=? AND i.status='open'", (kind, partner)):
            if r[0] > 0:
                tot += r[0]
                cnt += 1
                if r[1] < t:
                    over += r[0]
        return {'total': tot, 'overdue': over, 'count': cnt}

    this_m = t[:7]
    last_m = add_months(month_start(this_m), -1, 1).strftime('%Y-%m')

    def month_pl(ym):
        inc = exp = 0
        for _, typ, _, amt in report_lines(db, to=month_end(ym).isoformat(), frm=month_start(ym).isoformat()):
            if typ == 'income':
                inc -= amt
            elif typ in ('cos', 'expense'):
                exp += amt
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
    return {'banks': banks, 'receivable': ar_ap('sale'), 'payable': ar_ap('bill'), 'contributions': ar_ap('sale', 1),
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
    server_version = 'numbr/1.0'

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
        if method == 'POST' and u.path == '/api/shutdown':
            # Used by a newer copy of numbr starting up, so it can replace this (outdated) server.
            self.send_json({'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
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


class Server(socketserver.ThreadingMixIn, HTTPServer):
    """HTTPServer that won't share its port. On Windows, SO_REUSEADDR (HTTPServer's default) lets a second
    copy of numbr bind the same port while requests still reach the first, so use an exclusive bind there."""
    if os.name == 'nt':
        allow_reuse_address = False

        def server_bind(self):
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    # One thread per connection: a browser can hold an idle connection open (e.g. when a page is
    # refreshed), which would otherwise stall every other request. SQLite's BEGIN IMMEDIATE in
    # handle_api still runs requests' transactions one at a time.
    daemon_threads = True


def running_server(method, path):
    """Call the numbr server already on our port. Returns its JSON reply, or None if it didn't answer."""
    import urllib.request
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', method=method,
                                 headers={'X-Ledger': '1', 'Content-Type': 'application/json'},
                                 data=b'{}' if method == 'POST' else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def bind_server():
    """Start listening. If an outdated copy of numbr already holds the port, stop it and take over."""
    import time
    url = f'http://localhost:{PORT}/'
    try:
        return Server((HOST, PORT), Handler)
    except OSError:
        pass
    st = running_server('GET', '/api/state')
    if st is not None and st.get('server_current') is True:
        print(f'numbr is already running. Opening {url}')
        webbrowser.open(url)
        return None
    if st is not None and running_server('POST', '/api/shutdown'):
        for _ in range(50):
            time.sleep(0.1)
            try:
                srv = Server((HOST, PORT), Handler)
                print('Replaced an older copy of numbr that was still running.')
                return srv
            except OSError:
                pass
    print()
    print('  An older copy of numbr is still running, so this update can\'t start.')
    print('  Close the other numbr window (or restart the computer), then double-click start.bat again.')
    sys.exit(1)


def main():
    mimetypes.add_type('application/javascript', '.js')
    mimetypes.add_type('text/css', '.css')
    mimetypes.add_type('image/svg+xml', '.svg')
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
    srv = bind_server()
    if srv is None:
        return
    print('=' * 60)
    print(f'  numbr is running at {url}')
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
