'use strict';
/* Ledger front end - plain JavaScript, no build step. */

const S = { settings: {}, accounts: [], contacts: [], today: '' };
const main = document.getElementById('main');
let PREFILL = null;          // data handed from one screen to a form
let RETURN_TO = null;        // where to go after saving a prefilled form

// ---------------------------------------------------------------- utilities
async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: { 'X-Ledger': '1', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try { data = await res.json(); } catch { data = { error: 'The app returned an unexpected response' }; }
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}
const GET = p => api('GET', p);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmt(c, opt = {}) {
  if (c === null || c === undefined) return '';
  if (opt.blankZero && c === 0) return '';
  const s = (Math.abs(c) / 100).toLocaleString('en-AU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return c < 0 ? (opt.paren ? `(${s})` : `-${s}`) : s;
}
const $fmt = c => (c < 0 ? '-$' : '$') + fmt(Math.abs(c || 0));
const numCell = (c, opt = {}) => `<td class="num${c < 0 ? ' neg' : ''}">${fmt(c, opt)}</td>`;
function parseMoney(v) {
  if (v === null || v === undefined) return 0;
  v = String(v).replace(/[$,\s]/g, '');
  if (v === '') return 0;
  let neg = false;
  if (/^\(.*\)$/.test(v)) { neg = true; v = v.slice(1, -1); }
  if (!/^-?(\d+\.?\d{0,2}|\.\d{1,2})$/.test(v)) return NaN;
  const c = Math.round(parseFloat(v) * 100);
  return neg ? -c : c;
}
function dfmt(iso) { if (!iso) return ''; const [y, m, d] = iso.split('-'); return `${d}/${m}/${y}`; }
function monthLabel(ym) { const [y, m] = ym.split('-').map(Number); return new Date(y, m - 1, 1).toLocaleDateString('en-AU', { month: 'short', year: 'numeric' }); }
function monthLong(ym) { const [y, m] = ym.split('-').map(Number); return new Date(y, m - 1, 1).toLocaleDateString('en-AU', { month: 'long', year: 'numeric' }); }
function addDays(iso, n) { const d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n); return isoDate(d); }
function isoDate(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; }
function addMonthsYM(ym, n) { let [y, m] = ym.split('-').map(Number); m += n; while (m > 12) { m -= 12; y++; } while (m < 1) { m += 12; y--; } return `${y}-${String(m).padStart(2, '0')}`; }
function fyStartYM(iso) { const m0 = +S.settings.fy_start_month || 7; const [y, m] = iso.split('-').map(Number); return `${m >= m0 ? y : y - 1}-${String(m0).padStart(2, '0')}`; }
const acct = id => S.accounts.find(a => a.id == id);
const contactById = id => S.contacts.find(c => c.id == id);
const acctLabel = id => { const a = acct(id); return a ? `${a.code} ${a.name}` : ''; };

let toastTimer;
function toast(msg, err = false) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ''; }, err ? 6000 : 3000);
}
function fail(e) { toast(e.message || String(e), true); }

function modal(html, onMount) {
  const m = document.getElementById('modal');
  m.firstElementChild.innerHTML = html; m.hidden = false;
  const first = m.querySelector('input, select, textarea');
  if (first) setTimeout(() => first.focus(), 30);
  if (onMount) onMount(m.firstElementChild);
}
function closeModal() { document.getElementById('modal').hidden = true; }
document.getElementById('modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
function confirmBox(message, okLabel = 'OK', danger = false) {
  return new Promise(resolve => {
    modal(`<h2>Please confirm</h2><p>${message}</p><div class="modal-foot"><button data-x="no">Cancel</button>
      <button class="${danger ? 'danger' : 'primary'}" data-x="yes">${esc(okLabel)}</button></div>`, el => {
      el.querySelector('[data-x=no]').onclick = () => { closeModal(); resolve(false); };
      el.querySelector('[data-x=yes]').onclick = () => { closeModal(); resolve(true); };
    });
  });
}

function download(filename, rowsArr) {
  const csv = rowsArr.map(r => r.map(v => {
    v = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  }).join(',')).join('\r\n');
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const c2d = c => (c / 100).toFixed(2);

const TYPE_GROUPS = [['asset', 'Assets'], ['liability', 'Liabilities'], ['equity', 'Equity'], ['income', 'Income'], ['cos', 'Cost of Sales'], ['expense', 'Expenses']];
const TYPE_LABEL = Object.fromEntries(TYPE_GROUPS);
const isBank = a => a.subtype === 'bank';
const postable = a => !['ar', 'ap'].includes(a.subtype);

function acctOptions(sel, filter = () => true, blank = 'Choose account…') {
  let h = blank !== null ? `<option value="">${esc(blank)}</option>` : '';
  for (const [t, label] of TYPE_GROUPS) {
    const list = S.accounts.filter(a => a.type === t && (!a.archived || a.id == sel) && filter(a));
    if (!list.length) continue;
    h += `<optgroup label="${label}">` + list.map(a =>
      `<option value="${a.id}" ${a.id == sel ? 'selected' : ''}>${esc(a.code)} ${esc(a.name)}</option>`).join('') + '</optgroup>';
  }
  return h;
}
function bankOptions(sel, blank = null) {
  const list = S.accounts.filter(a => isBank(a) && (!a.archived || a.id == sel));
  return (blank ? `<option value="">${esc(blank)}</option>` : '') +
    list.map(a => `<option value="${a.id}" ${a.id == sel ? 'selected' : ''}>${esc(a.name)}</option>`).join('');
}
const firstBank = () => (S.accounts.find(a => isBank(a) && !a.archived && a.type === 'asset') || S.accounts.find(a => isBank(a) && !a.archived) || {}).id;
function contactOptions(sel, kind, blank = 'Choose…') {
  const ok = c => (!c.archived || c.id == sel) && (!kind || c.kind === kind || c.kind === 'both');
  return `<option value="">${esc(blank)}</option>` + S.contacts.filter(ok).map(c =>
    `<option value="${c.id}" ${c.id == sel ? 'selected' : ''}>${esc(c.name)}</option>`).join('') +
    `<option value="__new">+ Add new contact…</option>`;
}

async function refreshState() {
  const st = await GET('/api/state');
  Object.assign(S, st);
  document.getElementById('bizname').textContent = S.settings.business_name || '';
  document.title = S.settings.business_name ? `${S.settings.business_name} – Ledger` : 'Ledger';
  const made = (st.generated || []).filter(g => !g.skipped);
  const skipped = (st.generated || []).filter(g => g.skipped);
  if (made.length) toast(`Created ${made.length} recurring transaction${made.length > 1 ? 's' : ''}`);
  if (skipped.length) toast(`${skipped.length} recurring transaction(s) were skipped – see the Dashboard`, true);
}

function page(title, sub, actions, body) {
  main.innerHTML = `<div class="page-head"><div><h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ''}</div>
    <div class="actions">${actions || ''}</div></div>${body}`;
}
const $ = (sel, root = main) => root.querySelector(sel);
const $$ = (sel, root = main) => [...root.querySelectorAll(sel)];
const val = (sel, root = main) => { const e = $(sel, root); return e ? e.value : ''; };

function sourceHref(source, sourceId, journalId) {
  if (source === 'sale') return `#/sales/${sourceId}`;
  if (source === 'bill') return `#/purchases/${sourceId}`;
  if (source === 'receipt' || source === 'payment') return `#/payment/${sourceId}`;
  if (source === 'opening') return '#/opening';
  return `#/txn/${journalId}`;
}
const SOURCE_LABEL = { sale: 'Invoice', bill: 'Bill', receipt: 'Customer payment', payment: 'Supplier payment', spend: 'Spend money',
  receive: 'Receive money', transfer: 'Transfer', journal: 'Journal', opening: 'Opening balance' };

// Quick-add contact from any contact dropdown
function wireContactSelect(sel, kind, onChange) {
  sel.addEventListener('change', async () => {
    if (sel.value === '__new') {
      sel.value = '';
      const id = await quickContact(kind);
      if (id) { sel.innerHTML = contactOptions(id, kind); sel.value = String(id); }
    }
    onChange && onChange(sel.value);
  });
}
function quickContact(kind) {
  return new Promise(resolve => {
    modal(`<h2>New contact</h2>
      <div class="form-grid"><div class="field"><label>Name</label><input id="qc-name"></div>
      <div class="field"><label>Type</label><select id="qc-kind">
        <option value="customer" ${kind === 'customer' ? 'selected' : ''}>Customer</option>
        <option value="supplier" ${kind === 'supplier' ? 'selected' : ''}>Supplier</option>
        <option value="both">Both</option></select></div>
      <div class="field"><label>Email</label><input id="qc-email"></div>
      <div class="field"><label>Payment terms (days)</label><input id="qc-terms" type="number" min="0" value="${kind === 'supplier' ? 30 : 14}"></div></div>
      <div class="modal-foot"><button data-x="no">Cancel</button><button class="primary" data-x="yes">Add contact</button></div>`, el => {
      el.querySelector('[data-x=no]').onclick = () => { closeModal(); resolve(null); };
      el.querySelector('[data-x=yes]').onclick = async () => {
        try {
          const r = await api('POST', '/api/contacts', { name: val('#qc-name', el), kind: val('#qc-kind', el), email: val('#qc-email', el), terms_days: val('#qc-terms', el) });
          await refreshState(); closeModal(); resolve(r.id);
        } catch (e) { fail(e); }
      };
    });
  });
}

// ---------------------------------------------------------------- line editor
function lineEditor(el, { lines = [], filter = postable, amountLabel = 'Amount', onChange = () => {} } = {}) {
  let rows = lines.length ? lines.map(l => ({ account_id: l.account_id || '', description: l.description || '', amount: l.amount || 0 }))
    : [{ account_id: '', description: '', amount: 0 }];
  const total = () => rows.reduce((s, r) => s + (isNaN(r.amount) ? 0 : r.amount), 0);
  function render() {
    el.innerHTML = `<div class="table-wrap"><table class="lines"><thead><tr><th style="width:40%">Description</th><th style="width:36%">Account</th>
      <th class="num" style="width:18%">${amountLabel}</th><th></th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr data-i="${i}"><td><input data-f="description" value="${esc(r.description)}" placeholder="Description"></td>
        <td><select data-f="account_id">${acctOptions(r.account_id, filter)}</select></td>
        <td><input class="num" data-f="amount" value="${r.amount ? fmt(r.amount) : ''}" placeholder="0.00" inputmode="decimal"></td>
        <td><button type="button" class="rm" title="Remove line">×</button></td></tr>`).join('')}
      </tbody></table></div>
      <button type="button" class="small add-line">+ Add line</button>
      <div class="totals-box"><table><tr class="grand"><td>Total</td><td class="num tot">${$fmt(total())}</td></tr></table></div>`;
    $$('tr[data-i]', el).forEach(tr => {
      const i = +tr.dataset.i;
      tr.querySelector('[data-f=description]').oninput = e => { rows[i].description = e.target.value; };
      tr.querySelector('[data-f=account_id]').onchange = e => { rows[i].account_id = e.target.value; onChange(); };
      const amt = tr.querySelector('[data-f=amount]');
      amt.oninput = () => {
        rows[i].amount = parseMoney(amt.value);
        amt.style.borderColor = isNaN(rows[i].amount) ? 'var(--bad)' : '';
        el.querySelector('.tot').textContent = $fmt(total()); onChange();
      };
      amt.onblur = () => { if (!isNaN(rows[i].amount) && amt.value) amt.value = fmt(rows[i].amount); };
      tr.querySelector('.rm').onclick = () => {
        if (rows.length > 1) rows.splice(i, 1); else rows[0] = { account_id: '', description: '', amount: 0 };
        render(); onChange();
      };
    });
    el.querySelector('.add-line').onclick = () => {
      rows.push({ account_id: '', description: '', amount: 0 }); render();
      $$('tr[data-i]', el).pop().querySelector('input').focus();
    };
  }
  render();
  return {
    total,
    setDefaultAccount(id) { if (id && rows.length === 1 && !rows[0].account_id) { rows[0].account_id = String(id); render(); } },
    get() {
      const out = [];
      rows.forEach((r, i) => {
        if (isNaN(r.amount)) throw new Error(`Line ${i + 1}: the amount isn't a valid number`);
        if (!r.account_id && !r.amount && !r.description) return;
        if (!r.account_id) throw new Error(`Line ${i + 1}: choose an account`);
        out.push({ account_id: +r.account_id, description: r.description.trim(), amount: r.amount });
      });
      if (!out.length) throw new Error('Add at least one line');
      return out;
    },
  };
}

// ---------------------------------------------------------------- router
const ROUTES = [
  [/^#\/dashboard$/, dashboard],
  [/^#\/setup(?:\/(\d))?$/, m => setupPage(+(m[1] || 1))],
  [/^#\/sales$/, () => invoiceList('sale')],
  [/^#\/sales\/new$/, () => invoiceForm('sale')],
  [/^#\/sales\/(\d+)$/, m => invoiceView('sale', m[1])],
  [/^#\/sales\/(\d+)\/edit$/, m => invoiceForm('sale', m[1])],
  [/^#\/purchases$/, () => invoiceList('bill')],
  [/^#\/purchases\/new$/, () => invoiceForm('bill')],
  [/^#\/purchases\/(\d+)$/, m => invoiceView('bill', m[1])],
  [/^#\/purchases\/(\d+)\/edit$/, m => invoiceForm('bill', m[1])],
  [/^#\/receive$/, () => paymentForm('receipt')],
  [/^#\/pay$/, () => paymentForm('payment')],
  [/^#\/payment\/(\d+)$/, m => paymentView(m[1])],
  [/^#\/banking$/, bankingHome],
  [/^#\/banking\/(spend|receive|transfer)$/, m => txnForm(m[1])],
  [/^#\/txn\/(\d+)$/, m => txnForm(null, m[1])],
  [/^#\/reconcile$/, reconcilePage],
  [/^#\/recurring$/, recurringList],
  [/^#\/recurring\/new$/, () => recurringForm()],
  [/^#\/recurring\/(\d+)$/, m => recurringForm(m[1])],
  [/^#\/reports\/pl$/, reportPL],
  [/^#\/reports\/bs$/, reportBS],
  [/^#\/reports\/aged-ar$/, () => reportAged('sale')],
  [/^#\/reports\/aged-ap$/, () => reportAged('bill')],
  [/^#\/reports\/tb$/, reportTB],
  [/^#\/contacts$/, contactsList],
  [/^#\/contacts\/new$/, () => contactForm()],
  [/^#\/contacts\/(\d+)$/, m => contactForm(m[1])],
  [/^#\/accounts$/, accountsPage],
  [/^#\/register\/(\d+)$/, m => registerPage(m[1])],
  [/^#\/journal$/, journalList],
  [/^#\/journal\/new$/, () => txnForm('journal')],
  [/^#\/settings$/, settingsPage],
  [/^#\/opening$/, openingPage],
  [/^#\/audit$/, auditPage],
];
let QS = new URLSearchParams();

async function router() {
  const hash = location.hash || '#/dashboard';
  const [path, qs] = hash.split('?');
  QS = new URLSearchParams(qs || '');
  const sec = path.slice(2);
  $$('#nav nav a', document).forEach(a => {
    const s = a.dataset.sec;
    a.classList.toggle('active', sec === s || sec.startsWith(s + '/') || (s === 'banking' && (sec.startsWith('txn') || sec.startsWith('register'))) ||
      (s === 'sales' && sec === 'receive') || (s === 'purchases' && sec === 'pay') || (s === 'journal' && sec === 'journal/new'));
  });
  for (const [rx, fn] of ROUTES) {
    const m = path.match(rx);
    if (m) {
      main.innerHTML = '<div class="loading">Loading…</div>';
      window.scrollTo(0, 0);
      try { await fn(m); } catch (e) { main.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; console.error(e); }
      return;
    }
  }
  location.hash = '#/dashboard';
}
window.addEventListener('hashchange', router);

function go(h) { if (location.hash === h) router(); else location.hash = h; }
function afterSave(fallback) {
  const r = RETURN_TO; RETURN_TO = null;
  go(r || fallback);
}

// ---------------------------------------------------------------- dashboard
async function dashboard() {
  const d = await GET('/api/dashboard');
  const setupBanner = S.settings.setup_done !== '1'
    ? `<div class="notice warn">Setup isn't finished yet. <a href="#/setup">Continue setup</a> to enter your start date and opening balances.</div>` : '';
  const banks = d.banks.map(b => `<div class="stat"><div class="label">${esc(b.name)}</div>
      <a class="value" href="#/register/${b.id}">${$fmt(b.balance)}</a>
      <div class="note">${b.last_reconciled ? `Reconciled to ${dfmt(b.last_reconciled)}` : 'Not reconciled yet'}
      ${b.uncleared ? ` · <a href="#/reconcile?account=${b.id}">${b.uncleared} unreconciled</a>` : ''}</div></div>`).join('');
  const tm = d.this_month, lm = d.last_month;
  const recent = d.recent_recurring.length ? `<table><tbody>${d.recent_recurring.map(r => `<tr><td>${dfmt(r.date)}</td><td>${esc(r.name)}</td>
      <td>${r.skipped ? `<span class="pill bad">Skipped</span> <span class="muted">${esc(r.reason)}</span>` : `<span class="pill ok">Created</span>`}</td>
      <td class="num">${r.total !== undefined ? $fmt(r.total) : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">Nothing yet</div>';
  const upcoming = d.upcoming_recurring.length ? `<table><tbody>${d.upcoming_recurring.map(r => `<tr class="click" data-href="#/recurring/${r.id}">
      <td>${dfmt(r.next_date)}</td><td>${esc(r.name)}${r.contact_name ? ` <span class="muted">· ${esc(r.contact_name)}</span>` : ''}</td>
      <td class="num">${$fmt(r.total)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">Nothing due in the next 30 days</div>';
  page('Dashboard', new Date(S.today + 'T00:00:00').toLocaleDateString('en-AU', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }),
    `<a class="btn" href="#/sales/new">New invoice</a><a class="btn" href="#/purchases/new">New bill</a>
     <a class="btn" href="#/banking/spend">Spend money</a><a class="btn primary" href="#/reconcile">Reconcile</a>`,
    `${setupBanner}
    <div class="grid g4" style="margin-bottom:16px">${banks}</div>
    <div class="grid g4" style="margin-bottom:16px">
      <div class="stat"><div class="label">Owed to you</div><a class="value" href="#/sales">${$fmt(d.receivable.total)}</a>
        <div class="note">${d.receivable.count} open invoice${d.receivable.count === 1 ? '' : 's'}${d.receivable.overdue ? ` · <span class="neg">${$fmt(d.receivable.overdue)} overdue</span>` : ''}</div></div>
      <div class="stat"><div class="label">You owe</div><a class="value" href="#/purchases">${$fmt(d.payable.total)}</a>
        <div class="note">${d.payable.count} open bill${d.payable.count === 1 ? '' : 's'}${d.payable.overdue ? ` · <span class="neg">${$fmt(d.payable.overdue)} overdue</span>` : ''}</div></div>
      <div class="stat"><div class="label">Profit · ${monthLabel(tm.month)}</div><a class="value${tm.profit < 0 ? ' neg' : ''}" href="#/reports/pl">${$fmt(tm.profit)}</a>
        <div class="note">Income ${$fmt(tm.income)} · Expenses ${$fmt(tm.expenses)}</div></div>
      <div class="stat"><div class="label">Profit · ${monthLabel(lm.month)}</div><div class="value${lm.profit < 0 ? ' neg' : ''}">${$fmt(lm.profit)}</div>
        <div class="note">Income ${$fmt(lm.income)} · Expenses ${$fmt(lm.expenses)}</div></div>
    </div>
    <div class="grid g2">
      <div class="panel flush"><h2>Coming up (recurring)</h2>${upcoming}</div>
      <div class="panel flush"><h2>Recently created by recurring</h2>${recent}</div>
    </div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

// ---------------------------------------------------------------- setup wizard
async function setupPage(step) {
  const steps = `<div class="steps">${[1, 2, 3, 4].map(i => `<span class="${i <= step ? 'on' : ''}"></span>`).join('')}</div>`;
  const conv = S.settings.conversion_date;
  if (step === 1) {
    const defConv = S.today.slice(0, 7) + '-01';
    main.innerHTML = `<div class="setup">${steps}<div class="panel"><h1 style="margin-top:0">Welcome to Ledger</h1>
      <p class="muted">A few details to get your books started. You can change these later in Settings.</p>
      <div class="field"><label>Business name</label><input id="s-name" value="${esc(S.settings.business_name)}"></div>
      <div class="form-grid">
        <div class="field"><label>Start date</label><input id="s-conv" type="date" value="${esc(conv || defConv)}">
          <div class="hint">The first day you'll record transactions in Ledger. Your opening balances are as at the day before.
          The start of a month (or financial year) is easiest.</div></div>
        <div class="field"><label>Financial year starts in</label><select id="s-fy">${[...Array(12)].map((_, i) =>
          `<option value="${i + 1}" ${+S.settings.fy_start_month === i + 1 ? 'selected' : ''}>${new Date(2000, i, 1).toLocaleDateString('en-AU', { month: 'long' })}</option>`).join('')}</select></div>
        <div class="field"><label>Next invoice number</label><input id="s-num" type="number" value="${esc(S.settings.next_invoice_number)}">
          <div class="hint">Carry on from your MYOB numbering.</div></div>
      </div>
      <div class="modal-foot"><button class="primary" id="s-next">Continue</button></div></div></div>`;
    $('#s-next').onclick = async () => {
      try {
        await api('PUT', '/api/settings', { business_name: val('#s-name'), conversion_date: val('#s-conv'), fy_start_month: val('#s-fy'), next_invoice_number: val('#s-num') });
        await refreshState(); go('#/setup/2');
      } catch (e) { fail(e); }
    };
  } else if (step === 2) {
    const banks = S.accounts.filter(isBank);
    main.innerHTML = `<div class="setup">${steps}<div class="panel"><h2>Your bank and credit card accounts</h2>
      <p class="muted">Rename these to match your real accounts, or add more. You can fine-tune the full chart of accounts later.</p>
      <table><thead><tr><th>Code</th><th>Name</th><th>Kind</th><th></th></tr></thead><tbody>
      ${banks.map(a => `<tr data-id="${a.id}"><td style="width:110px"><input class="b-code" value="${esc(a.code)}"></td><td><input class="b-name" value="${esc(a.name)}"></td>
        <td>${a.type === 'asset' ? 'Bank account' : 'Credit card'}</td><td>${a.used ? '' : `<button class="small danger b-del">Remove</button>`}</td></tr>`).join('')}
      </tbody></table>
      <div class="actions" style="margin-top:10px"><button class="small" id="b-add-bank">+ Bank account</button><button class="small" id="b-add-cc">+ Credit card</button></div>
      <div class="modal-foot"><button onclick="go('#/setup/1')">Back</button><button class="primary" id="s-next">Save &amp; continue</button></div></div></div>`;
    const saveAll = async () => {
      for (const tr of $$('tr[data-id]')) {
        const a = acct(tr.dataset.id);
        const code = tr.querySelector('.b-code').value, name = tr.querySelector('.b-name').value;
        if (code !== a.code || name !== a.name) await api('PUT', `/api/accounts/${a.id}`, { code, name });
      }
    };
    const add = async type => {
      try {
        await saveAll();
        const pre = type === 'asset' ? '1-11' : '2-11';
        let n = 10; while (S.accounts.some(a => a.code === `${pre}${n}`)) n += 10;
        await api('POST', '/api/accounts', { code: `${pre}${n}`, name: type === 'asset' ? 'New Bank Account' : 'New Credit Card', type, subtype: 'bank' });
        await refreshState(); setupPage(2);
      } catch (e) { fail(e); }
    };
    $('#b-add-bank').onclick = () => add('asset');
    $('#b-add-cc').onclick = () => add('liability');
    $$('.b-del').forEach(b => b.onclick = async () => {
      try { await saveAll(); await api('DELETE', `/api/accounts/${b.closest('tr').dataset.id}`); await refreshState(); setupPage(2); } catch (e) { fail(e); }
    });
    $('#s-next').onclick = async () => { try { await saveAll(); await refreshState(); go('#/setup/3'); } catch (e) { fail(e); } };
  } else if (step === 3) {
    await openingPage(true, steps);
  } else if (step === 4) {
    const [ob, sales, bills] = await Promise.all([GET('/api/opening'), GET('/api/invoices?kind=sale&status=all'), GET('/api/invoices?kind=bill&status=all')]);
    const hs = sales.filter(i => i.historical && i.status === 'open'), hb = bills.filter(i => i.historical && i.status === 'open');
    const check = (label, opening, items) => opening === items
      ? `<span class="pill ok">Matches</span>` : `<span class="pill bad">${label} opening balance is ${$fmt(opening)} but the items below total ${$fmt(items)}</span>`;
    const list = (arr, kind) => arr.length ? `<table><tbody>${arr.map(i => `<tr class="click" data-href="#/${kind === 'sale' ? 'sales' : 'purchases'}/${i.id}">
      <td>${esc(i.number || '')}</td><td>${dfmt(i.date)}</td><td>${esc(i.contact_name)}</td><td class="num">${$fmt(i.total)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">None entered</div>';
    main.innerHTML = `<div class="setup">${steps}<div class="panel"><h2>Unpaid invoices and bills at your start date</h2>
      <p class="muted">Enter each invoice and bill that was still unpaid on ${dfmt(addDays(conv, -1))}, dated with its original date.
      Anything dated before ${dfmt(conv)} is treated as an opening item: it shows in your aged reports and can be paid, but doesn't post to the ledger again
      (your opening Accounts Receivable/Payable balances already include it).</p>
      <h2 style="margin-top:18px">Customer invoices ${check('Accounts Receivable', ob.ar_opening, ob.historical_sales)}</h2>${list(hs, 'sale')}
      <a class="btn small" href="#/sales/new?hist=1" style="margin-top:8px">+ Add unpaid invoice</a>
      <h2 style="margin-top:22px">Supplier bills ${check('Accounts Payable', ob.ap_opening, ob.historical_bills)}</h2>${list(hb, 'bill')}
      <a class="btn small" href="#/purchases/new?hist=1" style="margin-top:8px">+ Add unpaid bill</a>
      <div class="modal-foot"><button onclick="go('#/setup/3')">Back</button><button class="primary" id="s-finish">Finish setup</button></div></div></div>`;
    $$('tr[data-href]').forEach(tr => tr.onclick = () => { RETURN_TO = '#/setup/4'; go(tr.dataset.href); });
    $$('a.btn.small').forEach(a => a.onclick = () => { RETURN_TO = '#/setup/4'; });
    $('#s-finish').onclick = async () => {
      if ((ob.ar_opening !== ob.historical_sales || ob.ap_opening !== ob.historical_bills) &&
        !await confirmBox('Your unpaid items don\'t match the opening Receivable/Payable balances yet. Finish anyway? You can fix this later.', 'Finish anyway')) return;
      try { await api('PUT', '/api/settings', { setup_done: true }); await refreshState(); toast('Setup complete'); go('#/dashboard'); } catch (e) { fail(e); }
    };
  }
}

// ---------------------------------------------------------------- opening balances
async function openingPage(inSetup = false, steps = '') {
  const ob = await GET('/api/opening');
  const conv = ob.conversion_date;
  if (!conv) { main.innerHTML = `<div class="notice warn">Set your start date first. <a href="#/setup">Go to setup</a></div>`; return; }
  const accts = S.accounts.filter(a => ['asset', 'liability', 'equity'].includes(a.type) && a.subtype !== 'hist' && !a.archived);
  const vals = {}; accts.forEach(a => { vals[a.id] = ob.balances[a.id] || 0; });
  const locked = ob.locked;
  const rowsHtml = TYPE_GROUPS.filter(([t]) => ['asset', 'liability', 'equity'].includes(t)).map(([t, label]) =>
    `<tr class="sechead"><td colspan="2">${label}</td></tr>` + accts.filter(a => a.type === t).map(a =>
      `<tr class="sub"><td>${esc(a.code)} ${esc(a.name)}${a.subtype === 'ar' ? ' <span class="muted">(total of unpaid invoices)</span>' : ''}${a.subtype === 'ap' ? ' <span class="muted">(total of unpaid bills)</span>' : ''}</td>
       <td style="width:180px"><input class="num ob" data-id="${a.id}" value="${vals[a.id] ? fmt(vals[a.id]) : ''}" placeholder="0.00" ${locked ? 'disabled' : ''}></td></tr>`).join('')).join('');
  const body = `${locked ? `<div class="notice info">Opening balances are locked because you've reconciled a bank account since. Undo reconciliations to change them.</div>` : ''}
    <div class="panel"><p class="muted" style="margin-top:0">Enter each balance as at close of business on <b>${dfmt(addDays(conv, -1))}</b>, taken from your MYOB balance sheet at that date.
    Enter amounts as positive numbers in their normal direction (e.g. a credit card you owe $500 on is 500). A contra account like accumulated depreciation is entered as a negative.</p>
    <table><tbody>${rowsHtml}
      <tr class="sub"><td>3-9999 Historical Balancing <span class="muted">(automatic: should be zero once everything is entered)</span></td><td class="num" id="ob-hist"></td></tr>
      <tr class="total"><td>Total assets</td><td class="num" id="ob-a"></td></tr>
      <tr class="total"><td>Total liabilities + equity</td><td class="num" id="ob-le"></td></tr>
    </tbody></table>
    ${inSetup ? `<div class="modal-foot"><button onclick="go('#/setup/2')">Back</button><button class="primary" id="ob-save">Save &amp; continue</button></div>`
      : `<div class="modal-foot">${locked ? '' : '<button class="primary" id="ob-save">Save opening balances</button>'}</div>`}</div>`;
  if (inSetup) main.innerHTML = `<div class="setup">${steps}<h1>Opening balances</h1>${body}</div>`;
  else page('Opening balances', `As at ${dfmt(addDays(conv, -1))}`, `<a class="btn" href="#/sales/new?hist=1">+ Unpaid invoice at start</a><a class="btn" href="#/purchases/new?hist=1">+ Unpaid bill at start</a>`,
    `${ob.ar_opening !== ob.historical_sales ? `<div class="notice warn">Accounts Receivable opening balance (${$fmt(ob.ar_opening)}) doesn't match the unpaid invoices dated before your start date (${$fmt(ob.historical_sales)}).</div>` : ''}
     ${ob.ap_opening !== ob.historical_bills ? `<div class="notice warn">Accounts Payable opening balance (${$fmt(ob.ap_opening)}) doesn't match the unpaid bills dated before your start date (${$fmt(ob.historical_bills)}).</div>` : ''}
     ${body}`);
  const recalc = () => {
    let A = 0, LE = 0, bad = false;
    $$('.ob').forEach(inp => {
      const v = parseMoney(inp.value); if (isNaN(v)) { bad = true; inp.style.borderColor = 'var(--bad)'; return; }
      inp.style.borderColor = '';
      const a = acct(inp.dataset.id); if (a.type === 'asset') A += v; else LE += v;
    });
    const hist = A - LE;
    $('#ob-hist').textContent = fmt(hist); $('#ob-hist').className = 'num' + (hist ? ' neg' : '');
    $('#ob-a').textContent = fmt(A); $('#ob-le').textContent = fmt(LE + hist);
    return !bad;
  };
  $$('.ob').forEach(i => { i.oninput = recalc; i.onblur = () => { const v = parseMoney(i.value); if (!isNaN(v) && i.value) i.value = fmt(v); }; });
  recalc();
  const save = $('#ob-save');
  if (save) save.onclick = async () => {
    if (!recalc()) return toast('Fix the highlighted amounts', true);
    const balances = {}; $$('.ob').forEach(i => { balances[i.dataset.id] = parseMoney(i.value); });
    try {
      await api('PUT', '/api/opening', { balances }); await refreshState(); toast('Opening balances saved');
      if (inSetup) go('#/setup/4'); else openingPage();
    } catch (e) { fail(e); }
  };
}

// ---------------------------------------------------------------- sales & purchases
const KIND = {
  sale: { base: '#/sales', title: 'Sales', one: 'Invoice', who: 'Customer', ckind: 'customer', pay: '#/receive', payLabel: 'Receive payment' },
  bill: { base: '#/purchases', title: 'Purchases', one: 'Bill', who: 'Supplier', ckind: 'supplier', pay: '#/pay', payLabel: 'Pay bills' },
};

async function invoiceList(kind) {
  const K = KIND[kind];
  const status = QS.get('status') || 'open';
  const list = await GET(`/api/invoices?kind=${kind}&status=${status}`);
  const tabs = ['open', 'overdue', 'paid', 'void', 'all'].map(s => `<a href="${K.base}?status=${s}" class="${s === status ? 'active' : ''}">${s[0].toUpperCase() + s.slice(1)}</a>`).join('');
  const totBal = list.reduce((s, i) => s + i.balance, 0), tot = list.reduce((s, i) => s + (i.status === 'void' ? 0 : i.total), 0);
  page(K.title, kind === 'sale' ? 'Customer invoices and payments' : 'Supplier bills and payments',
    `<a class="btn" href="#/payments?kind=${kind === 'sale' ? 'receipt' : 'payment'}" id="pay-hist">Payment history</a><a class="btn" href="${K.pay}">${K.payLabel}</a><a class="btn primary" href="${K.base}/new">New ${K.one.toLowerCase()}</a>`,
    `<div class="tabs">${tabs}</div><div class="panel flush">${list.length ? `<div class="table-wrap"><table><thead><tr>
      <th>${kind === 'sale' ? 'Number' : 'Ref'}</th><th>Date</th><th>${K.who}</th><th>Due</th><th class="num">Total</th><th class="num">Balance</th><th>Status</th></tr></thead><tbody>
      ${list.map(i => `<tr class="click" data-href="${K.base}/${i.id}"><td>${esc(i.number || '—')}</td><td>${dfmt(i.date)}</td><td>${esc(i.contact_name)}</td>
        <td>${dfmt(i.due_date)}</td><td class="num">${fmt(i.total)}</td><td class="num">${fmt(i.balance)}</td>
        <td><span class="pill ${i.state}">${i.state}</span>${i.historical ? ' <span class="pill" title="Dated before your start date">opening</span>' : ''}${i.recurring_id ? ' <span class="pill" title="Created by a recurring transaction">↻</span>' : ''}</td></tr>`).join('')}
      <tr class="total"><td colspan="4">${list.length} item${list.length === 1 ? '' : 's'}</td><td class="num">${fmt(tot)}</td><td class="num">${fmt(totBal)}</td><td></td></tr>
      </tbody></table></div>` : `<div class="empty">No ${status === 'all' ? '' : status + ' '}${K.one.toLowerCase()}s</div>`}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#pay-hist').onclick = e => { e.preventDefault(); paymentHistory(kind === 'sale' ? 'receipt' : 'payment'); };
}

async function paymentHistory(pkind) {
  const list = await GET(`/api/payments?kind=${pkind}`);
  const kind = pkind === 'receipt' ? 'sale' : 'bill';
  page(pkind === 'receipt' ? 'Customer payments' : 'Supplier payments', '', `<a class="btn" href="${KIND[kind].base}">Back to ${KIND[kind].title.toLowerCase()}</a>`,
    `<div class="panel flush">${list.length ? `<table><thead><tr><th>Date</th><th>${KIND[kind].who}</th><th>Account</th><th>Reference</th><th class="num">Amount</th><th></th></tr></thead><tbody>
    ${list.map(p => `<tr class="click" data-href="#/payment/${p.id}"><td>${dfmt(p.date)}</td><td>${esc(p.contact_name)}</td><td>${esc(p.bank_name)}</td><td>${esc(p.reference || '')}</td>
      <td class="num">${fmt(p.amount)}</td><td>${p.reconciled ? '<span class="pill ok">Reconciled</span>' : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No payments yet</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

async function invoiceView(kind, id) {
  const K = KIND[kind];
  const inv = await GET(`/api/invoices/${id}`);
  const state = inv.status === 'void' ? 'void' : inv.balance === 0 ? 'paid' : inv.due_date < S.today ? 'overdue' : 'open';
  const canEdit = inv.status === 'open' && !inv.payments.length;
  page(`${K.one} ${esc(inv.number || '')}`, `${esc(inv.contact_name)} · <span class="pill ${state}">${state}</span>${inv.historical ? ' <span class="pill">opening item</span>' : ''}`,
    `<a class="btn" href="${K.base}">Back</a>${canEdit ? `<button class="danger" id="iv-void">Void</button><a class="btn" href="${K.base}/${inv.id}/edit">Edit</a>` : ''}
     <button onclick="window.print()">Print</button>${inv.balance > 0 ? `<a class="btn primary" href="${K.pay}?contact=${inv.contact_id}&inv=${inv.id}">${kind === 'sale' ? 'Receive payment' : 'Pay bill'}</a>` : ''}`,
    `${inv.historical ? `<div class="notice info no-print">This is dated before your start date, so it's an opening item: it doesn't post to the ledger (your opening balance already includes it).</div>` : ''}
    <div class="report">
      <div style="display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:22px">
        <div><div style="font-size:20px;font-weight:700">${esc(S.settings.business_name)}</div>
          <div style="font-size:26px;font-weight:300;margin-top:6px;letter-spacing:.04em">${kind === 'sale' ? 'INVOICE' : 'BILL'}</div></div>
        <div style="text-align:right"><div><span class="muted">${kind === 'sale' ? 'Invoice no.' : 'Supplier ref'}</span> <b>${esc(inv.number || '—')}</b></div>
          <div><span class="muted">Date</span> ${dfmt(inv.date)}</div><div><span class="muted">Due</span> ${dfmt(inv.due_date)}</div></div>
      </div>
      <div style="margin-bottom:18px"><div class="muted" style="font-size:12px;text-transform:uppercase">${kind === 'sale' ? 'Bill to' : 'From'}</div>
        <div style="font-weight:600">${esc(inv.contact_name)}</div><div style="white-space:pre-line">${esc(inv.contact_address || '')}</div></div>
      <table><thead><tr><th>Description</th><th class="no-print">Account</th><th class="num">Amount</th></tr></thead><tbody>
        ${inv.lines.map(l => `<tr><td>${esc(l.description || '')}</td><td class="no-print muted">${esc(l.account_code)} ${esc(l.account_name)}</td><td class="num">${fmt(l.amount)}</td></tr>`).join('')}
        <tr class="total"><td>Total</td><td class="no-print"></td><td class="num">${$fmt(inv.total)}</td></tr>
        ${inv.payments.map(p => `<tr><td class="muted">Paid ${dfmt(p.date)}${p.reference ? ` · ${esc(p.reference)}` : ''} <a class="no-print" href="#/payment/${p.payment_id}">view</a></td><td class="no-print muted">${esc(p.bank_name)}</td><td class="num">-${fmt(p.amount)}</td></tr>`).join('')}
        <tr class="grand"><td>Balance due</td><td class="no-print"></td><td class="num">${$fmt(inv.balance)}</td></tr>
      </tbody></table>
      ${inv.memo ? `<p style="margin-top:18px;white-space:pre-line">${esc(inv.memo)}</p>` : ''}
    </div>`);
  const v = $('#iv-void');
  if (v) v.onclick = async () => {
    if (!await confirmBox(`Void ${K.one.toLowerCase()} ${esc(inv.number || '')}? It will be removed from your reports.`, 'Void', true)) return;
    try { await api('POST', `/api/invoices/${inv.id}/void`); await refreshState(); toast('Voided'); go(`${K.base}/${inv.id}`); } catch (e) { fail(e); }
  };
}

async function invoiceForm(kind, id) {
  const K = KIND[kind];
  const inv = id ? await GET(`/api/invoices/${id}`) : null;
  if (inv && (inv.status !== 'open' || inv.payments.length)) { go(`${K.base}/${id}`); return; }
  const conv = S.settings.conversion_date;
  const hist = QS.get('hist') === '1';
  const defDate = inv ? inv.date : (hist && conv ? addDays(conv, -1) : S.today);
  page(inv ? `Edit ${K.one.toLowerCase()} ${esc(inv.number || '')}` : `New ${K.one.toLowerCase()}`, hist ? 'Unpaid at your start date (opening item)' : '',
    `<a class="btn" href="${inv ? `${K.base}/${inv.id}` : K.base}">Cancel</a><button class="primary" id="f-save">Save</button>`,
    `<div class="panel"><div class="form-grid">
      <div class="field"><label>${K.who}</label><select id="f-contact"></select></div>
      <div class="field"><label>${kind === 'sale' ? 'Invoice number' : 'Supplier reference'}</label><input id="f-number" value="${esc(inv ? inv.number : '')}" placeholder="${kind === 'sale' ? `Auto (${esc(S.settings.next_invoice_number)})` : 'Optional'}"></div>
      <div class="field"><label>Date</label><input id="f-date" type="date" value="${defDate}"></div>
      <div class="field"><label>Due date</label><input id="f-due" type="date" value="${inv ? inv.due_date : ''}"><div class="hint" id="f-terms"></div></div>
    </div>
    <div id="f-hist" class="notice info" hidden>Dated before your start date (${dfmt(conv)}): this is an opening item and won't post to the ledger.</div>
    <div id="f-lines"></div>
    <div class="field" style="margin-top:14px"><label>Notes${kind === 'sale' ? ' (shown on the invoice)' : ''}</label><textarea id="f-memo">${esc(inv ? inv.memo : '')}</textarea></div>
    </div>`);
  const filt = kind === 'sale' ? (a => postable(a) && !isBank(a)) : (a => postable(a) && !isBank(a));
  const ed = lineEditor($('#f-lines'), { lines: inv ? inv.lines : [], filter: filt });
  const sel = $('#f-contact');
  sel.innerHTML = contactOptions(inv ? inv.contact_id : (QS.get('contact') || ''), K.ckind);
  let dueTouched = !!inv;
  const updDue = () => {
    const c = contactById(sel.value); const d = val('#f-date');
    $('#f-terms').textContent = c ? `${c.terms_days} day terms` : '';
    if (c && d && !dueTouched) $('#f-due').value = addDays(d, c.terms_days);
    $('#f-hist').hidden = !(conv && d && d < conv);
  };
  wireContactSelect(sel, K.ckind, () => { const c = contactById(sel.value); if (c) ed.setDefaultAccount(c.default_account_id); updDue(); });
  $('#f-date').onchange = updDue;
  $('#f-due').onchange = () => { dueTouched = true; };
  updDue();
  if (!inv && !QS.get('contact')) sel.focus();
  $('#f-save').onclick = async () => {
    try {
      const body = { kind, contact_id: sel.value, number: val('#f-number'), date: val('#f-date'), due_date: val('#f-due'), memo: val('#f-memo'), lines: ed.get() };
      const r = inv ? await api('PUT', `/api/invoices/${inv.id}`, body) : await api('POST', '/api/invoices', body);
      await refreshState(); toast('Saved'); afterSave(`${K.base}/${r.id}`);
    } catch (e) { fail(e); }
  };
}

async function paymentForm(pkind) {
  const kind = pkind === 'receipt' ? 'sale' : 'bill', K = KIND[kind];
  page(pkind === 'receipt' ? 'Receive payment' : 'Pay bills', pkind === 'receipt' ? 'Record money received from a customer' : 'Record money paid to a supplier',
    `<a class="btn" href="${K.base}">Cancel</a><button class="primary" id="p-save">Save payment</button>`,
    `<div class="panel"><div class="form-grid">
      <div class="field"><label>${K.who}</label><select id="p-contact"></select></div>
      <div class="field"><label>${pkind === 'receipt' ? 'Deposit to' : 'Pay from'}</label><select id="p-bank">${bankOptions(PREFILL?.bank_account_id || QS.get('bank') || firstBank())}</select></div>
      <div class="field"><label>Date</label><input id="p-date" type="date" value="${PREFILL?.date || S.today}"></div>
      <div class="field"><label>Reference</label><input id="p-ref" value="${esc(PREFILL?.reference || '')}"></div>
      <div class="field"><label>Amount ${pkind === 'receipt' ? 'received' : 'paid'}</label><input id="p-amount" class="num" placeholder="Allocate automatically" value="${PREFILL?.amount ? fmt(PREFILL.amount) : ''}">
        <div class="hint">Fills invoices oldest first. Or type amounts below.</div></div>
    </div><div id="p-invs"><div class="empty">Choose a ${K.who.toLowerCase()} to see what's outstanding</div></div></div>`);
  const pre = PREFILL; PREFILL = null;
  let invs = [];
  const sel = $('#p-contact');
  sel.innerHTML = contactOptions(QS.get('contact') || '', K.ckind);
  const total = () => $$('.p-alloc').reduce((s, i) => s + (parseMoney(i.value) || 0), 0);
  const renderInvs = () => {
    if (!sel.value) { $('#p-invs').innerHTML = `<div class="empty">Choose a ${K.who.toLowerCase()} to see what's outstanding</div>`; return; }
    if (!invs.length) { $('#p-invs').innerHTML = `<div class="empty">Nothing outstanding for this ${K.who.toLowerCase()}</div>`; return; }
    $('#p-invs').innerHTML = `<table><thead><tr><th>${kind === 'sale' ? 'Invoice' : 'Bill'}</th><th>Date</th><th>Due</th><th class="num">Total</th><th class="num">Balance</th><th class="num" style="width:160px">Payment</th></tr></thead><tbody>
      ${invs.map(i => `<tr><td><a href="${K.base}/${i.id}">${esc(i.number || '—')}</a></td><td>${dfmt(i.date)}</td><td>${dfmt(i.due_date)}${i.state === 'overdue' ? ' <span class="pill overdue">overdue</span>' : ''}</td>
        <td class="num">${fmt(i.total)}</td><td class="num">${fmt(i.balance)}</td>
        <td><div class="inline"><input class="num p-alloc" data-id="${i.id}" data-bal="${i.balance}" placeholder="0.00"><button class="small p-full" title="Pay in full">Full</button></div></td></tr>`).join('')}
      <tr class="total"><td colspan="5">Total payment</td><td class="num" id="p-total">$0.00</td></tr></tbody></table>`;
    $$('.p-alloc').forEach(i => { i.oninput = () => { $('#p-total').textContent = $fmt(total()); }; i.onblur = () => { const v = parseMoney(i.value); if (!isNaN(v) && i.value) i.value = fmt(v); }; });
    $$('.p-full').forEach(b => b.onclick = () => { const i = b.parentElement.querySelector('input'); i.value = fmt(+i.dataset.bal); $('#p-total').textContent = $fmt(total()); });
  };
  const autoAllocate = () => {
    let rem = parseMoney(val('#p-amount'));
    if (isNaN(rem) || !val('#p-amount')) return;
    const ordered = [...$$('.p-alloc')].sort((a, b) => invs.find(i => i.id == a.dataset.id).due_date.localeCompare(invs.find(i => i.id == b.dataset.id).due_date));
    ordered.forEach(i => { const amt = Math.min(rem, +i.dataset.bal); i.value = amt > 0 ? fmt(amt) : ''; rem -= Math.max(amt, 0); });
    $('#p-total').textContent = $fmt(total());
    if (rem > 0) toast(`${$fmt(rem)} is more than the total outstanding`, true);
  };
  const load = async () => {
    invs = sel.value ? (await GET(`/api/invoices?kind=${kind}&status=open&contact_id=${sel.value}`)).sort((a, b) => a.due_date.localeCompare(b.due_date)) : [];
    renderInvs();
    const want = QS.get('inv');
    if (want) { const i = $(`.p-alloc[data-id="${want}"]`); if (i) { i.value = fmt(+i.dataset.bal); $('#p-total').textContent = $fmt(total()); } }
    else autoAllocate();
  };
  wireContactSelect(sel, K.ckind, load);
  $('#p-amount').oninput = autoAllocate;
  $('#p-amount').onblur = () => { const v = parseMoney(val('#p-amount')); if (!isNaN(v) && val('#p-amount')) $('#p-amount').value = fmt(v); };
  await load();
  $('#p-save').onclick = async () => {
    try {
      const allocations = $$('.p-alloc').map(i => {
        const amt = parseMoney(i.value); if (isNaN(amt)) throw new Error('One of the payment amounts isn\'t a valid number');
        return { invoice_id: +i.dataset.id, amount: amt };
      }).filter(a => a.amount);
      const r = await api('POST', '/api/payments', { kind: pkind, contact_id: sel.value, bank_account_id: val('#p-bank'), date: val('#p-date'), reference: val('#p-ref'), allocations });
      await refreshState(); toast('Payment saved');
      if (pre && pre.onSaved) pre.onSaved();
      afterSave(`#/payment/${r.id}`);
    } catch (e) { fail(e); }
  };
}

async function paymentView(id) {
  const p = await GET(`/api/payments/${id}`);
  const kind = p.kind === 'receipt' ? 'sale' : 'bill', K = KIND[kind];
  page(p.kind === 'receipt' ? 'Customer payment' : 'Supplier payment', `${esc(p.contact_name)} · ${dfmt(p.date)}`,
    `<a class="btn" href="${K.base}">Back</a>${p.reconciled ? '<span class="pill ok">Reconciled</span>' : '<button class="danger" id="pv-del">Delete payment</button>'}`,
    `<div class="panel"><div class="grid g4">
      <div><label>Amount</label><div style="font-size:20px;font-weight:650">${$fmt(p.amount)}</div></div>
      <div><label>${p.kind === 'receipt' ? 'Deposited to' : 'Paid from'}</label>${esc(p.bank_name)}</div>
      <div><label>Date</label>${dfmt(p.date)}</div><div><label>Reference</label>${esc(p.reference || '—')}</div></div></div>
    <div class="panel flush"><h2>Applied to</h2><table><thead><tr><th>${K.one}</th><th>Date</th><th class="num">${K.one} total</th><th class="num">Applied</th></tr></thead><tbody>
      ${p.allocations.map(a => `<tr class="click" data-href="${K.base}/${a.invoice_id}"><td>${esc(a.number || '—')}</td><td>${dfmt(a.date)}</td><td class="num">${fmt(a.total)}</td><td class="num">${fmt(a.amount)}</td></tr>`).join('')}
    </tbody></table></div>
    ${p.reconciled ? '<div class="notice info">This payment is reconciled. To delete it, undo the bank reconciliation that includes it first.</div>' : ''}`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  const d = $('#pv-del');
  if (d) d.onclick = async () => {
    if (!await confirmBox('Delete this payment? The invoices it paid will show as outstanding again.', 'Delete', true)) return;
    try { await api('DELETE', `/api/payments/${p.id}`); await refreshState(); toast('Payment deleted'); go(K.base); } catch (e) { fail(e); }
  };
}

// ---------------------------------------------------------------- banking
async function bankingHome() {
  const banks = S.accounts.filter(a => isBank(a) && !a.archived);
  const sel = QS.get('account') || firstBank();
  if (!sel) { page('Banking', '', '', '<div class="empty">No bank accounts. Add one in Chart of Accounts.</div>'); return; }
  await registerView(sel, {
    title: 'Banking',
    picker: `<div class="tabs">${banks.map(b => `<a href="#/banking?account=${b.id}" class="${b.id == sel ? 'active' : ''}">${esc(b.name)}</a>`).join('')}</div>`,
    actions: `<a class="btn" href="#/banking/transfer">Transfer</a><a class="btn" href="#/banking/receive">Receive money</a>
      <a class="btn" href="#/banking/spend">Spend money</a><a class="btn primary" href="#/reconcile?account=${sel}">Reconcile</a>`,
    hashBase: `#/banking?account=${sel}`,
  });
}
async function registerPage(id) {
  await registerView(id, { title: null, actions: `<button onclick="history.back()">Back</button>`, hashBase: `#/register/${id}?` });
}
async function registerView(id, { title, picker = '', actions = '', hashBase }) {
  const from = QS.get('from') || addDays(S.today, -92), to = QS.get('to') || '';
  const r = await GET(`/api/register?account_id=${id}&from=${from}${to ? `&to=${to}` : ''}`);
  const a = r.account, bank = isBank(a);
  const lines = [...r.lines].reverse();
  page(title || `${esc(a.code)} ${esc(a.name)}`, title ? '' : TYPE_LABEL[a.type], actions,
    `${picker}<div class="filters"><div class="field"><label>From</label><input type="date" id="r-from" value="${from}"></div>
      <div class="field"><label>To</label><input type="date" id="r-to" value="${to}"></div>
      <div class="field"><label>Search</label><input id="r-q" placeholder="Description, contact, amount"></div>
      <div style="margin-left:auto;text-align:right"><div class="muted" style="font-size:12px">Balance${to ? ` at ${dfmt(to)}` : ''}</div>
      <div style="font-size:20px;font-weight:650">${$fmt(r.closing)}</div></div></div>
    <div class="panel flush">${lines.length ? `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Description</th><th>Type</th><th>Ref</th>
      <th class="num">${bank ? 'Money in' : 'Debit'}</th><th class="num">${bank ? 'Money out' : 'Credit'}</th><th class="num">Balance</th>${bank ? '<th></th>' : ''}</tr></thead><tbody>
      ${lines.map(l => `<tr class="click" data-href="${sourceHref(l.source, l.source_id, l.journal_id)}" data-q="${esc((l.memo + ' ' + (l.description || '') + ' ' + (l.contact_name || '') + ' ' + fmt(Math.abs(l.amount))).toLowerCase())}">
        <td>${dfmt(l.date)}</td><td>${esc(l.contact_name ? `${l.contact_name}${l.description && !l.description.includes(l.contact_name) ? ' – ' + l.description : ''}` : (l.description || l.memo || ''))}</td>
        <td class="muted">${SOURCE_LABEL[l.source] || l.source}</td><td class="muted">${esc(l.reference || '')}</td>
        <td class="num">${l.amount > 0 ? fmt(l.amount) : ''}</td><td class="num">${l.amount < 0 ? fmt(-l.amount) : ''}</td><td class="num">${fmt(l.balance)}</td>
        ${bank ? `<td title="${l.recon_id ? 'Reconciled' : 'Not reconciled'}">${l.recon_id ? '✓' : ''}</td>` : ''}</tr>`).join('')}
      <tr class="total"><td colspan="6">Opening balance ${dfmt(from)}</td><td class="num">${fmt(r.opening)}</td>${bank ? '<td></td>' : ''}</tr>
      </tbody></table></div>` : '<div class="empty">No transactions in this period</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  const sep = hashBase.includes('?') ? (hashBase.endsWith('?') ? '' : '&') : '?';
  const reload = () => go(`${hashBase}${sep}from=${val('#r-from')}${val('#r-to') ? `&to=${val('#r-to')}` : ''}`);
  $('#r-from').onchange = reload; $('#r-to').onchange = reload;
  $('#r-q').oninput = () => { const q = val('#r-q').toLowerCase().trim(); $$('tr[data-q]').forEach(tr => { tr.hidden = q && !tr.dataset.q.includes(q); }); };
}

async function txnForm(source, id) {
  const j = id ? await GET(`/api/journals/${id}`) : null;
  if (j) source = j.source;
  if (j && !['spend', 'receive', 'transfer', 'journal'].includes(source)) { go(sourceHref(j.source, j.source_id, j.id)); return; }
  const pre = PREFILL; PREFILL = null;
  const ro = j && !j.editable;
  const titles = { spend: 'Spend money', receive: 'Receive money', transfer: 'Transfer money', journal: 'General journal' };
  const backHref = source === 'journal' ? '#/journal' : '#/banking';
  const why = ro ? (j.reconciled ? 'This transaction is reconciled. Undo the reconciliation to change it.' : `This is dated in a locked period (on or before ${dfmt(S.settings.lock_date)}).`) : '';
  let body;
  const d = j || pre || {};
  if (source === 'spend' || source === 'receive') {
    body = `<div class="form-grid">
      <div class="field"><label>${source === 'spend' ? 'Pay from' : 'Deposit to'}</label><select id="t-bank">${bankOptions(d.bank_account_id || QS.get('bank') || firstBank())}</select></div>
      <div class="field"><label>Date</label><input type="date" id="t-date" value="${d.date || S.today}"></div>
      <div class="field"><label>${source === 'spend' ? 'Paid to' : 'Received from'} (optional)</label><select id="t-contact"></select></div>
      <div class="field"><label>Reference</label><input id="t-ref" value="${esc(d.reference || '')}"></div></div>
      <div class="field"><label>Memo</label><input id="t-memo" value="${esc(d.memo || '')}"></div>
      <div id="t-lines"></div>`;
  } else if (source === 'transfer') {
    body = `<div class="form-grid">
      <div class="field"><label>From</label><select id="t-from">${bankOptions(d.from_account_id || firstBank())}</select></div>
      <div class="field"><label>To</label><select id="t-to">${bankOptions(d.to_account_id || '', 'Choose…')}</select></div>
      <div class="field"><label>Date</label><input type="date" id="t-date" value="${d.date || S.today}"></div>
      <div class="field"><label>Amount</label><input id="t-amount" class="num" value="${d.amount ? fmt(d.amount) : ''}"></div></div>
      <div class="field"><label>Memo</label><input id="t-memo" value="${esc(d.memo || '')}"></div>`;
  } else {
    body = `<div class="form-grid"><div class="field"><label>Date</label><input type="date" id="t-date" value="${d.date || S.today}"></div>
      <div class="field"><label>Reference</label><input id="t-ref" value="${esc(d.reference || '')}"></div></div>
      <div class="field"><label>Memo (required)</label><input id="t-memo" value="${esc(d.memo || '')}"></div>
      <div id="t-jlines"></div>`;
  }
  page(j ? `${titles[source]}` : titles[source], j ? `${dfmt(j.date)}${j.recurring_id ? ' · created by a recurring transaction' : ''}` : '',
    `<a class="btn" href="${RETURN_TO || backHref}" id="t-cancel">${ro ? 'Back' : 'Cancel'}</a>${j && !ro ? '<button class="danger" id="t-del">Delete</button>' : ''}${ro ? '' : '<button class="primary" id="t-save">Save</button>'}`,
    `${ro ? `<div class="notice info">${why}</div>` : ''}<div class="panel">${body}</div>`);
  $('#t-cancel').onclick = () => { RETURN_TO = null; };
  let ed, jed;
  if (source === 'spend' || source === 'receive') {
    const csel = $('#t-contact');
    csel.innerHTML = contactOptions(d.contact_id || '', null, 'None');
    wireContactSelect(csel, source === 'spend' ? 'supplier' : 'customer', () => { const c = contactById(csel.value); if (c) ed.setDefaultAccount(c.default_account_id); });
    ed = lineEditor($('#t-lines'), { lines: j ? j.items : (pre?.lines || []), filter: a => postable(a) });
  } else if (source === 'journal') {
    jed = journalEditor($('#t-jlines'), j ? j.lines.map(l => ({ account_id: l.account_id, description: l.description || '', debit: l.amount > 0 ? l.amount : 0, credit: l.amount < 0 ? -l.amount : 0 })) : []);
  }
  if (ro) $$('input, select, textarea, .lines button, button.add-line').forEach(e => { e.disabled = true; });
  const save = $('#t-save');
  if (save) save.onclick = async () => {
    try {
      let b;
      if (source === 'spend' || source === 'receive') b = { bank_account_id: val('#t-bank'), date: val('#t-date'), contact_id: val('#t-contact') || null, reference: val('#t-ref'), memo: val('#t-memo'), lines: ed.get() };
      else if (source === 'transfer') { const amt = parseMoney(val('#t-amount')); if (isNaN(amt)) throw new Error('Amount isn\'t a valid number'); b = { from_account_id: val('#t-from'), to_account_id: val('#t-to'), date: val('#t-date'), amount: amt, memo: val('#t-memo') }; }
      else b = { date: val('#t-date'), reference: val('#t-ref'), memo: val('#t-memo'), lines: jed.get() };
      const r = j ? await api('PUT', `/api/journals/${j.id}`, b) : await api('POST', `/api/txn/${source}`, b);
      await refreshState(); toast('Saved');
      if (pre && pre.onSaved) pre.onSaved(r.id);
      afterSave(source === 'journal' ? '#/journal' : `#/banking?account=${b.bank_account_id || b.from_account_id}`);
    } catch (e) { fail(e); }
  };
  const del = $('#t-del');
  if (del) del.onclick = async () => {
    if (!await confirmBox('Delete this transaction?', 'Delete', true)) return;
    try { await api('DELETE', `/api/journals/${j.id}`); await refreshState(); toast('Deleted'); afterSave(backHref); } catch (e) { fail(e); }
  };
}

function journalEditor(el, lines) {
  let rows = lines.length ? lines : [{ account_id: '', description: '', debit: 0, credit: 0 }, { account_id: '', description: '', debit: 0, credit: 0 }];
  const tot = k => rows.reduce((s, r) => s + (isNaN(r[k]) ? 0 : r[k]), 0);
  function render() {
    el.innerHTML = `<table class="lines"><thead><tr><th style="width:34%">Account</th><th>Description</th><th class="num" style="width:15%">Debit</th><th class="num" style="width:15%">Credit</th><th></th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr data-i="${i}"><td><select data-f="account_id">${acctOptions(r.account_id, postable)}</select></td><td><input data-f="description" value="${esc(r.description)}"></td>
      <td><input class="num" data-f="debit" value="${r.debit ? fmt(r.debit) : ''}"></td><td><input class="num" data-f="credit" value="${r.credit ? fmt(r.credit) : ''}"></td><td><button type="button" class="rm">×</button></td></tr>`).join('')}
      </tbody></table><button type="button" class="small add-line">+ Add line</button>
      <div class="totals-box"><table><tr><td>Total debits</td><td class="num jd"></td></tr><tr><td>Total credits</td><td class="num jc"></td></tr><tr class="grand"><td>Out of balance</td><td class="num jo"></td></tr></table></div>`;
    const upd = () => { const d = tot('debit'), c = tot('credit'); el.querySelector('.jd').textContent = fmt(d); el.querySelector('.jc').textContent = fmt(c); const o = el.querySelector('.jo'); o.textContent = fmt(d - c); o.className = 'num jo' + (d !== c ? ' neg' : ''); };
    $$('tr[data-i]', el).forEach(tr => {
      const i = +tr.dataset.i;
      tr.querySelector('[data-f=account_id]').onchange = e => { rows[i].account_id = e.target.value; };
      tr.querySelector('[data-f=description]').oninput = e => { rows[i].description = e.target.value; };
      ['debit', 'credit'].forEach(k => { const inp = tr.querySelector(`[data-f=${k}]`); inp.oninput = () => { rows[i][k] = parseMoney(inp.value); upd(); }; inp.onblur = () => { if (!isNaN(rows[i][k]) && inp.value) inp.value = fmt(rows[i][k]); }; });
      tr.querySelector('.rm').onclick = () => { if (rows.length > 2) rows.splice(i, 1); render(); };
    });
    el.querySelector('.add-line').onclick = () => { rows.push({ account_id: '', description: '', debit: 0, credit: 0 }); render(); };
    upd();
  }
  render();
  return {
    get() {
      return rows.filter(r => r.account_id || r.debit || r.credit).map((r, i) => {
        if (isNaN(r.debit) || isNaN(r.credit)) throw new Error(`Line ${i + 1}: invalid amount`);
        if (!r.account_id) throw new Error(`Line ${i + 1}: choose an account`);
        return { account_id: +r.account_id, description: r.description, debit: r.debit || 0, credit: r.credit || 0 };
      });
    },
  };
}

async function journalList() {
  const list = await GET('/api/journals?source=journal');
  page('General journal', 'Adjustments such as depreciation, owner contributions or corrections', `<a class="btn" href="#/audit">Audit log</a><a class="btn primary" href="#/journal/new">New journal</a>`,
    `<div class="panel flush">${list.length ? `<table><thead><tr><th>Date</th><th>Memo</th><th>Reference</th><th class="num">Amount</th></tr></thead><tbody>
    ${list.map(j => `<tr class="click" data-href="#/txn/${j.id}"><td>${dfmt(j.date)}</td><td>${esc(j.memo || '')}</td><td>${esc(j.reference || '')}</td><td class="num">${fmt(j.total)}</td></tr>`).join('')}
    </tbody></table>` : '<div class="empty">No journals yet</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

// ---------------------------------------------------------------- reconcile
const R = { account_id: null, statement_date: null, statement_balance: '', ticked: new Set(), csv: null, showLater: false };

async function reconcilePage() {
  const acc = QS.get('account') || R.account_id || firstBank();
  if (!acc) { page('Reconcile', '', '', '<div class="empty">No bank accounts</div>'); return; }
  if (String(acc) !== String(R.account_id)) { R.account_id = acc; R.ticked = new Set(); R.csv = null; R.statement_balance = ''; R.statement_date = null; }
  const data = await GET(`/api/reconcile?account_id=${acc}`);
  const s = data.sign;
  R.statement_date = R.statement_date || S.today;
  const validIds = new Set(data.lines.map(l => l.id));
  R.ticked = new Set([...R.ticked].filter(id => validIds.has(id)));
  const lineById = Object.fromEntries(data.lines.map(l => [l.id, l]));
  const cc = data.account.type === 'liability';
  page('Reconcile', `${esc(data.account.name)}${data.last ? ` · last reconciled to ${dfmt(data.last.statement_date)} at ${$fmt(data.last.statement_balance)}` : ''}`,
    `<label class="btn" style="margin:0">Import statement CSV<input type="file" id="rc-file" accept=".csv,text/csv" hidden></label>
     <button id="rc-add">+ Bank fee / other</button>`,
    `<div class="filters">
      <div class="field"><label>Account</label><select id="rc-acc">${bankOptions(acc)}</select></div>
      <div class="field"><label>Statement date</label><input type="date" id="rc-date" value="${R.statement_date}"></div>
      <div class="field"><label>Closing balance on statement${cc ? ' (amount owed)' : ''}</label><input id="rc-bal" class="num" value="${esc(R.statement_balance)}" placeholder="0.00" style="width:170px"></div>
      <label class="check" style="margin-bottom:9px"><input type="checkbox" id="rc-later" ${R.showLater ? 'checked' : ''}> Show items after statement date</label>
    </div>
    <div class="recon-bar">
      <div><div class="k">Last reconciled</div><div class="v">${$fmt(data.cleared_balance)}</div></div>
      <div><div class="k">+ Money in ticked</div><div class="v" id="rc-in"></div></div>
      <div><div class="k">− Money out ticked</div><div class="v" id="rc-out"></div></div>
      <div><div class="k">= Calculated</div><div class="v" id="rc-calc"></div></div>
      <div id="rc-diffbox"><div class="k">Out by</div><div class="v" id="rc-diff"></div></div>
      <div style="margin-left:auto" class="actions"><button id="rc-all">Tick all shown</button><button id="rc-none">Clear ticks</button><button class="primary" id="rc-go" disabled>Reconcile</button></div>
    </div>
    <div id="rc-csv"></div>
    <div class="panel flush"><div class="table-wrap"><table><thead><tr><th style="width:36px"></th><th>Date</th><th>Description</th><th>Type</th><th>Ref</th><th class="num">Money in</th><th class="num">Money out</th></tr></thead>
      <tbody id="rc-body"></tbody></table></div></div>
    <div class="panel flush"><h2>Reconciliation history</h2>${data.history.length ? `<table><thead><tr><th>Statement date</th><th class="num">Balance</th><th class="num">Items</th><th>Done</th><th></th></tr></thead><tbody>
      ${data.history.map((h, i) => `<tr><td>${dfmt(h.statement_date)}</td><td class="num">${fmt(h.statement_balance)}</td><td class="num">${h.note === 'opening' ? 'Opening balance' : h.line_count}</td>
        <td class="muted">${h.note === 'opening' ? '' : esc(h.created_at.replace('T', ' ').slice(0, 16))}</td>
        <td>${i === 0 && h.note !== 'opening' ? '<button class="small" id="rc-undo">Undo</button>' : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">None yet</div>'}</div>`);

  const body = $('#rc-body');
  const matchedIds = new Set();
  const drawLines = () => {
    const sd = val('#rc-date');
    const shown = data.lines.filter(l => R.showLater || l.date <= sd || R.ticked.has(l.id));
    body.innerHTML = shown.length ? shown.map(l => `<tr class="${R.ticked.has(l.id) ? 'ticked' : ''}${matchedIds.has(l.id) ? ' matched' : ''}" data-id="${l.id}">
      <td><input type="checkbox" ${R.ticked.has(l.id) ? 'checked' : ''}></td><td>${dfmt(l.date)}${l.date > sd ? ' <span class="pill overdue">after</span>' : ''}</td>
      <td>${esc(l.contact_name ? `${l.contact_name}${l.description && !l.description.includes(l.contact_name) ? ' – ' + l.description : ''}` : (l.description || l.memo || ''))} <a href="${sourceHref(l.source, l.source_id, l.journal_id)}" class="muted" title="Open">↗</a></td>
      <td class="muted">${SOURCE_LABEL[l.source] || ''}</td><td class="muted">${esc(l.reference || '')}</td>
      <td class="num">${l.amount > 0 ? fmt(l.amount) : ''}</td><td class="num">${l.amount < 0 ? fmt(-l.amount) : ''}</td></tr>`).join('')
      : `<tr><td colspan="7" class="empty">Everything up to this date is reconciled. Enter new transactions, or import your statement.</td></tr>`;
    $$('tr[data-id]', body).forEach(tr => {
      tr.onclick = e => {
        if (e.target.tagName === 'A') return;
        const id = +tr.dataset.id;
        if (R.ticked.has(id)) R.ticked.delete(id); else R.ticked.add(id);
        tr.classList.toggle('ticked', R.ticked.has(id)); tr.querySelector('input').checked = R.ticked.has(id); calc();
      };
    });
    calc();
  };
  const calc = () => {
    let inn = 0, out = 0;
    R.ticked.forEach(id => { const l = lineById[id]; if (l.amount > 0) inn += l.amount; else out -= l.amount; });
    const calcBal = data.cleared_balance + s * (inn - out);
    $('#rc-in').textContent = $fmt(inn); $('#rc-out').textContent = $fmt(out); $('#rc-calc').textContent = $fmt(calcBal);
    const sb = parseMoney(val('#rc-bal'));
    const box = $('#rc-diffbox');
    if (!val('#rc-bal') || isNaN(sb)) { $('#rc-diff').textContent = '—'; box.className = ''; $('#rc-go').disabled = true; return; }
    const diff = sb - calcBal;
    $('#rc-diff').textContent = $fmt(diff); box.className = diff === 0 ? 'diff-ok' : 'diff-bad';
    const lateTicked = [...R.ticked].some(id => lineById[id].date > val('#rc-date'));
    $('#rc-go').disabled = diff !== 0 || lateTicked;
    $('#rc-go').title = lateTicked ? 'A ticked item is dated after the statement date' : '';
  };
  $('#rc-acc').onchange = () => go(`#/reconcile?account=${val('#rc-acc')}`);
  $('#rc-date').onchange = () => { R.statement_date = val('#rc-date'); drawLines(); };
  $('#rc-bal').oninput = () => { R.statement_balance = val('#rc-bal'); calc(); };
  $('#rc-bal').onblur = () => { const v = parseMoney(val('#rc-bal')); if (!isNaN(v) && val('#rc-bal')) { $('#rc-bal').value = fmt(v); R.statement_balance = fmt(v); } };
  $('#rc-later').onchange = () => { R.showLater = $('#rc-later').checked; drawLines(); };
  $('#rc-all').onclick = () => { const sd = val('#rc-date'); data.lines.forEach(l => { if (l.date <= sd) R.ticked.add(l.id); }); drawLines(); };
  $('#rc-none').onclick = () => { R.ticked.clear(); drawLines(); };
  $('#rc-go').onclick = async () => {
    try {
      await api('POST', '/api/reconcile', { account_id: acc, statement_date: val('#rc-date'), statement_balance: parseMoney(val('#rc-bal')), line_ids: [...R.ticked] });
      toast('Reconciled'); R.ticked.clear(); R.csv = null; R.statement_balance = ''; await refreshState(); reconcilePage();
    } catch (e) { fail(e); }
  };
  const undo = $('#rc-undo');
  if (undo) undo.onclick = async () => {
    if (!await confirmBox('Undo the most recent reconciliation? Its items will become unreconciled again.', 'Undo')) return;
    try { await api('POST', '/api/reconcile/undo', { account_id: acc }); toast('Reconciliation undone'); reconcilePage(); } catch (e) { fail(e); }
  };
  $('#rc-add').onclick = () => quickBankTxn(acc, {}, () => reconcilePage());

  // CSV statement matching
  const drawCsv = () => {
    if (!R.csv) { $('#rc-csv').innerHTML = ''; return; }
    matchedIds.clear();
    const used = new Set();
    R.csv.forEach(c => {
      c.match = null;
      let best = null, bestGap = 99;
      data.lines.forEach(l => {
        if (used.has(l.id) || l.amount !== c.amount) return;
        const gap = Math.abs((new Date(l.date) - new Date(c.date)) / 86400000);
        if (gap <= 7 && gap < bestGap) { best = l; bestGap = gap; }
      });
      if (best) { c.match = best.id; used.add(best.id); matchedIds.add(best.id); }
    });
    const sd = val('#rc-date');
    const inPeriod = R.csv.filter(c => c.date <= sd);
    inPeriod.forEach(c => { if (c.match) R.ticked.add(c.match); });
    const unmatched = inPeriod.filter(c => !c.match);
    $('#rc-csv').innerHTML = `<div class="panel flush"><h2>Statement lines (${inPeriod.length} up to ${dfmt(sd)}) · ${inPeriod.length - unmatched.length} matched · ${unmatched.length} unmatched
        <button class="small" id="rc-csv-clear" style="float:right">Close statement</button></h2>
      ${unmatched.length ? `<p class="muted" style="padding:0 18px">These statement lines have no matching transaction in Ledger. Create one, or enter it properly (e.g. as a customer payment) and come back.</p>
      <table><thead><tr><th>Date</th><th>Description</th><th class="num">Amount</th><th></th></tr></thead><tbody>
      ${unmatched.map(c => `<tr><td>${dfmt(c.date)}</td><td>${esc(c.description)}</td><td class="num${c.amount < 0 ? ' neg' : ''}">${fmt(c.amount)}</td>
        <td class="actions" style="justify-content:flex-end">${c.amount > 0 ? `<button class="small" data-pay="receipt" data-i="${R.csv.indexOf(c)}">Customer payment</button>` : `<button class="small" data-pay="payment" data-i="${R.csv.indexOf(c)}">Bill payment</button>`}
        <button class="small primary" data-new="${R.csv.indexOf(c)}">${c.amount < 0 ? 'Spend money' : 'Receive money'}</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="notice good" style="margin:12px 18px">Every statement line matches a transaction in Ledger.</div>'}</div>`;
    $('#rc-csv-clear').onclick = () => { R.csv = null; drawCsv(); drawLines(); };
    $$('[data-new]').forEach(b => b.onclick = () => {
      const c = R.csv[+b.dataset.new];
      quickBankTxn(acc, { date: c.date, description: c.description, amount: Math.abs(c.amount), kind: c.amount < 0 ? 'spend' : 'receive' }, () => reconcilePage());
    });
    $$('[data-pay]').forEach(b => b.onclick = () => {
      const c = R.csv[+b.dataset.i];
      PREFILL = { date: c.date, amount: Math.abs(c.amount), reference: c.description.slice(0, 40), bank_account_id: acc };
      RETURN_TO = `#/reconcile?account=${acc}`;
      go(b.dataset.pay === 'receipt' ? '#/receive' : '#/pay');
    });
  };
  $('#rc-file').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const txt = await f.text();
    const parsed = parseCSV(txt);
    if (parsed.length < 1) return toast('That file looks empty', true);
    csvMapping(parsed, cc, lines => {
      R.csv = lines;
      const maxD = lines.reduce((m, l) => (l.date > m ? l.date : m), '');
      if (maxD && (!R.statement_date || R.statement_date === S.today)) { R.statement_date = maxD; $('#rc-date').value = maxD; }
      drawCsv(); drawLines();
      toast(`Imported ${lines.length} statement lines`);
    });
    e.target.value = '';
  };
  drawCsv();
  drawLines();
}

function quickBankTxn(bankId, pre, done) {
  const kind0 = pre.kind || 'spend';
  modal(`<h2>New bank transaction</h2>
    <div class="form-grid">
      <div class="field"><label>Type</label><select id="q-kind"><option value="spend" ${kind0 === 'spend' ? 'selected' : ''}>Spend money (money out)</option><option value="receive" ${kind0 === 'receive' ? 'selected' : ''}>Receive money (money in)</option></select></div>
      <div class="field"><label>Account</label><select id="q-bank">${bankOptions(bankId)}</select></div>
      <div class="field"><label>Date</label><input type="date" id="q-date" value="${pre.date || S.today}"></div>
      <div class="field"><label>Amount</label><input id="q-amt" class="num" value="${pre.amount ? fmt(pre.amount) : ''}"></div>
    </div>
    <div class="field"><label>Description</label><input id="q-desc" value="${esc(pre.description || '')}"></div>
    <div class="field"><label>Allocate to account</label><select id="q-acct">${acctOptions('', a => postable(a) && !isBank(a))}</select></div>
    <div class="hint">For a customer or supplier payment against an invoice or bill, use Receive payment / Pay bills instead so it clears what they owe.</div>
    <div class="modal-foot"><button data-x="no">Cancel</button><button class="primary" data-x="yes">Save</button></div>`, el => {
    const bankFee = S.accounts.find(a => a.code === '6-1200');
    if (bankFee && kind0 === 'spend' && (!pre.description || /\bfees?\b|charge/i.test(pre.description))) el.querySelector('#q-acct').value = bankFee.id;
    el.querySelector('[data-x=no]').onclick = closeModal;
    el.querySelector('[data-x=yes]').onclick = async () => {
      try {
        const amt = parseMoney(val('#q-amt', el)); if (isNaN(amt) || amt <= 0) throw new Error('Enter an amount');
        if (!val('#q-acct', el)) throw new Error('Choose an account');
        await api('POST', `/api/txn/${val('#q-kind', el)}`, { bank_account_id: val('#q-bank', el), date: val('#q-date', el), memo: val('#q-desc', el),
          lines: [{ account_id: +val('#q-acct', el), description: val('#q-desc', el), amount: amt }] });
        closeModal(); toast('Saved'); done && done();
      } catch (e) { fail(e); }
    };
  });
}

function parseCSV(text) {
  const rows = []; let row = [], cell = '', q = false;
  text = text.replace(/^﻿/, '');
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += ch;
    } else if (ch === '"') q = true;
    else if (ch === ',') { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') { if (ch === '\r' && text[i + 1] === '\n') i++; row.push(cell); if (row.some(c => c.trim() !== '')) rows.push(row); row = []; cell = ''; }
    else cell += ch;
  }
  row.push(cell); if (row.some(c => c.trim() !== '')) rows.push(row);
  return rows.map(r => r.map(c => c.trim()));
}

function parseAnyDate(s, fmtPref) {
  s = s.trim().split(' ')[0];
  let m;
  if ((m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`;
  if ((m = s.match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})$/))) {
    let [a, b, y] = [m[1], m[2], m[3]]; if (y.length === 2) y = '20' + y;
    const [d, mo] = fmtPref === 'mdy' ? [b, a] : [a, b];
    return `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`;
  }
  if ((m = s.match(/^(\d{1,2})[\s\-]([A-Za-z]{3})[A-Za-z]*[\s\-](\d{2,4})$/))) {
    const mo = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'].indexOf(m[2].toLowerCase()) + 1;
    let y = m[3]; if (y.length === 2) y = '20' + y;
    if (mo) return `${y}-${String(mo).padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  }
  return null;
}

function csvMapping(rows, isCard, onDone) {
  const first = rows[0];
  const looksHeader = first.some(c => /date|amount|desc|narr|debit|credit|details|balance|transaction/i.test(c)) && !first.some(c => parseAnyDate(c));
  const ncol = Math.max(...rows.map(r => r.length));
  const guess = re => { const i = looksHeader ? first.findIndex(c => re.test(c)) : -1; return i; };
  const sample = looksHeader ? rows[1] || [] : first;
  let gDate = guess(/date/i); if (gDate < 0) gDate = sample.findIndex(c => parseAnyDate(c));
  let gAmt = guess(/^amount$|amount/i);
  const gDr = guess(/debit|withdraw|money out/i), gCr = guess(/credit|deposit|money in/i);
  let gDesc = guess(/desc|narr|details|particulars|transaction/i);
  if (!looksHeader) { // e.g. CBA: date, amount, description, balance
    const numIdx = sample.map((c, i) => (!isNaN(parseMoney(c)) && c !== '' && i !== gDate ? i : -1)).filter(i => i >= 0);
    if (gAmt < 0 && numIdx.length) gAmt = numIdx[0];
    if (gDesc < 0) gDesc = sample.findIndex((c, i) => i !== gDate && isNaN(parseMoney(c)));
  }
  const mode0 = gAmt >= 0 || gDr < 0 ? 'single' : 'split';
  const colOpts = sel => `<option value="-1">—</option>` + [...Array(ncol)].map((_, i) => `<option value="${i}" ${i === sel ? 'selected' : ''}>${looksHeader ? esc(first[i] || `Column ${i + 1}`) : `Column ${i + 1}`}${!looksHeader && sample[i] ? ` (e.g. ${esc(sample[i].slice(0, 24))})` : ''}</option>`).join('');
  modal(`<h2>Match up your statement columns</h2>
    <label class="check"><input type="checkbox" id="m-head" ${looksHeader ? 'checked' : ''}> First row is a header</label>
    <div class="form-grid" style="margin-top:10px">
      <div class="field"><label>Date column</label><select id="m-date">${colOpts(gDate)}</select></div>
      <div class="field"><label>Date format</label><select id="m-df"><option value="dmy">Day/Month/Year</option><option value="mdy">Month/Day/Year</option></select></div>
      <div class="field"><label>Description column</label><select id="m-desc">${colOpts(gDesc)}</select></div>
      <div class="field"><label>Amounts are in</label><select id="m-mode"><option value="single" ${mode0 === 'single' ? 'selected' : ''}>One column (+/-)</option><option value="split" ${mode0 === 'split' ? 'selected' : ''}>Separate debit &amp; credit columns</option></select></div>
      <div class="field m-single"><label>Amount column</label><select id="m-amt">${colOpts(gAmt)}</select></div>
      <div class="field m-split"><label>Money out (debit) column</label><select id="m-dr">${colOpts(gDr)}</select></div>
      <div class="field m-split"><label>Money in (credit) column</label><select id="m-cr">${colOpts(gCr)}</select></div>
    </div>
    <label class="check"><input type="checkbox" id="m-flip" ${isCard ? 'checked' : ''}> Flip signs (tick if purchases show as positive numbers, common on credit card exports)</label>
    <div id="m-prev" style="margin-top:12px"></div>
    <div class="modal-foot"><button data-x="no">Cancel</button><button class="primary" data-x="yes">Import</button></div>`, el => {
    const g = id => +el.querySelector(id).value;
    const build = () => {
      const head = el.querySelector('#m-head').checked, mode = el.querySelector('#m-mode').value, flip = el.querySelector('#m-flip').checked;
      el.querySelectorAll('.m-single').forEach(x => { x.hidden = mode !== 'single'; });
      el.querySelectorAll('.m-split').forEach(x => { x.hidden = mode !== 'split'; });
      const out = [], bad = [];
      (head ? rows.slice(1) : rows).forEach((r, i) => {
        const date = parseAnyDate(r[g('#m-date')] || '', el.querySelector('#m-df').value);
        let amt;
        if (mode === 'single') amt = parseMoney(r[g('#m-amt')]);
        else { const dr = parseMoney(r[g('#m-dr')] || ''), cr = parseMoney(r[g('#m-cr')] || ''); amt = (isNaN(cr) ? 0 : Math.abs(cr)) - (isNaN(dr) ? 0 : Math.abs(dr)); if (isNaN(dr) && isNaN(cr)) amt = NaN; }
        if (flip) amt = -amt;
        if (!date || isNaN(amt) || amt === 0) { bad.push(i + 1); return; }
        out.push({ date, amount: amt, description: r[g('#m-desc')] || '' });
      });
      el.querySelector('#m-prev').innerHTML = `<div class="hint">${out.length} lines read${bad.length ? `, ${bad.length} skipped (no date or zero amount)` : ''}. First few:</div>
        <table><tbody>${out.slice(0, 4).map(o => `<tr><td>${dfmt(o.date)}</td><td>${esc(o.description)}</td><td class="num${o.amount < 0 ? ' neg' : ''}">${fmt(o.amount)}</td></tr>`).join('')}</tbody></table>
        <div class="hint">Money out should be negative, money in positive.</div>`;
      return out;
    };
    el.querySelectorAll('select, input').forEach(x => { x.onchange = build; });
    build();
    el.querySelector('[data-x=no]').onclick = closeModal;
    el.querySelector('[data-x=yes]').onclick = () => { const out = build(); if (!out.length) return toast('No usable lines found. Check the column choices.', true); closeModal(); onDone(out); };
  });
}

// ---------------------------------------------------------------- recurring
const FREQ_LABEL = { weekly: 'Weekly', fortnightly: 'Fortnightly', monthly: 'Monthly', quarterly: 'Quarterly', yearly: 'Yearly' };
const RKIND = { sale: 'Sales invoice', bill: 'Supplier bill', spend: 'Spend money', receive: 'Receive money' };

async function recurringList() {
  const list = await GET('/api/recurring');
  page('Recurring transactions', 'Created automatically on their date whenever Ledger is open (or the next time you open it)', `<a class="btn primary" href="#/recurring/new">New recurring</a>`,
    `<div class="panel flush">${list.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Creates</th><th>Who / account</th><th>How often</th><th>Next</th><th class="num">Amount</th><th>Status</th></tr></thead><tbody>
    ${list.map(r => `<tr class="click" data-href="#/recurring/${r.id}"><td>${esc(r.name)}</td><td>${RKIND[r.kind]}</td><td>${esc(r.contact_name || r.bank_name || '')}</td>
      <td>${FREQ_LABEL[r.frequency]}${r.anchor_day === 31 && r.frequency !== 'weekly' && r.frequency !== 'fortnightly' ? ' (month end)' : ''}</td>
      <td>${r.active ? dfmt(r.next_date) : '—'}</td><td class="num">${fmt(r.total)}</td>
      <td>${r.active ? '<span class="pill ok">Active</span>' : '<span class="pill">Stopped</span>'} <span class="muted">${r.generated_count} made</span></td></tr>`).join('')}
    </tbody></table></div>` : `<div class="empty">No recurring transactions yet.<br>Set up things like rent, subscriptions or a monthly retainer invoice.</div>`}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

async function recurringForm(id) {
  const r = id ? await GET(`/api/recurring/${id}`) : null;
  page(r ? esc(r.name) : 'New recurring transaction', r ? `Next: ${dfmt(r.next_date)}` : '',
    `<a class="btn" href="#/recurring">Cancel</a>${r ? '<button class="danger" id="r-del">Delete</button><button id="r-skip">Skip next</button>' : ''}<button class="primary" id="r-save">Save</button>`,
    `<div class="panel"><div class="form-grid">
      <div class="field"><label>Name</label><input id="r-name" value="${esc(r ? r.name : '')}" placeholder="e.g. Office rent"></div>
      <div class="field"><label>Creates a</label><select id="r-kind">${Object.entries(RKIND).map(([k, v]) => `<option value="${k}" ${r && r.kind === k ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
      <div class="field r-contact"><label id="r-clabel">Customer</label><select id="r-contact"></select></div>
      <div class="field r-bank"><label>Bank account</label><select id="r-bank">${bankOptions(r ? r.bank_account_id : firstBank())}</select></div>
      <div class="field"><label>How often</label><select id="r-freq">${Object.entries(FREQ_LABEL).map(([k, v]) => `<option value="${k}" ${(r ? r.frequency : 'monthly') === k ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
      <div class="field"><label>Next date</label><input type="date" id="r-next" value="${r ? r.next_date : S.today}"></div>
      <div class="field"><label>End date (optional)</label><input type="date" id="r-end" value="${r && r.end_date ? r.end_date : ''}"></div>
      <div class="field r-terms"><label>Payment terms (days)</label><input type="number" min="0" id="r-terms" value="${r ? r.terms_days : 14}"></div>
    </div>
    <div class="inline" style="gap:18px;margin-bottom:12px">
      <label class="check r-eom"><input type="checkbox" id="r-eom" ${r && r.anchor_day === 31 ? 'checked' : ''}> Always on the last day of the month</label>
      <label class="check"><input type="checkbox" id="r-active" ${!r || r.active ? 'checked' : ''}> Active</label></div>
    <div id="r-lines"></div>
    <div class="field" style="margin-top:12px"><label>Notes / memo</label><input id="r-memo" value="${esc(r ? r.memo || '' : '')}"></div>
    <div class="hint">If the next date is today or in the past, saving will create the due transaction(s) straight away.</div></div>`);
  const ed = lineEditor($('#r-lines'), { lines: r ? r.lines : [], filter: a => postable(a) && !isBank(a) });
  const csel = $('#r-contact');
  const upd = () => {
    const k = val('#r-kind');
    const inv = k === 'sale' || k === 'bill';
    $('.r-bank').hidden = inv; $('.r-terms').hidden = !inv;
    $('#r-clabel').textContent = k === 'sale' ? 'Customer' : k === 'bill' ? 'Supplier' : (k === 'spend' ? 'Paid to (optional)' : 'Received from (optional)');
    const cur = csel.value || (r ? r.contact_id : '');
    csel.innerHTML = contactOptions(cur, k === 'sale' || k === 'receive' ? 'customer' : 'supplier', inv ? 'Choose…' : 'None');
    $('.r-eom').hidden = ['weekly', 'fortnightly'].includes(val('#r-freq'));
  };
  wireContactSelect(csel, 'customer', () => { const c = contactById(csel.value); if (c) ed.setDefaultAccount(c.default_account_id); });
  $('#r-kind').onchange = upd; $('#r-freq').onchange = upd;
  upd();
  const payload = () => ({
    name: val('#r-name'), kind: val('#r-kind'), contact_id: csel.value || null, bank_account_id: val('#r-bank'), frequency: val('#r-freq'),
    next_date: val('#r-next'), end_date: val('#r-end'), terms_days: val('#r-terms'), memo: val('#r-memo'), lines: ed.get(),
    last_day_of_month: $('#r-eom').checked && !['weekly', 'fortnightly'].includes(val('#r-freq')), active: $('#r-active').checked,
  });
  $('#r-save').onclick = async () => {
    try {
      const res = r ? await api('PUT', `/api/recurring/${r.id}`, payload()) : await api('POST', '/api/recurring', payload());
      await refreshState();
      const made = (res.generated || []).filter(g => !g.skipped).length;
      toast(made ? `Saved and created ${made} transaction${made > 1 ? 's' : ''}` : 'Saved');
      go('#/recurring');
    } catch (e) { fail(e); }
  };
  if (r) {
    $('#r-del').onclick = async () => {
      if (!await confirmBox('Delete this recurring transaction? Transactions it already created are kept.', 'Delete', true)) return;
      try { await api('DELETE', `/api/recurring/${r.id}`); toast('Deleted'); go('#/recurring'); } catch (e) { fail(e); }
    };
    $('#r-skip').onclick = async () => {
      if (!await confirmBox(`Skip the one due on ${dfmt(r.next_date)}?`, 'Skip')) return;
      try { const x = await api('POST', `/api/recurring/${r.id}/skip`); toast(`Next is now ${dfmt(x.next_date)}`); recurringForm(r.id); } catch (e) { fail(e); }
    };
  }
}

// ---------------------------------------------------------------- reports
const RF = {};
function reportHead(title, period) {
  return `<div class="report-title"><div class="biz-name">${esc(S.settings.business_name)}</div><h2>${title}</h2><div class="period">${period}</div></div>`;
}

async function reportPL() {
  const cur = S.today.slice(0, 7);
  RF.plFrom = RF.plFrom || fyStartYM(S.today); RF.plTo = RF.plTo || cur;
  const d = await GET(`/api/reports/pl?from=${RF.plFrom}&to=${RF.plTo}`);
  const multi = d.months.length > 1;
  const head = `<tr><th></th>${multi ? d.months.map(m => `<th class="num">${monthLabel(m)}</th>`).join('') : ''}<th class="num">${multi ? 'Total' : monthLabel(d.months[0])}</th></tr>`;
  const valsRow = (label, vals, total, cls = '', href = '') => `<tr class="${cls}${href ? ' click' : ''}" ${href ? `data-href="${href}"` : ''}><td>${label}</td>${multi ? vals.map(v => numCell(v, { paren: true })).join('') : ''}${numCell(total, { paren: true })}</tr>`;
  let rowsHtml = '';
  d.sections.forEach(s => {
    if (!s.rows.length && s.type === 'cos') return;
    rowsHtml += `<tr class="sechead"><td colspan="${d.months.length + 2}">${s.title}</td></tr>`;
    s.rows.forEach(r => { rowsHtml += valsRow(esc(r.name), r.values, r.total, 'sub', `#/register/${r.account_id}?from=${d.from}&to=${d.to}`); });
    if (!s.rows.length) rowsHtml += `<tr class="sub"><td class="muted" colspan="${d.months.length + 2}">None</td></tr>`;
    rowsHtml += valsRow(`Total ${s.title.toLowerCase()}`, s.totals, s.total, 'total');
    if (s.type === 'cos' && s.rows.length) rowsHtml += valsRow('Gross profit', d.gross_profit, d.gross_profit_total, 'total');
  });
  rowsHtml += valsRow('Net profit', d.net_profit, d.net_profit_total, 'grand');
  const fy = fyStartYM(S.today);
  page('Profit &amp; Loss', '', `<button id="x-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button>`,
    `<div class="filters">
      <div class="field"><label>From month</label><input type="month" id="f-from" value="${RF.plFrom}"></div>
      <div class="field"><label>To month</label><input type="month" id="f-to" value="${RF.plTo}"></div>
      <div class="actions"><button class="small" data-r="${cur},${cur}">This month</button><button class="small" data-r="${addMonthsYM(cur, -1)},${addMonthsYM(cur, -1)}">Last month</button>
      <button class="small" data-r="${fy},${cur}">This financial year</button><button class="small" data-r="${addMonthsYM(fy, -12)},${addMonthsYM(fy, -1)}">Last financial year</button></div></div>
    <div class="report">${reportHead('Profit &amp; Loss', d.months.length === 1 ? monthLong(d.months[0]) : `${monthLong(d.months[0])} to ${monthLong(d.months[d.months.length - 1])}`)}
      <div class="table-wrap"><table><thead>${head}</thead><tbody>${rowsHtml}</tbody></table></div></div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  const apply = () => { RF.plFrom = val('#f-from'); RF.plTo = val('#f-to'); reportPL(); };
  $('#f-from').onchange = apply; $('#f-to').onchange = apply;
  $$('[data-r]').forEach(b => b.onclick = () => { [RF.plFrom, RF.plTo] = b.dataset.r.split(','); reportPL(); });
  $('#x-csv').onclick = () => {
    const out = [[S.settings.business_name], ['Profit & Loss'], [], ['Account', ...(multi ? d.months : []), multi ? 'Total' : d.months[0]]];
    d.sections.forEach(s => {
      out.push([s.title]);
      s.rows.forEach(r => out.push([`${r.code} ${r.name}`, ...(multi ? r.values.map(c2d) : []), c2d(r.total)]));
      out.push([`Total ${s.title}`, ...(multi ? s.totals.map(c2d) : []), c2d(s.total)]);
    });
    out.push(['Net profit', ...(multi ? d.net_profit.map(c2d) : []), c2d(d.net_profit_total)]);
    download(`Profit-and-Loss-${RF.plFrom}-to-${RF.plTo}.csv`, out);
  };
}

async function reportBS() {
  RF.bsTo = RF.bsTo || S.today.slice(0, 7); RF.bsCount = RF.bsCount || 1;
  const d = await GET(`/api/reports/bs?to=${RF.bsTo}&count=${RF.bsCount}`);
  const n = d.dates.length;
  const head = `<tr><th></th>${d.dates.map(x => `<th class="num">${dfmt(x)}</th>`).join('')}</tr>`;
  const valsRow = (label, vals, cls = '', href = '') => `<tr class="${cls}${href ? ' click' : ''}" ${href ? `data-href="${href}"` : ''}><td>${label}</td>${vals.map(v => numCell(v, { paren: true })).join('')}</tr>`;
  let rowsHtml = '';
  const [A, L, E] = d.sections;
  [A, L].forEach(s => {
    rowsHtml += `<tr class="sechead"><td colspan="${n + 1}">${s.title}</td></tr>`;
    s.rows.forEach(r => { rowsHtml += valsRow(esc(r.name), r.values, 'sub', `#/register/${r.account_id}?from=2000-01-01&to=${d.dates[n - 1]}`); });
    if (!s.rows.length) rowsHtml += `<tr class="sub"><td class="muted" colspan="${n + 1}">None</td></tr>`;
    rowsHtml += valsRow(`Total ${s.title.toLowerCase()}`, s.totals, 'total');
  });
  rowsHtml += valsRow('Net assets', d.net_assets, 'grand');
  rowsHtml += `<tr class="sechead"><td colspan="${n + 1}">${E.title}</td></tr>`;
  E.rows.forEach(r => { rowsHtml += valsRow(esc(r.name) + (r.computed ? ' <span class="muted">(this financial year)</span>' : ''), r.values, 'sub', r.account_id ? `#/register/${r.account_id}?from=2000-01-01&to=${d.dates[n - 1]}` : ''); });
  rowsHtml += valsRow('Total equity', E.totals, 'grand');
  const hist = E.rows.find(r => r.subtype === 'hist');
  page('Balance Sheet', '', `<button id="x-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button>`,
    `<div class="filters"><div class="field"><label>As at end of</label><input type="month" id="f-to" value="${RF.bsTo}"></div>
      <div class="field"><label>Compare</label><select id="f-count">${[[1, 'Single month'], [2, '2 months'], [3, '3 months'], [6, '6 months'], [12, '12 months'], [13, '13 months (year on year)']].map(([v, l]) => `<option value="${v}" ${+RF.bsCount === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div></div>
    ${!d.balanced ? '<div class="notice bad">The balance sheet doesn\'t balance. This shouldn\'t happen. Please check the Trial Balance.</div>' : ''}
    ${hist && hist.values.some(v => v) ? '<div class="notice warn">Historical Balancing isn\'t zero. Your opening balances don\'t fully balance yet. <a href="#/opening">Review opening balances</a></div>' : ''}
    <div class="report">${reportHead('Balance Sheet', `As at ${dfmt(d.dates[n - 1])}`)}<div class="table-wrap"><table><thead>${head}</thead><tbody>${rowsHtml}</tbody></table></div></div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#f-to').onchange = () => { RF.bsTo = val('#f-to'); reportBS(); };
  $('#f-count').onchange = () => { RF.bsCount = val('#f-count'); reportBS(); };
  $('#x-csv').onclick = () => {
    const out = [[S.settings.business_name], ['Balance Sheet'], [], ['Account', ...d.dates]];
    d.sections.forEach(s => { out.push([s.title]); s.rows.forEach(r => out.push([`${r.code} ${r.name}`.trim(), ...r.values.map(c2d)])); out.push([`Total ${s.title}`, ...s.totals.map(c2d)]); });
    out.push(['Net assets', ...d.net_assets.map(c2d)]);
    download(`Balance-Sheet-${RF.bsTo}.csv`, out);
  };
}

async function reportAged(kind) {
  const key = kind === 'sale' ? 'arAt' : 'apAt';
  RF[key] = RF[key] || S.today;
  const d = await GET(`/api/reports/aged?kind=${kind}&as_at=${RF[key]}`);
  const K = KIND[kind];
  const title = kind === 'sale' ? 'Aged Receivables' : 'Aged Payables';
  const B = ['current', 'd30', 'd60', 'd90', 'd90plus'];
  const head = `<tr><th>${K.who} / ${K.one.toLowerCase()}</th><th>Due</th><th class="num">Current</th><th class="num">1–30 days</th><th class="num">31–60</th><th class="num">61–90</th><th class="num">90+</th><th class="num">Total</th></tr>`;
  let rowsHtml = '';
  d.groups.forEach(g => {
    rowsHtml += `<tr class="sechead"><td colspan="2">${esc(g.contact_name)}</td>${B.map(b => `<td class="num">${fmt(g[b], { blankZero: true })}</td>`).join('')}<td class="num">${fmt(g.total)}</td></tr>`;
    g.invoices.forEach(i => { rowsHtml += `<tr class="sub click" data-href="${K.base}/${i.id}"><td>${esc(i.number || '—')} <span class="muted">${dfmt(i.date)}</span></td><td>${dfmt(i.due_date)}${i.days_overdue ? ` <span class="muted">(${i.days_overdue}d)</span>` : ''}</td>
      ${B.map(b => `<td class="num">${i.bucket === b ? fmt(i.balance) : ''}</td>`).join('')}<td></td></tr>`; });
  });
  rowsHtml += `<tr class="grand"><td colspan="2">Total</td>${B.map(b => `<td class="num">${fmt(d.totals[b])}</td>`).join('')}<td class="num">${fmt(d.totals.total)}</td></tr>`;
  const ties = d.control_balance === d.totals.total;
  page(title, '', `<button id="x-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button>`,
    `<div class="filters"><div class="field"><label>As at</label><input type="date" id="f-at" value="${RF[key]}"></div></div>
    <div class="notice ${ties ? 'good' : 'warn'} no-print">${ties ? `Ties to ${esc(d.control_name)} on the balance sheet (${$fmt(d.control_balance)}).`
      : `${esc(d.control_name)} on the balance sheet is ${$fmt(d.control_balance)}, which differs from this report by ${$fmt(d.control_balance - d.totals.total)}. Usually this means the opening balance doesn't match the unpaid items entered at your start date. <a href="#/opening">Check opening balances</a>`}</div>
    <div class="report">${reportHead(title, `As at ${dfmt(d.as_at)}`)}${d.groups.length ? `<div class="table-wrap"><table><thead>${head}</thead><tbody>${rowsHtml}</tbody></table></div>` : `<div class="empty">Nothing outstanding</div>`}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#f-at').onchange = () => { RF[key] = val('#f-at'); reportAged(kind); };
  $('#x-csv').onclick = () => {
    const out = [[S.settings.business_name], [`${title} as at ${d.as_at}`], [], [K.who, K.one, 'Date', 'Due', 'Days overdue', 'Balance', 'Bucket']];
    const lbl = { current: 'Current', d30: '1-30', d60: '31-60', d90: '61-90', d90plus: '90+' };
    d.groups.forEach(g => g.invoices.forEach(i => out.push([g.contact_name, i.number, i.date, i.due_date, i.days_overdue, c2d(i.balance), lbl[i.bucket]])));
    download(`${title.replace(' ', '-')}-${d.as_at}.csv`, out);
  };
}

async function reportTB() {
  RF.tbAt = RF.tbAt || S.today;
  const d = await GET(`/api/reports/tb?as_at=${RF.tbAt}`);
  page('Trial Balance', '', `<button id="x-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button>`,
    `<div class="filters"><div class="field"><label>As at</label><input type="date" id="f-at" value="${RF.tbAt}"></div></div>
    <div class="report">${reportHead('Trial Balance', `As at ${dfmt(d.as_at)}`)}<table><thead><tr><th>Code</th><th>Account</th><th>Type</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>
    ${d.rows.map(r => `<tr class="click" data-href="#/register/${r.account_id}?from=2000-01-01&to=${d.as_at}"><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td class="muted">${TYPE_LABEL[r.type]}</td><td class="num">${fmt(r.debit, { blankZero: true })}</td><td class="num">${fmt(r.credit, { blankZero: true })}</td></tr>`).join('')}
    <tr class="grand"><td colspan="3">Total</td><td class="num">${fmt(d.debit)}</td><td class="num">${fmt(d.credit)}</td></tr></tbody></table></div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#f-at').onchange = () => { RF.tbAt = val('#f-at'); reportTB(); };
  $('#x-csv').onclick = () => download(`Trial-Balance-${d.as_at}.csv`, [['Code', 'Account', 'Debit', 'Credit'], ...d.rows.map(r => [r.code, r.name, c2d(r.debit), c2d(r.credit)]), ['', 'Total', c2d(d.debit), c2d(d.credit)]]);
}

// ---------------------------------------------------------------- contacts
async function contactsList() {
  await refreshState();
  const f = QS.get('kind') || 'all';
  const list = S.contacts.filter(c => (f === 'all' || c.kind === f || c.kind === 'both') && (QS.get('archived') ? true : !c.archived));
  page('Contacts', '', `<a class="btn primary" href="#/contacts/new">New contact</a>`,
    `<div class="tabs">${[['all', 'All'], ['customer', 'Customers'], ['supplier', 'Suppliers']].map(([k, l]) => `<a href="#/contacts?kind=${k}" class="${k === f ? 'active' : ''}">${l}</a>`).join('')}</div>
    <div class="panel flush">${list.length ? `<table><thead><tr><th>Name</th><th>Type</th><th>Email</th><th>Phone</th><th class="num">Owes you</th><th class="num">You owe</th></tr></thead><tbody>
    ${list.map(c => `<tr class="click" data-href="#/contacts/${c.id}"><td>${esc(c.name)}${c.archived ? ' <span class="pill">archived</span>' : ''}</td><td>${c.kind[0].toUpperCase() + c.kind.slice(1)}</td><td>${esc(c.email || '')}</td><td>${esc(c.phone || '')}</td>
      <td class="num">${fmt(c.receivable, { blankZero: true })}</td><td class="num">${fmt(c.payable, { blankZero: true })}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No contacts yet</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

async function contactForm(id) {
  const c = id ? contactById(id) : null;
  if (id && !c) throw new Error('Contact not found');
  const [sales, bills] = c ? await Promise.all([GET(`/api/invoices?kind=sale&status=all&contact_id=${c.id}`), GET(`/api/invoices?kind=bill&status=all&contact_id=${c.id}`)]) : [[], []];
  const items = [...sales, ...bills].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 30);
  page(c ? esc(c.name) : 'New contact', '', `<a class="btn" href="#/contacts">Cancel</a>${c ? '<button class="danger" id="c-del">Delete</button>' : ''}<button class="primary" id="c-save">Save</button>`,
    `<div class="panel"><div class="form-grid">
      <div class="field"><label>Name</label><input id="c-name" value="${esc(c ? c.name : '')}"></div>
      <div class="field"><label>Type</label><select id="c-kind">${['customer', 'supplier', 'both'].map(k => `<option value="${k}" ${(c ? c.kind : (QS.get('kind') || 'customer')) === k ? 'selected' : ''}>${k[0].toUpperCase() + k.slice(1)}</option>`).join('')}</select></div>
      <div class="field"><label>Email</label><input id="c-email" value="${esc(c ? c.email || '' : '')}"></div>
      <div class="field"><label>Phone</label><input id="c-phone" value="${esc(c ? c.phone || '' : '')}"></div>
      <div class="field"><label>Payment terms (days)</label><input type="number" min="0" id="c-terms" value="${c ? c.terms_days : 14}"></div>
      <div class="field"><label>Default account</label><select id="c-acct">${acctOptions(c ? c.default_account_id : '', a => postable(a) && !isBank(a), 'None')}</select><div class="hint">Pre-fills new invoices, bills and spends</div></div>
    </div>
    <div class="grid g2"><div class="field"><label>Address</label><textarea id="c-addr">${esc(c ? c.address || '' : '')}</textarea></div>
    <div class="field"><label>Notes</label><textarea id="c-notes">${esc(c ? c.notes || '' : '')}</textarea></div></div>
    ${c ? `<label class="check"><input type="checkbox" id="c-arch" ${c.archived ? 'checked' : ''}> Archived (hidden from lists)</label>` : ''}</div>
    ${c ? `<div class="panel flush"><h2>Recent invoices &amp; bills</h2>${items.length ? `<table><tbody>${items.map(i => `<tr class="click" data-href="${KIND[i.kind].base}/${i.id}">
      <td>${KIND[i.kind].one} ${esc(i.number || '')}</td><td>${dfmt(i.date)}</td><td class="num">${fmt(i.total)}</td><td class="num">${i.balance ? fmt(i.balance) + ' owing' : ''}</td><td><span class="pill ${i.state}">${i.state}</span></td></tr>`).join('')}</tbody></table>` : '<div class="empty">None yet</div>'}</div>` : ''}`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#c-save').onclick = async () => {
    try {
      const b = { name: val('#c-name'), kind: val('#c-kind'), email: val('#c-email'), phone: val('#c-phone'), terms_days: val('#c-terms'), default_account_id: val('#c-acct') || null,
        address: val('#c-addr'), notes: val('#c-notes'), archived: c ? $('#c-arch').checked : false };
      if (c) await api('PUT', `/api/contacts/${c.id}`, b); else await api('POST', '/api/contacts', b);
      await refreshState(); toast('Saved'); go('#/contacts');
    } catch (e) { fail(e); }
  };
  if (c) $('#c-del').onclick = async () => {
    if (!await confirmBox(`Delete ${esc(c.name)}?`, 'Delete', true)) return;
    try { await api('DELETE', `/api/contacts/${c.id}`); await refreshState(); toast('Deleted'); go('#/contacts'); } catch (e) { fail(e); }
  };
}

// ---------------------------------------------------------------- accounts
async function accountsPage() {
  await refreshState();
  const showArch = QS.get('archived') === '1';
  let rowsHtml = '';
  TYPE_GROUPS.forEach(([t, label]) => {
    const list = S.accounts.filter(a => a.type === t && (showArch || !a.archived));
    if (!list.length) return;
    rowsHtml += `<tr class="sechead"><td colspan="4">${label}</td></tr>` + list.map(a => `<tr class="click sub" data-id="${a.id}"><td>${esc(a.code)}</td><td>${esc(a.name)}
      ${a.archived ? ' <span class="pill">archived</span>' : ''}</td><td class="muted">${a.subtype === 'bank' ? (a.type === 'asset' ? 'Bank account' : 'Credit card') : a.system ? 'System' : ''}</td>
      <td class="num">${a.used ? `<a href="#/register/${a.id}?from=2000-01-01" onclick="event.stopPropagation()">${fmt(a.balance)}</a>` : ''}</td></tr>`).join('');
  });
  page('Chart of accounts', '', `<a class="btn" href="#/accounts${showArch ? '' : '?archived=1'}">${showArch ? 'Hide' : 'Show'} archived</a><button class="primary" id="a-new">New account</button>`,
    `<div class="panel flush"><table><thead><tr><th style="width:110px">Code</th><th>Name</th><th>Kind</th><th class="num">Balance</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>`);
  $$('tr[data-id]').forEach(tr => tr.onclick = () => accountModal(acct(tr.dataset.id)));
  $('#a-new').onclick = () => accountModal(null);
}

function accountModal(a) {
  const sys = a && a.system;
  modal(`<h2>${a ? 'Edit account' : 'New account'}</h2>
    ${sys ? '<div class="notice info">This is a system account used by invoices, bills or reports. You can rename it, but not change its type.</div>' : ''}
    <div class="form-grid">
      <div class="field"><label>Code</label><input id="am-code" value="${esc(a ? a.code : '')}" placeholder="e.g. 6-1250"></div>
      <div class="field"><label>Name</label><input id="am-name" value="${esc(a ? a.name : '')}"></div>
      <div class="field"><label>Type</label><select id="am-type" ${sys ? 'disabled' : ''}>${TYPE_GROUPS.map(([t, l]) => `<option value="${t}" ${a && a.type === t ? 'selected' : ''}>${l.replace(/s$/, '').replace('Liabilitie', 'Liability').replace('Cost of Sale', 'Cost of Sales')}</option>`).join('')}</select></div>
      <div class="field"><label>Kind</label><select id="am-sub" ${sys ? 'disabled' : ''}><option value="other">Normal</option><option value="bank" ${a && a.subtype === 'bank' ? 'selected' : ''}>Bank / credit card (reconcilable)</option></select></div>
    </div>
    ${a && !sys ? `<label class="check"><input type="checkbox" id="am-arch" ${a.archived ? 'checked' : ''}> Archived</label>` : ''}
    <div class="modal-foot">${a && !sys && !a.used ? '<button class="danger" data-x="del" style="margin-right:auto">Delete</button>' : ''}<button data-x="no">Cancel</button><button class="primary" data-x="yes">Save</button></div>`, el => {
    el.querySelector('[data-x=no]').onclick = closeModal;
    const del = el.querySelector('[data-x=del]');
    if (del) del.onclick = async () => { try { await api('DELETE', `/api/accounts/${a.id}`); closeModal(); toast('Deleted'); accountsPage(); } catch (e) { fail(e); } };
    el.querySelector('[data-x=yes]').onclick = async () => {
      try {
        const b = { code: val('#am-code', el), name: val('#am-name', el), type: val('#am-type', el), subtype: val('#am-sub', el) };
        if (a && !sys) b.archived = el.querySelector('#am-arch').checked;
        if (a) await api('PUT', `/api/accounts/${a.id}`, b); else await api('POST', '/api/accounts', b);
        closeModal(); toast('Saved'); accountsPage();
      } catch (e) { fail(e); }
    };
  });
}

// ---------------------------------------------------------------- settings
async function settingsPage() {
  await refreshState();
  const st = S.settings;
  const lastMonthEnd = addDays(S.today.slice(0, 7) + '-01', -1);
  page('Settings', '', '', `
    <div class="panel"><h2>Business</h2><div class="form-grid">
      <div class="field"><label>Business name</label><input id="st-name" value="${esc(st.business_name)}"></div>
      <div class="field"><label>Financial year starts in</label><select id="st-fy">${[...Array(12)].map((_, i) => `<option value="${i + 1}" ${+st.fy_start_month === i + 1 ? 'selected' : ''}>${new Date(2000, i, 1).toLocaleDateString('en-AU', { month: 'long' })}</option>`).join('')}</select></div>
      <div class="field"><label>Next invoice number</label><input type="number" id="st-num" value="${esc(st.next_invoice_number)}"></div>
      <div class="field"><label>Start date</label><input type="date" value="${esc(st.conversion_date)}" disabled><div class="hint">Set during setup</div></div>
    </div><button class="primary" id="st-save">Save</button></div>
    <div class="panel"><h2>Lock period</h2>
      <p class="muted" style="margin-top:0">After you've reported on a month, lock it so nothing dated on or before that day can be added, changed or deleted by accident. Your reports for locked months won't change.</p>
      <div class="filters"><div class="field"><label>Lock transactions up to and including</label><input type="date" id="st-lock" value="${esc(st.lock_date)}"></div>
      <button id="st-lock-save">Update lock</button><button id="st-lock-last">Lock to ${dfmt(lastMonthEnd)}</button></div></div>
    <div class="panel"><h2>Backups</h2>
      <p class="muted" style="margin-top:0">Ledger keeps all your data in one file (<code>data\\books.db</code> inside the Ledger folder) and copies it to <code>data\\backups</code> every time it starts, keeping the latest 40.
      For safety, also copy the <code>data</code> folder to OneDrive, a USB drive or similar from time to time.</p>
      <button id="st-backup">Back up now</button></div>
    <div class="panel"><h2>More</h2><div class="actions"><a class="btn" href="#/opening">Opening balances</a><a class="btn" href="#/accounts">Chart of accounts</a><a class="btn" href="#/audit">Audit log</a><a class="btn" href="#/setup/4">Unpaid items at start</a></div></div>`);
  $('#st-save').onclick = async () => {
    try { await api('PUT', '/api/settings', { business_name: val('#st-name'), fy_start_month: val('#st-fy'), next_invoice_number: val('#st-num') }); await refreshState(); toast('Saved'); } catch (e) { fail(e); }
  };
  const lock = async d => { try { await api('PUT', '/api/settings', { lock_date: d }); await refreshState(); toast(d ? `Locked up to ${dfmt(d)}` : 'Lock removed'); settingsPage(); } catch (e) { fail(e); } };
  $('#st-lock-save').onclick = () => lock(val('#st-lock'));
  $('#st-lock-last').onclick = () => lock(lastMonthEnd);
  $('#st-backup').onclick = async () => { try { const r = await api('POST', '/api/backup'); toast('Backup saved: ' + r.file.split(/[\\/]/).pop()); } catch (e) { fail(e); } };
}

async function auditPage() {
  const list = await GET('/api/audit');
  page('Audit log', 'The last 300 changes made in Ledger', `<button onclick="history.back()">Back</button>`,
    `<div class="panel flush"><table><thead><tr><th>When</th><th>Action</th><th>Detail</th></tr></thead><tbody>
    ${list.map(a => `<tr><td class="muted" style="white-space:nowrap">${esc(a.ts.replace('T', ' '))}</td><td>${esc(a.action.replace(/_/g, ' '))}</td>
      <td class="muted" style="font-family:var(--mono);font-size:12px;word-break:break-all">${esc((a.detail || '').slice(0, 220))}</td></tr>`).join('')}</tbody></table></div>`);
}

// ---------------------------------------------------------------- boot
(async function boot() {
  try {
    await refreshState();
    if (S.settings.setup_done !== '1' && !location.hash) location.hash = '#/setup';
    router();
  } catch (e) {
    main.innerHTML = `<div class="notice bad">Couldn't connect to Ledger. Make sure the Ledger window is still open, then refresh this page.<br><small>${esc(e.message)}</small></div>`;
  }
})();
