'use strict';
/* numbr front end - plain JavaScript, no build step. */

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
  if (opt.dashZero && c === 0) return '–';
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
const acctLabel = id => { const a = acct(id); return a ? a.name : ''; };
// Accounts sort by name, ignoring case; numbers inside names sort naturally (Bank 2 before Bank 10).
const byName = (a, b) => a.name.localeCompare(b.name, 'en-AU', { sensitivity: 'base', numeric: true });

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
// Shared bank accounts: share_bp is the user's share in basis points (10000 = 100%).
const isShared = a => isBank(a) && a.share_bp < 10000;
const pctText = bp => `${+(bp / 100).toFixed(2)}%`;
const parsePct = (v, name) => {
  const t = String(v).trim().replace(/%$/, '');
  const n = t === '' ? 100 : Number(t);
  if (isNaN(n) || n <= 0 || n > 100) throw new Error(`${name}: your share must be more than 0% and no more than 100%`);
  return n;
};
// cents * bp / 10000, rounded half away from zero (matches cut() in app.py)
const cutBp = (c, bp) => Math.sign(c) * Math.round(Math.abs(c) * bp / 10000);
const postable = a => !['ar', 'ap'].includes(a.subtype);

// Options for an account chooser: grouped by type (Assets, Liabilities, …), then A-Z by name.
// Put them in a <select class="acct-pick"> to get the type-to-search picker.
function acctOptions(sel, filter = () => true, blank = 'Choose account…') {
  let h = blank !== null ? `<option value="">${esc(blank)}</option>` : '';
  for (const [t, label] of TYPE_GROUPS) {
    const list = S.accounts.filter(a => a.type === t && (!a.archived || a.id == sel) && filter(a)).sort(byName);
    if (!list.length) continue;
    h += `<optgroup label="${label}">` + list.map(a =>
      `<option value="${a.id}" ${a.id == sel ? 'selected' : ''}>${esc(a.name)}</option>`).join('') + '</optgroup>';
  }
  return h;
}
function bankOptions(sel, blank = null) {
  const list = S.accounts.filter(a => isBank(a) && (!a.archived || a.id == sel)).sort(byName);
  return (blank ? `<option value="">${esc(blank)}</option>` : '') +
    list.map(a => `<option value="${a.id}" ${a.id == sel ? 'selected' : ''}>${esc(a.name)}</option>`).join('');
}
// Create a bank account (type 'asset') or credit card (type 'liability') with a default name. The server picks the code.
async function createBankAccount(type, name) {
  if (!name) {
    const base = type === 'asset' ? 'Bank' : 'Credit Card';
    let k = S.accounts.filter(a => isBank(a) && a.type === type).length + 1;
    while (S.accounts.some(a => a.name.toLowerCase() === `${base} ${k}`.toLowerCase())) k++;
    name = `${base} ${k}`;
  }
  return api('POST', '/api/accounts', { name, type, subtype: 'bank' });
}
const firstBank = () => (S.accounts.find(a => isBank(a) && !a.archived && a.type === 'asset') || S.accounts.find(a => isBank(a) && !a.archived) || {}).id;
function contactOptions(sel, kind, blank = 'Choose…') {
  const ok = c => (!c.archived || c.id == sel) && (!kind || c.kind === kind || c.kind === 'both');
  return `<option value="">${esc(blank)}</option>` + S.contacts.filter(ok).map(c =>
    `<option value="${c.id}" ${c.id == sel ? 'selected' : ''}>${esc(c.name)}</option>`).join('') +
    `<option value="__new">+ Add new contact…</option>`;
}

// ---------------------------------------------------------------- account names and the type-to-search picker
// Names compare ignoring case and extra spaces (matches name_key() in app.py).
const nameKey = s => String(s || '').trim().split(/\s+/).join(' ').toLowerCase();
const searchWords = q => nameKey(q).split(' ').filter(Boolean);
const wordsMatch = (words, text) => { const t = nameKey(text); return words.every(w => t.includes(w)); };
// Names used by more than one account (only possible in books made before names had to be unique).
function duplicateAccountNames() {
  const seen = new Map();
  S.accounts.forEach(a => { const k = nameKey(a.name); seen.set(k, [...(seen.get(k) || []), a.name]); });
  return [...seen.values()].filter(v => v.length > 1).map(v => v[0]);
}
// Escape text and wrap each typed word in <mark>.
function highlight(text, words) {
  if (!words.length) return esc(text);
  const rx = new RegExp(`(${words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
  return String(text).split(rx).map((p, i) => (i % 2 ? `<mark>${esc(p)}</mark>` : esc(p))).join('');
}
// Turns a <select class="acct-pick"> into a text box with a filtered list. The select stays in the page
// (invisible, underneath) and holds the value, so existing .value reads, onchange handlers and tests work.
function enhanceAcctPick(sel) {
  if (sel.classList.contains('ap-done')) return;
  sel.classList.add('ap-done'); sel.tabIndex = -1;
  const wrap = document.createElement('span'); wrap.className = 'acct-pick-wrap';
  sel.parentNode.insertBefore(wrap, sel); wrap.appendChild(sel);
  const input = document.createElement('input');
  Object.assign(input, { type: 'text', autocomplete: 'off', spellcheck: false, className: 'acct-pick-input' });
  input.setAttribute('role', 'combobox'); input.setAttribute('aria-expanded', 'false');
  const list = document.createElement('div'); list.className = 'acct-pick-list'; list.hidden = true; list.setAttribute('role', 'listbox');
  wrap.append(input, list);
  let items = [], active = -1, open = false;
  const selectedText = () => { const o = sel.selectedOptions[0]; return o && o.value ? o.textContent : ''; };
  const blank = () => sel.querySelector('option[value=""]');
  const sync = () => {
    if (!open) input.value = selectedText();
    input.placeholder = blank() ? blank().textContent : 'Choose account…';
    input.disabled = sel.disabled;
  };
  const mark = () => {
    list.querySelectorAll('.ap-item').forEach(d => d.classList.toggle('active', +d.dataset.k === active));
    const a = list.querySelector('.ap-item.active'); if (a) a.scrollIntoView({ block: 'nearest' });
  };
  const draw = q => {
    const words = searchWords(q);
    let h = ''; items = [];
    const item = (o, cls = '') => { h += `<div class="ap-item${cls}" role="option" data-k="${items.length}">${highlight(o.textContent, words)}</div>`; items.push(o.value); };
    if (!words.length && blank()) item(blank(), ' ap-blank');
    sel.querySelectorAll('optgroup').forEach(og => {
      // Each word must be in the name, or start the type name (3+ letters), so "exp tele" finds Telephone under Expenses.
      const type = nameKey(og.label);
      const opts = [...og.querySelectorAll('option')].filter(o => {
        const n = nameKey(o.textContent);
        return words.every(w => n.includes(w) || (w.length >= 3 && type.startsWith(w)));
      });
      if (!opts.length) return;
      h += `<div class="ap-head">${esc(og.label)}</div>`;
      opts.forEach(o => item(o));
    });
    list.innerHTML = h || '<div class="ap-none">No accounts match. Try fewer letters.</div>';
    active = !items.length ? -1 : words.length ? 0 : Math.max(0, items.indexOf(sel.value));
    place(); mark();
  };
  // The list is position:fixed so scrolling tables and modals don't clip it; place it under (or above) the box.
  const place = () => {
    const r = input.getBoundingClientRect(), below = window.innerHeight - r.bottom - 8;
    const h = Math.min(300, Math.max(below, r.top - 8));
    list.style.left = `${r.left}px`; list.style.width = `${Math.max(r.width, 240)}px`; list.style.maxHeight = `${h}px`;
    list.style.top = below >= Math.min(300, list.scrollHeight) || below >= r.top ? `${r.bottom + 3}px` : `${Math.max(8, r.top - 3 - Math.min(h, list.scrollHeight))}px`;
  };
  const onScroll = e => { if (!list.contains(e.target)) place(); };
  const show = () => {
    if (open || sel.disabled) return;
    open = true; list.hidden = false; input.setAttribute('aria-expanded', 'true'); draw('');
    window.addEventListener('scroll', onScroll, true); window.addEventListener('resize', place);
  };
  const hide = () => {
    open = false; list.hidden = true; input.setAttribute('aria-expanded', 'false'); sync();
    window.removeEventListener('scroll', onScroll, true); window.removeEventListener('resize', place);
  };
  const choose = v => {
    const changed = sel.value !== v;
    open = false; sel.value = v; hide();
    if (changed) sel.dispatchEvent(new Event('change', { bubbles: true }));
  };
  // Keep the text right when code sets select.value or rebuilds its options.
  const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  Object.defineProperty(sel, 'value', { configurable: true, get() { return desc.get.call(this); }, set(v) { desc.set.call(this, v); sync(); } });
  new MutationObserver(sync).observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
  sel.addEventListener('change', sync);
  input.addEventListener('mousedown', () => { if (document.activeElement === input) show(); });
  input.addEventListener('click', () => { show(); input.select(); });
  input.addEventListener('input', () => { show(); draw(input.value); });
  input.addEventListener('blur', hide);
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { show(); return; }
      if (items.length) active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      mark();
    } else if (e.key === 'Enter') {
      if (open) { e.preventDefault(); if (active >= 0) choose(items[active]); }
    } else if (e.key === 'Escape') {
      if (open) { e.stopPropagation(); hide(); }
    } else if (e.key === 'Tab') {
      if (open && input.value !== selectedText() && active >= 0) choose(items[active]);
    }
  });
  list.addEventListener('mousedown', e => {
    e.preventDefault();  // keep focus in the text box
    const d = e.target.closest('.ap-item'); if (d) choose(items[+d.dataset.k]);
  });
  sync();
}
new MutationObserver(() => document.querySelectorAll('select.acct-pick:not(.ap-done)').forEach(enhanceAcctPick))
  .observe(document.body, { childList: true, subtree: true });

// numbr was updated while an older copy was still running: the old server ignores new fields, so changes
// (like a bank account's share) wouldn't save. Tell the user how to restart.
function showStaleServer(stale) {
  let el = document.getElementById('stale-server');
  if (!stale) { if (el) el.remove(); return; }
  if (el) return;
  el = document.createElement('div');
  el.id = 'stale-server';
  el.className = 'notice bad';
  el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:50;margin:0;box-shadow:0 4px 16px rgba(0,0,0,.15)';
  el.innerHTML = '<b>numbr has been updated but the old version is still running.</b> Some changes won\'t save until you restart it. Close the numbr window, then double-click start.bat again.';
  document.body.appendChild(el);
}
async function refreshState() {
  const st = await GET('/api/state');
  Object.assign(S, st);
  showStaleServer(st.server_current !== true);
  document.getElementById('bizname').textContent = S.settings.business_name || '';
  document.title = S.settings.business_name ? `${S.settings.business_name} – numbr` : 'numbr';
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
</div>
      <div class="modal-foot"><button data-x="no">Cancel</button><button class="primary" data-x="yes">Add contact</button></div>`, el => {
      el.querySelector('[data-x=no]').onclick = () => { closeModal(); resolve(null); };
      el.querySelector('[data-x=yes]').onclick = async () => {
        try {
          const r = await api('POST', '/api/contacts', { name: val('#qc-name', el), kind: val('#qc-kind', el) });
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
        <td><select class="acct-pick" data-f="account_id">${acctOptions(r.account_id, filter)}</select></td>
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
  [/^#\/partner$/, () => invoiceList('partner')],
  [/^#\/partner\/new$/, () => invoiceForm('partner')],
  [/^#\/partner\/receive$/, () => paymentForm('contribution')],
  [/^#\/partner\/(\d+)$/, m => invoiceView('partner', m[1])],
  [/^#\/partner\/(\d+)\/edit$/, m => invoiceForm('partner', m[1])],
  [/^#\/receive$/, () => paymentForm('receipt')],
  [/^#\/pay$/, () => paymentForm('payment')],
  [/^#\/payment\/(\d+)$/, m => paymentView(m[1])],
  [/^#\/banking$/, bankingHome],
  [/^#\/banking\/(spend|receive|transfer)$/, m => txnForm(m[1])],
  [/^#\/txn\/(\d+)$/, m => txnForm(null, m[1])],
  [/^#\/reconcile$/, reconcilePage],
  [/^#\/rules$/, rulesList],
  [/^#\/rules\/new$/, () => ruleForm()],
  [/^#\/rules\/(\d+)$/, m => ruleForm(m[1])],
  [/^#\/recurring$/, recurringList],
  [/^#\/recurring\/new$/, () => recurringForm()],
  [/^#\/recurring\/(\d+)$/, m => recurringForm(m[1])],
  [/^#\/reports\/pl$/, reportPL],
  [/^#\/reports\/bs$/, reportBS],
  [/^#\/reports\/aged-ar$/, () => reportAged('sale')],
  [/^#\/reports\/aged-ap$/, () => reportAged('bill')],
  [/^#\/reports\/tb$/, reportTB],
  [/^#\/budgets$/, budgetList],
  [/^#\/budgets\/new$/, budgetBuilder],
  [/^#\/budgets\/(\d+)$/, m => budgetView(m[1])],
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
      ${b.share_bp < 10000 ? `<div class="note">Your share (${pctText(b.share_bp)}): <b>${$fmt(b.my_balance)}</b></div>` : ''}
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
      ${d.contributions.count ? `<div class="stat"><div class="label">Partner contributions due</div><a class="value" href="#/partner">${$fmt(d.contributions.total)}</a>
        <div class="note">${d.contributions.count} open${d.contributions.overdue ? ` · <span class="neg">${$fmt(d.contributions.overdue)} overdue</span>` : ''}</div></div>` : ''}
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
    main.innerHTML = `<div class="setup">${steps}<div class="panel"><h1 style="margin-top:0">Welcome to numbr</h1>
      <p class="muted">A few details to get your books started. You can change these later in Settings.</p>
      <div class="field"><label>Business name</label><input id="s-name" value="${esc(S.settings.business_name)}"></div>
      <div class="form-grid">
        <div class="field"><label>Start date</label><input id="s-conv" type="date" value="${esc(conv || defConv)}">
          <div class="hint">The first day you'll record transactions in numbr. Your opening balances are as at the day before.
          The start of a month (or financial year) is easiest.</div></div>
        <div class="field"><label>Financial year starts in</label><select id="s-fy">${[...Array(12)].map((_, i) =>
          `<option value="${i + 1}" ${+S.settings.fy_start_month === i + 1 ? 'selected' : ''}>${new Date(2000, i, 1).toLocaleDateString('en-AU', { month: 'long' })}</option>`).join('')}</select></div>
        <div class="field"><label>Next invoice number</label><input id="s-num" type="number" value="${esc(S.settings.next_invoice_number)}"></div>
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
      <table><thead><tr><th>Name</th><th>Kind</th><th></th></tr></thead><tbody>
      ${banks.map(a => `<tr data-id="${a.id}"><td><input class="b-name" value="${esc(a.name)}"></td>
        <td>${a.type === 'asset' ? 'Bank account' : 'Credit card'}</td><td>${a.used ? '' : `<button class="small danger b-del">Remove</button>`}</td></tr>`).join('')}
      </tbody></table>
      <div class="actions" style="margin-top:10px"><button class="small" id="b-add-bank">+ Bank account</button><button class="small" id="b-add-cc">+ Credit card</button></div>
      <div class="modal-foot"><button onclick="go('#/setup/1')">Back</button><button class="primary" id="s-next">Save &amp; continue</button></div></div></div>`;
    const saveAll = async () => {
      for (const tr of $$('tr[data-id]')) {
        const a = acct(tr.dataset.id);
        const name = tr.querySelector('.b-name').value;
        if (name !== a.name) await api('PUT', `/api/accounts/${a.id}`, { name });
      }
    };
    const add = async type => {
      try {
        await saveAll(); await refreshState();
        await createBankAccount(type);
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
    `<tr class="sechead"><td colspan="2">${label}</td></tr>` + accts.filter(a => a.type === t).sort(byName).map(a =>
      `<tr class="sub"><td>${esc(a.name)}${a.subtype === 'ar' ? ' <span class="muted">(total of unpaid invoices)</span>' : ''}${a.subtype === 'ap' ? ' <span class="muted">(total of unpaid bills)</span>' : ''}</td>
       <td style="width:180px"><input class="num ob" data-id="${a.id}" value="${vals[a.id] ? fmt(vals[a.id]) : ''}" placeholder="0.00" ${locked ? 'disabled' : ''}></td></tr>`).join('')).join('');
  const body = `${locked ? `<div class="notice info">Opening balances are locked because you've reconciled a bank account since. Undo reconciliations to change them.</div>` : ''}
    <div class="panel"><p class="muted" style="margin-top:0">Enter each balance as at close of business on <b>${dfmt(addDays(conv, -1))}</b>, taken from your previous balance sheet at that date.
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
  // A partner contribution is money a partner is due to put into an account you track (e.g. a joint account).
  // It's stored as a 'sale' with partner=1, posts nothing until paid, and is paid into "Other owners' share".
  partner: { base: '#/partner', title: 'Partner contributions', one: 'Contribution', who: 'Partner', ckind: null, pay: '#/partner/receive', payLabel: 'Mark paid' },
};
const kindOf = inv => inv.partner ? 'partner' : inv.kind;
const PAY_KIND = { sale: 'receipt', bill: 'payment', partner: 'contribution' };
const PARTNER_NOTE = `A partner contribution is a reminder of money a partner is due to put in. It doesn't touch your Profit &amp; Loss or Balance Sheet until it's paid, and when it's paid the money counts as the other owners' share, not yours.`;

async function invoiceList(kind) {
  const K = KIND[kind];
  const status = QS.get('status') || 'open';
  const list = await GET(`/api/invoices?kind=${kind}&status=${status}`);
  const tabs = ['open', 'overdue', 'paid', 'void', 'all'].map(s => `<a href="${K.base}?status=${s}" class="${s === status ? 'active' : ''}">${s[0].toUpperCase() + s.slice(1)}</a>`).join('');
  const totBal = list.reduce((s, i) => s + i.balance, 0), tot = list.reduce((s, i) => s + (i.status === 'void' ? 0 : i.total), 0);
  page(K.title, kind === 'sale' ? 'Customer invoices and payments' : kind === 'bill' ? 'Supplier bills and payments' : 'Money partners are due to put into shared accounts',
    `<a class="btn" href="#/payments?kind=${PAY_KIND[kind]}" id="pay-hist">Payment history</a><a class="btn" href="${K.pay}">${K.payLabel}</a><a class="btn primary" href="${K.base}/new">New ${K.one.toLowerCase()}</a>`,
    `<div class="tabs">${tabs}</div><div class="panel flush">${list.length ? `<div class="table-wrap"><table><thead><tr>
      <th>${kind === 'sale' ? 'Number' : 'Ref'}</th><th>Date</th><th>${K.who}</th><th>Due</th><th class="num">Total</th><th class="num">Balance</th><th>Status</th></tr></thead><tbody>
      ${list.map(i => `<tr class="click" data-href="${K.base}/${i.id}"><td>${esc(i.number || '—')}</td><td>${dfmt(i.date)}</td><td>${esc(i.contact_name)}</td>
        <td>${dfmt(i.due_date)}</td><td class="num">${fmt(i.total)}</td><td class="num">${fmt(i.balance)}</td>
        <td><span class="pill ${i.state}">${i.state}</span>${i.historical ? ' <span class="pill" title="Dated before your start date">opening</span>' : ''}${i.recurring_id ? ' <span class="pill" title="Created by a recurring transaction">↻</span>' : ''}${i.share_bp < 10000 ? ` <span class="pill" title="Shared: your share">${pctText(i.share_bp)}</span>` : ''}</td></tr>`).join('')}
      <tr class="total"><td colspan="4">${list.length} item${list.length === 1 ? '' : 's'}</td><td class="num">${fmt(tot)}</td><td class="num">${fmt(totBal)}</td><td></td></tr>
      </tbody></table></div>` : `<div class="empty">No ${status === 'all' ? '' : status + ' '}${K.one.toLowerCase()}s</div>`}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#pay-hist').onclick = e => { e.preventDefault(); paymentHistory(PAY_KIND[kind]); };
}

async function paymentHistory(pkind) {
  const list = await GET(`/api/payments?kind=${pkind}`);
  const kind = pkind === 'receipt' ? 'sale' : pkind === 'contribution' ? 'partner' : 'bill';
  page(pkind === 'receipt' ? 'Customer payments' : pkind === 'contribution' ? 'Partner contributions received' : 'Supplier payments', '', `<a class="btn" href="${KIND[kind].base}">Back to ${KIND[kind].title.toLowerCase()}</a>`,
    `<div class="panel flush">${list.length ? `<table><thead><tr><th>Date</th><th>${KIND[kind].who}</th><th>Account</th><th>Reference</th><th class="num">Amount</th><th></th></tr></thead><tbody>
    ${list.map(p => `<tr class="click" data-href="#/payment/${p.id}"><td>${dfmt(p.date)}</td><td>${esc(p.contact_name)}</td><td>${esc(p.bank_name)}</td><td>${esc(p.reference || '')}</td>
      <td class="num">${fmt(p.amount)}</td><td>${p.reconciled ? '<span class="pill ok">Reconciled</span>' : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No payments yet</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

async function invoiceView(kind, id) {
  const K = KIND[kind];
  const inv = await GET(`/api/invoices/${id}`);
  if (kindOf(inv) !== kind) { go(`${KIND[kindOf(inv)].base}/${inv.id}`); return; }
  const state = inv.status === 'void' ? 'void' : inv.balance === 0 ? 'paid' : inv.due_date < S.today ? 'overdue' : 'open';
  const canEdit = inv.status === 'open' && !inv.payments.length;
  page(`${K.one} ${esc(inv.number || '')}`, `${esc(inv.contact_name)} · <span class="pill ${state}">${state}</span>${inv.historical ? ' <span class="pill">opening item</span>' : ''}`,
    `<a class="btn" href="${K.base}">Back</a>${canEdit ? `<button class="danger" id="iv-void">Void</button><a class="btn" href="${K.base}/${inv.id}/edit">Edit</a>` : ''}
     ${kind === 'sale' && inv.status === 'open' && !inv.historical ? '<button id="iv-partner" title="Treat this as money a partner put into a shared account, not income">Convert to partner contribution</button>' : ''}
     <button onclick="window.print()">Print</button>${inv.balance > 0 ? `<a class="btn primary" href="${K.pay}?contact=${inv.contact_id}&inv=${inv.id}">${kind === 'sale' ? 'Receive payment' : kind === 'partner' ? 'Mark paid' : 'Pay bill'}</a>` : ''}`,
    `${kind === 'partner' ? `<div class="notice info no-print">${PARTNER_NOTE}</div>` : ''}
    ${inv.historical ? `<div class="notice info no-print">This is dated before your start date, so it's an opening item: it doesn't post to the ledger (your opening balance already includes it).</div>` : ''}
    ${inv.share_bp < 10000 ? `<div class="notice info no-print">Shared ${K.one.toLowerCase()}: ${pctText(inv.share_bp)} is yours, so your reports show ${$fmt(cutBp(inv.total, inv.share_bp))} of the ${$fmt(inv.total)}. The full amount is still ${kind === 'sale' ? 'owed to you' : 'owed by you'}.</div>` : ''}
    <div class="report">
      <div style="display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:22px">
        <div><div style="font-size:20px;font-weight:700">${esc(S.settings.business_name)}</div>
          <div style="font-size:26px;font-weight:300;margin-top:6px;letter-spacing:.04em">${kind === 'sale' ? 'INVOICE' : kind === 'partner' ? 'CONTRIBUTION' : 'BILL'}</div></div>
        <div style="text-align:right"><div><span class="muted">${kind === 'sale' ? 'Invoice no.' : kind === 'partner' ? 'Reference' : 'Supplier ref'}</span> <b>${esc(inv.number || '—')}</b></div>
          <div><span class="muted">Date</span> ${dfmt(inv.date)}</div><div><span class="muted">Due</span> ${dfmt(inv.due_date)}</div></div>
      </div>
      <div style="margin-bottom:18px"><div class="muted" style="font-size:12px;text-transform:uppercase">${kind === 'sale' ? 'Bill to' : kind === 'partner' ? 'Partner' : 'From'}</div>
        <div style="font-weight:600">${esc(inv.contact_name)}</div><div style="white-space:pre-line">${esc(inv.contact_address || '')}</div></div>
      <table><thead><tr><th>Description</th><th class="no-print">Account</th><th class="num">Amount</th></tr></thead><tbody>
        ${inv.lines.map(l => `<tr><td>${esc(l.description || '')}</td><td class="no-print muted">${esc(l.account_name)}</td><td class="num">${fmt(l.amount)}</td></tr>`).join('')}
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
  const cv = $('#iv-partner');
  if (cv) cv.onclick = async () => {
    if (!await confirmBox(`Convert invoice ${esc(inv.number || '')} to a partner contribution?<br><br>It will no longer be income or money owed to you. ` +
      `${inv.payments.length ? 'Its payments will count as the other owners\' share of the bank account instead. ' : ''}Bank reconciliations aren't affected.`, 'Convert')) return;
    try { await api('POST', `/api/invoices/${inv.id}/to-partner`); await refreshState(); toast('Converted to a partner contribution'); go(`#/partner/${inv.id}`); } catch (e) { fail(e); }
  };
}

async function invoiceForm(kind, id) {
  const K = KIND[kind];
  const inv = id ? await GET(`/api/invoices/${id}`) : null;
  if (inv && (inv.status !== 'open' || inv.payments.length || kindOf(inv) !== kind)) { go(`${KIND[kindOf(inv)].base}/${id}`); return; }
  const conv = S.settings.conversion_date;
  const hist = QS.get('hist') === '1';
  const defDate = inv ? inv.date : (hist && conv ? addDays(conv, -1) : S.today);
  const isP = kind === 'partner';
  page(inv ? `Edit ${K.one.toLowerCase()} ${esc(inv.number || '')}` : `New ${isP ? 'partner contribution' : K.one.toLowerCase()}`, hist && !isP ? 'Unpaid at your start date (opening item)' : '',
    `<a class="btn" href="${inv ? `${K.base}/${inv.id}` : K.base}">Cancel</a><button class="primary" id="f-save">Save</button>`,
    `<div class="panel">${isP ? `<div class="notice info">${PARTNER_NOTE}</div>` : ''}<div class="form-grid">
      <div class="field"><label>${K.who}</label><select id="f-contact"></select></div>
      <div class="field"><label>${kind === 'sale' ? 'Invoice number' : isP ? 'Reference' : 'Supplier reference'}</label><input id="f-number" value="${esc(inv ? inv.number : '')}" placeholder="${kind === 'sale' ? `Auto (${esc(S.settings.next_invoice_number)})` : 'Optional'}"></div>
      <div class="field"><label>Date</label><input id="f-date" type="date" value="${defDate}"></div>
      <div class="field"><label>Due date</label><input id="f-due" type="date" value="${inv ? inv.due_date : ''}"></div>
      ${isP ? `<div class="field"><label>Amount</label><input id="f-amount" class="num" inputmode="decimal" placeholder="0.00" value="${inv ? fmt(inv.total) : ''}"></div>
      <div class="field"><label>Description</label><input id="f-desc" value="${esc(inv && inv.lines[0] ? inv.lines[0].description || '' : '')}" placeholder="e.g. Weekly shared expenses"></div>`
      : `<div class="field"><label>Your share %</label><input id="f-share" inputmode="decimal" value="${inv ? +(inv.share_bp / 100).toFixed(2) : 100}">
        <div class="hint">Less than 100 if it's shared, e.g. 50 for rent split two ways. Reports show only your share.</div></div>`}
    </div>
    <div id="f-hist" class="notice info" hidden>Dated before your start date (${dfmt(conv)}): this is an opening item and won't post to the ledger.</div>
    <div id="f-lines"></div>
    <div class="field" style="margin-top:14px"><label>Notes${kind === 'sale' ? ' (shown on the invoice)' : ''}</label><textarea id="f-memo">${esc(inv ? inv.memo : '')}</textarea></div>
    </div>`);
  const filt = a => postable(a) && !isBank(a);
  const ed = isP ? null : lineEditor($('#f-lines'), { lines: inv ? inv.lines : [], filter: filt });
  const sel = $('#f-contact');
  sel.innerHTML = contactOptions(inv ? inv.contact_id : (QS.get('contact') || ''), K.ckind);
  let dueTouched = !!inv;
  const updDue = () => {
    const d = val('#f-date');
    if (d && !dueTouched) $('#f-due').value = d;
    $('#f-hist').hidden = isP || !(conv && d && d < conv);
  };
  wireContactSelect(sel, K.ckind, () => { const c = contactById(sel.value); if (c && ed) ed.setDefaultAccount(c.default_account_id); updDue(); });
  $('#f-date').onchange = updDue;
  $('#f-due').onchange = () => { dueTouched = true; };
  updDue();
  if (!inv && !QS.get('contact')) sel.focus();
  $('#f-save').onclick = async () => {
    try {
      const body = { kind, contact_id: sel.value, number: val('#f-number'), date: val('#f-date'), due_date: val('#f-due'), memo: val('#f-memo') };
      if (isP) {
        const amt = parseMoney(val('#f-amount'));
        if (isNaN(amt) || amt <= 0) throw new Error('Enter the amount the partner is due to pay');
        Object.assign(body, { amount: amt, description: val('#f-desc') });
      } else Object.assign(body, { lines: ed.get(), share_pct: parsePct(val('#f-share'), K.one) });
      const r = inv ? await api('PUT', `/api/invoices/${inv.id}`, body) : await api('POST', '/api/invoices', body);
      await refreshState(); toast('Saved'); afterSave(`${K.base}/${r.id}`);
    } catch (e) { fail(e); }
  };
}

async function paymentForm(pkind) {
  const kind = pkind === 'receipt' ? 'sale' : pkind === 'contribution' ? 'partner' : 'bill', K = KIND[kind];
  if (pkind === 'contribution') pkind = 'receipt';
  page(kind === 'partner' ? 'Partner contribution received' : pkind === 'receipt' ? 'Receive payment' : 'Pay bills',
    kind === 'partner' ? 'Mark contributions paid when the money arrives in the bank' : pkind === 'receipt' ? 'Record money received from a customer' : 'Record money paid to a supplier',
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
  // The total row only exists once a contact with something outstanding is chosen.
  const showTotal = () => { const t = $('#p-total'); if (t) t.textContent = $fmt(total()); };
  const renderInvs = () => {
    if (!sel.value) { $('#p-invs').innerHTML = `<div class="empty">Choose a ${K.who.toLowerCase()} to see what's outstanding</div>`; return; }
    if (!invs.length) { $('#p-invs').innerHTML = `<div class="empty">Nothing outstanding for this ${K.who.toLowerCase()}</div>`; return; }
    $('#p-invs').innerHTML = `<table><thead><tr><th>${K.one}</th><th>Date</th><th>Due</th><th class="num">Total</th><th class="num">Balance</th><th class="num" style="width:160px">Payment</th></tr></thead><tbody>
      ${invs.map(i => `<tr><td><a href="${K.base}/${i.id}">${esc(i.number || '—')}</a></td><td>${dfmt(i.date)}</td><td>${dfmt(i.due_date)}${i.state === 'overdue' ? ' <span class="pill overdue">overdue</span>' : ''}</td>
        <td class="num">${fmt(i.total)}</td><td class="num">${fmt(i.balance)}</td>
        <td><div class="inline"><input class="num p-alloc" data-id="${i.id}" data-bal="${i.balance}" placeholder="0.00"><button class="small p-full" title="Pay in full">Full</button></div></td></tr>`).join('')}
      <tr class="total"><td colspan="5">Total payment</td><td class="num" id="p-total">$0.00</td></tr></tbody></table>`;
    $$('.p-alloc').forEach(i => { i.oninput = () => { showTotal(); }; i.onblur = () => { const v = parseMoney(i.value); if (!isNaN(v) && i.value) i.value = fmt(v); }; });
    $$('.p-full').forEach(b => b.onclick = () => { const i = b.parentElement.querySelector('input'); i.value = fmt(+i.dataset.bal); showTotal(); });
  };
  const autoAllocate = () => {
    let rem = parseMoney(val('#p-amount'));
    if (isNaN(rem) || !val('#p-amount')) return;
    const ordered = [...$$('.p-alloc')].sort((a, b) => invs.find(i => i.id == a.dataset.id).due_date.localeCompare(invs.find(i => i.id == b.dataset.id).due_date));
    ordered.forEach(i => { const amt = Math.min(rem, +i.dataset.bal); i.value = amt > 0 ? fmt(amt) : ''; rem -= Math.max(amt, 0); });
    showTotal();
    if (rem > 0) toast(`${$fmt(rem)} is more than the total outstanding`, true);
  };
  const load = async () => {
    invs = sel.value ? (await GET(`/api/invoices?kind=${kind}&status=open&contact_id=${sel.value}`)).sort((a, b) => a.due_date.localeCompare(b.due_date)) : [];
    renderInvs();
    const want = QS.get('inv');
    if (want) { const i = $(`.p-alloc[data-id="${want}"]`); if (i) { i.value = fmt(+i.dataset.bal); showTotal(); } }
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
  const kind = p.partner ? 'partner' : p.kind === 'receipt' ? 'sale' : 'bill', K = KIND[kind];
  page(p.partner ? 'Partner contribution received' : p.kind === 'receipt' ? 'Customer payment' : 'Supplier payment', `${esc(p.contact_name)} · ${dfmt(p.date)}`,
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
    if (!await confirmBox(`Delete this payment? The ${K.one.toLowerCase()}s it paid will show as outstanding again.`, 'Delete', true)) return;
    try { await api('DELETE', `/api/payments/${p.id}`); await refreshState(); toast('Payment deleted'); go(K.base); } catch (e) { fail(e); }
  };
}

// ---------------------------------------------------------------- banking
async function bankingHome() {
  const banks = S.accounts.filter(a => isBank(a) && !a.archived);
  const want = QS.get('account');
  const sel = want && banks.some(b => b.id == want) ? want : firstBank();
  if (!sel) {
    page('Banking', '', '<button class="primary" id="bk-manage">Manage accounts</button>',
      '<div class="empty">No bank accounts yet. Use Manage accounts to add one.</div>');
    $('#bk-manage').onclick = manageBankAccounts;
    return;
  }
  await registerView(sel, {
    title: 'Banking',
    picker: `<div class="tabs">${banks.map(b => `<a href="#/banking?account=${b.id}" class="${b.id == sel ? 'active' : ''}">${esc(b.name)}${isShared(b) ? ` <span class="muted">(${pctText(b.share_bp)})</span>` : ''}</a>`).join('')}</div>`,
    actions: `<button id="bk-manage">Manage accounts</button><a class="btn" href="#/rules">Bank rules</a><a class="btn" href="#/banking/transfer">Transfer</a><a class="btn" href="#/banking/receive">Receive money</a>
      <a class="btn" href="#/banking/spend">Spend money</a><a class="btn primary" href="#/reconcile?account=${sel}">Reconcile</a>`,
    hashBase: `#/banking?account=${sel}`,
  });
  $('#bk-manage').onclick = manageBankAccounts;
}

// Add, rename and remove bank accounts and credit cards.
function manageBankAccounts() {
  const draw = () => {
    const banks = S.accounts.filter(a => isBank(a) && !a.archived);
    modal(`<h2>Bank accounts</h2>
      <p class="muted">Rename an account by editing its name. Accounts that have transactions can only be removed once their balance is zero; they're archived so history is kept.</p>
      <p class="muted">For a shared account (e.g. a joint account), set <b>Your share</b> to the percentage that belongs to you. Reports then show only your share of its balance and transactions. It applies to all past and future transactions in that account.</p>
      ${banks.length ? `<table><thead><tr><th>Name</th><th>Kind</th><th class="num">Your share %</th><th class="num">Balance</th><th></th></tr></thead><tbody>
      ${banks.map(a => `<tr data-id="${a.id}"><td><input class="mb-name" value="${esc(a.name)}" aria-label="Account name"></td>
        <td class="muted">${a.type === 'asset' ? 'Bank account' : 'Credit card'}</td>
        <td class="num"><input class="mb-share num" style="width:80px" inputmode="decimal" value="${+(a.share_bp / 100).toFixed(2)}" aria-label="Your share percent"></td>
        <td class="num">${$fmt(a.balance)}</td>
        <td><button class="small danger mb-del">Remove</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">No bank accounts</div>'}
      <div class="actions" style="margin-top:10px"><button class="small" id="mb-add-bank">+ Bank account</button><button class="small" id="mb-add-cc">+ Credit card</button></div>
      <div class="modal-foot"><button data-x="no">Cancel</button><button class="primary" data-x="yes">Save</button></div>`, el => {
      const saveNames = async () => {
        for (const tr of el.querySelectorAll('tr[data-id]')) {
          const a = acct(tr.dataset.id), name = tr.querySelector('.mb-name').value.trim();
          if (!name) throw new Error('Every bank account needs a name');
          const pct = parsePct(tr.querySelector('.mb-share').value, name);
          const b = {};
          if (name !== a.name) b.name = name;
          if (Math.round(pct * 100) !== a.share_bp) b.share_pct = pct;
          if (Object.keys(b).length) await api('PUT', `/api/accounts/${a.id}`, b);
        }
      };
      const add = async type => {
        try { await saveNames(); await refreshState(); await createBankAccount(type); await refreshState(); draw(); } catch (e) { fail(e); }
      };
      el.querySelector('#mb-add-bank').onclick = () => add('asset');
      el.querySelector('#mb-add-cc').onclick = () => add('liability');
      el.querySelectorAll('.mb-del').forEach(b => b.onclick = async () => {
        const a = acct(b.closest('tr').dataset.id);
        try {
          await saveNames(); await refreshState();
          if (!await confirmBox(`Remove <b>${esc(a.name)}</b>?${a.used ? ' It has transactions, so it will be archived and its history kept.' : ''}`, 'Remove', true)) { draw(); return; }
          if (a.used) await api('PUT', `/api/accounts/${a.id}`, { archived: true });
          else await api('DELETE', `/api/accounts/${a.id}`);
          await refreshState(); toast(`${a.name} removed`); draw();
        } catch (e) { fail(e); draw(); }
      });
      el.querySelector('[data-x=no]').onclick = () => { closeModal(); router(); };
      el.querySelector('[data-x=yes]').onclick = async () => {
        try { await saveNames(); await refreshState(); closeModal(); toast('Bank accounts saved'); router(); } catch (e) { fail(e); }
      };
    });
  };
  draw();
}
async function registerPage(id) {
  await registerView(id, { title: null, actions: `<button onclick="history.back()">Back</button>`, hashBase: `#/register/${id}?` });
}
async function registerView(id, { title, picker = '', actions = '', hashBase }) {
  const from = QS.get('from') || addDays(S.today, -92), to = QS.get('to') || '';
  const r = await GET(`/api/register?account_id=${id}&from=${from}${to ? `&to=${to}` : ''}`);
  const a = r.account, bank = isBank(a), sh = r.shared;
  const sgn = ['asset', 'cos', 'expense'].includes(a.type) ? 1 : -1;  // show the share in the account's natural sign
  const lines = [...r.lines].reverse();
  page(title || esc(a.name), title ? '' : TYPE_LABEL[a.type], actions,
    `${picker}<div class="filters"><div class="field"><label>From</label><input type="date" id="r-from" value="${from}"></div>
      <div class="field"><label>To</label><input type="date" id="r-to" value="${to}"></div>
      <div class="field"><label>Search</label><input id="r-q" placeholder="Description, contact, amount, rule"></div>
      ${bank ? `<div class="field"><label>Show</label><select id="r-rule"><option value="">All transactions</option><option value="rule">Created by bank rules</option>
        <option value="auto">Auto-applied by rules</option><option value="review">Rule suggestions to review</option></select></div>` : ''}
      <div style="margin-left:auto;text-align:right"><div class="muted" style="font-size:12px">Balance${to ? ` at ${dfmt(to)}` : ''}</div>
      <div style="font-size:20px;font-weight:650">${$fmt(r.closing)}</div>
      ${sh ? `<div class="muted" style="font-size:12px">Your share ${$fmt(r.share_closing)}</div>` : ''}</div></div>
    ${sh ? `<div class="notice info">${isShared(a) ? `This is a shared account. ${pctText(a.share_bp)} of it is yours.` : 'Some of these transactions came from a shared bank account.'} Reports use the <b>Your share</b> column.</div>` : ''}
    <div class="panel flush">${lines.length ? `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Description</th><th>Type</th><th>Ref</th>
      <th class="num">${bank ? 'Money in' : 'Debit'}</th><th class="num">${bank ? 'Money out' : 'Credit'}</th><th class="num">Balance</th>${sh ? '<th class="num">Your share</th><th class="num">Your balance</th>' : ''}${bank ? '<th></th>' : ''}</tr></thead><tbody>
      ${lines.map(l => `<tr class="click" data-href="${sourceHref(l.source, l.source_id, l.journal_id)}" data-rule="${!l.rule_id ? '' : l.rule_mode === 'auto' ? 'auto' : l.reviewed ? 'reviewed' : 'review'}" data-q="${esc((l.memo + ' ' + (l.description || '') + ' ' + (l.contact_name || '') + ' ' + (l.rule_name || '') + ' ' + (l.bank_text || '') + ' ' + fmt(Math.abs(l.amount))).toLowerCase())}">
        <td>${dfmt(l.date)}</td><td>${esc(l.contact_name ? `${l.contact_name}${l.description && !l.description.includes(l.contact_name) ? ' – ' + l.description : ''}` : (l.description || l.memo || ''))}${rulePill(l)}${l.category ? `<div class="muted" style="font-size:12px">→ ${esc(l.category)}</div>` : ''}</td>
        <td class="muted">${SOURCE_LABEL[l.source] || l.source}</td><td class="muted">${esc(l.reference || '')}</td>
        <td class="num">${l.amount > 0 ? fmt(l.amount) : ''}</td><td class="num">${l.amount < 0 ? fmt(-l.amount) : ''}</td><td class="num">${fmt(l.balance)}</td>
        ${sh ? `<td class="num">${fmt(l.share * sgn)}</td><td class="num">${fmt(l.share_balance)}</td>` : ''}
        ${bank ? `<td title="${l.recon_id ? 'Reconciled' : 'Not reconciled'}">${l.recon_id ? '✓' : ''}</td>` : ''}</tr>`).join('')}
      <tr class="total"><td colspan="6">Opening balance ${dfmt(from)}</td><td class="num">${fmt(r.opening)}</td>${sh ? `<td></td><td class="num">${fmt(r.share_opening)}</td>` : ''}${bank ? '<td></td>' : ''}</tr>
      </tbody></table></div>` : '<div class="empty">No transactions in this period</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = e => { if (!e.target.closest('a')) go(tr.dataset.href); });
  const sep = hashBase.includes('?') ? (hashBase.endsWith('?') ? '' : '&') : '?';
  const reload = () => go(`${hashBase}${sep}from=${val('#r-from')}${val('#r-to') ? `&to=${val('#r-to')}` : ''}`);
  $('#r-from').onchange = reload; $('#r-to').onchange = reload;
  const filter = () => {
    const q = val('#r-q').toLowerCase().trim(), rf = val('#r-rule');
    $$('tr[data-q]').forEach(tr => {
      const r = tr.dataset.rule;
      const ruleOk = !rf || (rf === 'rule' ? !!r : r === rf);
      tr.hidden = !ruleOk || (q && !tr.dataset.q.includes(q));
    });
  };
  $('#r-q').oninput = filter;
  if ($('#r-rule')) $('#r-rule').onchange = filter;
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
    `${ro ? `<div class="notice info">${why}</div>` : ''}${j && j.rule_id ? `<div class="notice ${j.rule_mode === 'recommend' && !j.reviewed ? 'warn' : 'info'}">
      ${j.rule_mode === 'auto' ? 'Categorised and ticked automatically' : j.reviewed ? 'Categorised (and since reviewed)' : 'Categorised, waiting for your review,'} by the bank rule
      <a href="#/rules/${j.rule_id}"><b>${esc(j.rule_name || 'rule')}</b></a>${j.bank_text ? ` from the statement line "${esc(j.bank_text)}"` : ''}.
      ${ro ? '' : 'If the category is wrong, change it below and save. To stop it happening again, edit the rule.'}</div>` : ''}<div class="panel">${body}</div>`);
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
      ${rows.map((r, i) => `<tr data-i="${i}"><td><select class="acct-pick" data-f="account_id">${acctOptions(r.account_id, postable)}</select></td><td><input data-f="description" value="${esc(r.description)}"></td>
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
      <td>${esc(l.contact_name ? `${l.contact_name}${l.description && !l.description.includes(l.contact_name) ? ' – ' + l.description : ''}` : (l.description || l.memo || ''))} <a href="${sourceHref(l.source, l.source_id, l.journal_id)}" class="muted" title="Open">↗</a>${rulePill(l)}
        ${l.category ? `<div class="muted" style="font-size:12px">→ ${esc(l.category)}</div>` : ''}</td>
      <td class="muted">${SOURCE_LABEL[l.source] || ''}</td><td class="muted">${esc(l.reference || '')}</td>
      <td class="num">${l.amount > 0 ? fmt(l.amount) : ''}</td><td class="num">${l.amount < 0 ? fmt(-l.amount) : ''}</td></tr>`).join('')
      : `<tr><td colspan="7" class="empty">Everything up to this date is reconciled. Enter new transactions, or import your statement.</td></tr>`;
    $$('tr[data-id]', body).forEach(tr => {
      tr.onclick = e => {
        if (e.target.closest('a')) return;
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
      // Remember the statement text of CSV lines matched to items entered by hand, so re-importing skips them.
      const bankText = {};
      (R.csv || []).forEach(c => { if (c.status === 'none' && c.match && R.ticked.has(c.match) && c.description) bankText[c.match] = { text: c.description, date: c.date }; });
      await api('POST', '/api/reconcile', { account_id: acc, statement_date: val('#rc-date'), statement_balance: parseMoney(val('#rc-bal')),
        line_ids: [...R.ticked], bank_text: bankText });
      toast('Reconciled'); R.ticked.clear(); R.csv = null; R.statement_balance = ''; await refreshState(); reconcilePage();
    } catch (e) { fail(e); }
  };
  const undo = $('#rc-undo');
  if (undo) undo.onclick = async () => {
    if (!await confirmBox('Undo the most recent reconciliation? Its items will become unreconciled again.', 'Undo')) return;
    try { await api('POST', '/api/reconcile/undo', { account_id: acc }); toast('Reconciliation undone'); reconcilePage(); } catch (e) { fail(e); }
  };
  $('#rc-add').onclick = () => quickBankTxn(acc, {}, () => reconcilePage());

  // CSV statement matching. The server has already (see /api/reconcile/import):
  //   skipped lines already in numbr ('duplicate'), and run bank rules ('auto' / 'recommend', or 'locked' / 'error').
  // Lines left as 'none' are matched here to items entered by hand (same amount, within 7 days).
  const drawCsv = () => {
    if (!R.csv) { $('#rc-csv').innerHTML = ''; return; }
    matchedIds.clear();
    const used = new Set(R.csv.filter(c => c.line_id).map(c => c.line_id));
    R.csv.forEach(c => {
      if (c.status === 'auto' || c.status === 'recommend') { c.match = lineById[c.line_id] ? c.line_id : null; return; }
      if (c.status === 'duplicate') { c.match = !c.reconciled && lineById[c.line_id] ? c.line_id : null; return; }
      if (c.status !== 'none') { c.match = null; return; }
      if (c.match && lineById[c.match] && !used.has(c.match)) { used.add(c.match); return; }
      c.match = null;
      let best = null, bestGap = 99;
      data.lines.forEach(l => {
        if (used.has(l.id) || l.bank_text || l.amount !== c.amount) return;
        const gap = Math.abs((new Date(l.date) - new Date(c.date)) / 86400000);
        if (gap <= 7 && gap < bestGap) { best = l; bestGap = gap; }
      });
      if (best) { c.match = best.id; used.add(best.id); }
    });
    R.csv.forEach(c => { if (c.match) matchedIds.add(c.match); });
    const sd = val('#rc-date');
    const inPeriod = R.csv.filter(c => c.date <= sd);
    // Tick each match once (so unticking sticks). Recommended lines wait for the user, unless already reviewed.
    inPeriod.forEach(c => {
      if (!c.match || c.ticked) return;
      const l = lineById[c.match];
      if (l.rule_mode === 'recommend' && !l.reviewed) return;
      R.ticked.add(c.match); c.ticked = true;
    });
    const skipped = inPeriod.filter(c => c.status === 'duplicate' && !c.match).length;
    const auto = inPeriod.filter(c => c.status === 'auto' && c.match);
    const review = inPeriod.filter(c => c.match && lineById[c.match].rule_mode === 'recommend' && !lineById[c.match].reviewed);
    const matched = inPeriod.filter(c => c.match && c.status !== 'auto' && !review.includes(c));
    const unmatched = inPeriod.filter(c => !c.match && c.status !== 'duplicate');
    const dups = inPeriod.filter(c => c.possible_dup_line_id && c.match && lineById[c.possible_dup_line_id]);
    const idx = c => R.csv.indexOf(c);
    const amt = c => `<td class="num${c.amount < 0 ? ' neg' : ''}">${fmt(c.amount)}</td>`;
    $('#rc-csv').innerHTML = `<div class="panel flush"><h2>Statement lines (${inPeriod.length} up to ${dfmt(sd)}) · ${auto.length} auto-applied · ${review.length} to review · ${matched.length} matched · ${unmatched.length} unmatched
        <button class="small" id="rc-csv-clear" style="float:right">Close statement</button></h2>
      ${skipped ? `<p class="muted" style="padding:0 18px">${skipped} line${skipped === 1 ? ' was' : 's were'} already in numbr and reconciled, so ${skipped === 1 ? 'it was' : 'they were'} skipped.</p>` : ''}
      ${dups.map(c => { const o = lineById[c.possible_dup_line_id]; return `<div class="notice warn" style="margin:12px 18px">Possible double-up: the rule <b>${esc(c.rule_name)}</b> created
        <a href="#/txn/${c.journal_id}">${esc(c.description)} ${$fmt(c.amount)} on ${dfmt(c.date)}</a>, and you also entered
        <a href="${sourceHref(o.source, o.source_id, o.journal_id)}">${esc(o.description || o.memo || 'a transaction')} on ${dfmt(o.date)}</a> for the same amount. If they're the same, delete one.</div>`; }).join('')}
      ${review.length ? `<h3 class="csv-sub">To review</h3><p class="muted" style="padding:0 18px">A recommend rule categorised these. Check the category, then tick them.</p>
      <table><thead><tr><th>Date</th><th>Description</th><th>Categorised as</th><th class="num">Amount</th><th></th></tr></thead><tbody>
      ${review.map(c => { const l = lineById[c.match]; return `<tr><td>${dfmt(c.date)}</td><td>${esc(c.description)}${rulePill(l)}</td><td>${esc(l.category || '')}</td>${amt(c)}
        <td class="actions" style="justify-content:flex-end"><button class="small" data-open="${l.journal_id}">Change</button><button class="small primary" data-ok="${l.id}">${R.ticked.has(l.id) ? 'Ticked ✓' : 'Looks right, tick'}</button></td></tr>`; }).join('')}
      </tbody></table>` : ''}
      ${unmatched.length ? `<h3 class="csv-sub">Unmatched <button class="small" id="rc-rerun" style="float:right" title="Run your bank rules again over these lines, e.g. after making a new rule">Apply rules again</button></h3><p class="muted" style="padding:0 18px">These statement lines have no matching transaction in numbr. Create one, or enter it properly (e.g. as a customer payment) and come back. <b>Make a rule</b> handles lines like it automatically next time.</p>
      <table><thead><tr><th>Date</th><th>Description</th><th class="num">Amount</th><th></th></tr></thead><tbody>
      ${unmatched.map(c => `<tr><td>${dfmt(c.date)}</td><td>${esc(c.description)}${c.status === 'locked' ? `<div class="muted" style="font-size:12px">Rule ${esc(c.rule_name)} matched, but this date is locked.</div>` : ''}${c.status === 'error' ? `<div class="neg" style="font-size:12px">Rule ${esc(c.rule_name)} matched but couldn't apply: ${esc(c.error)}</div>` : ''}</td>${amt(c)}
        <td class="actions" style="justify-content:flex-end"><button class="small" data-rule="${idx(c)}">Make a rule</button>${c.amount > 0 ? `<button class="small" data-pay="receipt" data-i="${idx(c)}">Customer payment</button><button class="small" data-pay="contribution" data-i="${idx(c)}">Partner contribution</button>` : `<button class="small" data-pay="payment" data-i="${idx(c)}">Bill payment</button>`}
        <button class="small" data-xfer="${idx(c)}">Transfer</button><button class="small primary" data-new="${idx(c)}">${c.amount < 0 ? 'Spend money' : 'Receive money'}</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="notice good" style="margin:12px 18px">Every statement line matches a transaction in numbr.</div>'}</div>`;
    $('#rc-csv-clear').onclick = () => { R.csv = null; drawCsv(); drawLines(); };
    const rb = $('#rc-rerun'); if (rb) rb.onclick = rerunRules;
    $$('[data-open]').forEach(b => b.onclick = () => { RETURN_TO = `#/reconcile?account=${acc}`; go(`#/txn/${b.dataset.open}`); });
    $$('[data-ok]').forEach(b => b.onclick = () => { R.ticked.add(+b.dataset.ok); drawCsv(); drawLines(); });
    $$('[data-rule]').forEach(b => b.onclick = () => {
      const c = R.csv[+b.dataset.rule];
      const words = c.description.replace(/\d.*$/, '').trim() || c.description;
      PREFILL = { rule: { name: words.slice(0, 40), text_op: 'contains', text_tokens: [{ t: 'text', v: words }], direction: c.amount < 0 ? 'out' : 'in',
        account_ids: [], sample: c.description } };
      RETURN_TO = `#/reconcile?account=${acc}`;
      go('#/rules/new');
    });
    $$('[data-new]').forEach(b => b.onclick = () => {
      const c = R.csv[+b.dataset.new];
      quickBankTxn(acc, { date: c.date, description: c.description, amount: Math.abs(c.amount), kind: c.amount < 0 ? 'spend' : 'receive' }, () => reconcilePage());
    });
    $$('[data-xfer]').forEach(b => b.onclick = () => {
      const c = R.csv[+b.dataset.xfer];
      quickBankTxn(acc, { date: c.date, description: c.description, amount: Math.abs(c.amount), kind: c.amount < 0 ? 'xfer-out' : 'xfer-in' }, () => reconcilePage());
    });
    $$('[data-pay]').forEach(b => b.onclick = () => {
      const c = R.csv[+b.dataset.i];
      PREFILL = { date: c.date, amount: Math.abs(c.amount), reference: c.description.slice(0, 40), bank_account_id: acc };
      RETURN_TO = `#/reconcile?account=${acc}`;
      go({ receipt: '#/receive', contribution: '#/partner/receive', payment: '#/pay' }[b.dataset.pay]);
    });
  };
  $('#rc-file').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const txt = await f.text();
    const parsed = parseCSV(txt);
    if (parsed.length < 1) return toast('That file looks empty', true);
    csvMapping(parsed, acc, async (lines, target, mapping) => {
      try {
        const res = await api('POST', '/api/reconcile/import', { account_id: target, lines, mapping });
        const ta = acct(target); if (ta) ta.csv_map = mapping; // remembered for next time (saved on the account)
        lines.forEach((l, i) => Object.assign(l, res.results[i]));
        if (String(target) !== String(acc)) { R.account_id = target; R.ticked = new Set(); R.statement_balance = ''; R.statement_date = null; }
        R.csv = lines;
        const maxD = lines.reduce((m, l) => (l.date > m ? l.date : m), '');
        if (maxD) R.statement_date = maxD;  // the statement runs up to its last line
        const n = res.counts, parts = [];
        if (n.auto) parts.push(`${n.auto} auto-applied`);
        if (n.recommend) parts.push(`${n.recommend} to review`);
        if (n.duplicate) parts.push(`${n.duplicate} already in numbr`);
        toast(`Imported ${lines.length} statement lines into ${acct(target).name}${parts.length ? ': ' + parts.join(', ') : ''}`);
        go(`#/reconcile?account=${target}`);
      } catch (err) { fail(err); }
    });
    e.target.value = '';
  };
  // After making a rule from an unmatched line: run the rules again over the lines still unmatched.
  const rerunRules = async () => {
    const todo = R.csv.filter(c => !c.match && c.status !== 'duplicate');
    try {
      const res = await api('POST', '/api/reconcile/import', { account_id: acc, lines: todo.map(c => ({ date: c.date, amount: c.amount, description: c.description })) });
      todo.forEach((c, i) => { delete c.error; Object.assign(c, res.results[i]); });
      const hit = (res.counts.auto || 0) + (res.counts.recommend || 0);
      toast(hit ? `Rules categorised ${hit} more line${hit === 1 ? '' : 's'}` : 'No rules matched the unmatched lines');
      reconcilePage();
    } catch (err) { fail(err); }
  };
  drawCsv();
  drawLines();
}

function quickBankTxn(bankId, pre, done) {
  const kind0 = pre.kind || 'spend';
  modal(`<h2>New bank transaction</h2>
    <div class="form-grid">
      <div class="field"><label>Type</label><select id="q-kind">
        <option value="spend" ${kind0 === 'spend' ? 'selected' : ''}>Spend money (money out)</option>
        <option value="receive" ${kind0 === 'receive' ? 'selected' : ''}>Receive money (money in)</option>
        <option value="xfer-out" ${kind0 === 'xfer-out' ? 'selected' : ''}>Transfer out (to another of my accounts)</option>
        <option value="xfer-in" ${kind0 === 'xfer-in' ? 'selected' : ''}>Transfer in (from another of my accounts)</option></select></div>
      <div class="field"><label>Account</label><select id="q-bank">${bankOptions(bankId)}</select></div>
      <div class="field"><label>Date</label><input type="date" id="q-date" value="${pre.date || S.today}"></div>
      <div class="field"><label>Amount</label><input id="q-amt" class="num" value="${pre.amount ? fmt(pre.amount) : ''}"></div>
    </div>
    <div class="field"><label>Description</label><input id="q-desc" value="${esc(pre.description || '')}"></div>
    <div class="field" id="q-acctbox"><label>Allocate to account</label><select class="acct-pick" id="q-acct">${acctOptions('', a => postable(a) && !isBank(a))}</select></div>
    <div class="field" id="q-otherbox"><label id="q-otherlabel">Other bank account</label><select id="q-other">${bankOptions('', 'Choose account…')}</select>
      <div class="hint">When you import that account's statement, its side of the transfer is matched to this one.</div></div>
    <div class="hint" id="q-payhint">For a customer or supplier payment against an invoice or bill, use Receive payment / Pay bills instead so it clears what they owe.</div>
    <div class="modal-foot"><button data-x="no">Cancel</button><button class="primary" data-x="yes">Save</button></div>`, el => {
    const bankFee = S.accounts.find(a => a.code === '6-1200');
    if (bankFee && kind0 === 'spend' && (!pre.description || /\bfees?\b|charge/i.test(pre.description))) el.querySelector('#q-acct').value = bankFee.id;
    const showKind = () => {
      const k = val('#q-kind', el), xfer = k.startsWith('xfer');
      el.querySelector('#q-acctbox').hidden = xfer; el.querySelector('#q-payhint').hidden = xfer;
      el.querySelector('#q-otherbox').hidden = !xfer;
      el.querySelector('#q-otherlabel').textContent = k === 'xfer-out' ? 'Transfer to' : 'Transfer from';
    };
    el.querySelector('#q-kind').onchange = showKind;
    showKind();
    el.querySelector('[data-x=no]').onclick = closeModal;
    el.querySelector('[data-x=yes]').onclick = async () => {
      try {
        const amt = parseMoney(val('#q-amt', el)); if (isNaN(amt) || amt <= 0) throw new Error('Enter an amount');
        const k = val('#q-kind', el), bank = val('#q-bank', el);
        if (k.startsWith('xfer')) {
          const other = val('#q-other', el);
          if (!other) throw new Error('Choose the other bank account');
          if (other === bank) throw new Error('Choose a different account from the one the money is in');
          const [from, to] = k === 'xfer-out' ? [bank, other] : [other, bank];
          await api('POST', '/api/txn/transfer', { from_account_id: from, to_account_id: to, date: val('#q-date', el), amount: amt, memo: val('#q-desc', el) });
        } else {
          if (!val('#q-acct', el)) throw new Error('Choose an account');
          await api('POST', `/api/txn/${k}`, { bank_account_id: bank, date: val('#q-date', el), memo: val('#q-desc', el),
            lines: [{ account_id: +val('#q-acct', el), description: val('#q-desc', el), amount: amt }] });
        }
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
  s = s.trim().replace(/[\sT]+\d{1,2}:\d{2}.*$/, ''); // drop a trailing time, keep "07 Nov 26"
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

function csvMapping(rows, accId, onDone) {
  const isCard = (acct(accId) || {}).type === 'liability';
  const first = rows[0];
  const looksHeader = first.some(c => /date|amount|desc|narr|debit|credit|details|balance|transaction/i.test(c)) && !first.some(c => parseAnyDate(c));
  const ncol = Math.max(...rows.map(r => r.length));
  const guess = re => { const i = looksHeader ? first.findIndex(c => re.test(c)) : -1; return i; };
  const sample = looksHeader ? rows[1] || [] : first;
  let gDate = guess(/date/i); if (gDate < 0) gDate = sample.findIndex(c => parseAnyDate(c));
  let gAmt = guess(/^amount$|amount/i);
  const gDr = guess(/debit|withdraw|money out/i), gCr = guess(/credit|deposit|money in/i);
  let gDesc = guess(/desc|narr|details|particulars/i);
  if (gDesc < 0) gDesc = guess(/transaction/i);
  if (!looksHeader) { // e.g. CBA: date, amount, description, balance
    const numIdx = sample.map((c, i) => (!isNaN(parseMoney(c)) && c !== '' && i !== gDate ? i : -1)).filter(i => i >= 0);
    if (gAmt < 0 && numIdx.length) gAmt = numIdx[0];
    if (gDesc < 0) gDesc = sample.findIndex((c, i) => i !== gDate && isNaN(parseMoney(c)));
  }
  const mode0 = gAmt >= 0 || gDr < 0 ? 'single' : 'split';
  const colOpts = sel => `<option value="-1">—</option>` + [...Array(ncol)].map((_, i) => `<option value="${i}" ${i === sel ? 'selected' : ''}>${looksHeader ? esc(first[i] || `Column ${i + 1}`) : `Column ${i + 1}`}${!looksHeader && sample[i] ? ` (e.g. ${esc(sample[i].slice(0, 24))})` : ''}</option>`).join('');
  modal(`<h2>Import a bank statement</h2>
    <div class="field"><label>Import into</label><select id="m-acc">${bankOptions(accId)}</select>
      <div class="hint">Choose the account this statement is from. Its bank rules are applied, and lines already in it are skipped.</div></div>
    <h3 style="margin:14px 0 8px">Match up your statement columns</h3>
    <div class="hint" id="m-saved" hidden>Using the column choices from your last import into this account.</div>
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
    const accSel = el.querySelector('#m-acc'), go_ = el.querySelector('[data-x=yes]');
    const COLS = { date: '#m-date', desc: '#m-desc', amt: '#m-amt', dr: '#m-dr', cr: '#m-cr' };
    const defaults = { head: looksHeader, df: 'dmy', mode: mode0, cols: { date: gDate, desc: gDesc, amt: gAmt, dr: gDr, cr: gCr } };
    // A saved column is found by its header name first (in case the bank reorders columns), then by position.
    const findCol = (c, head) => {
      if (!c) return -1;
      if (head && c.name) { const i = first.findIndex(h => h.toLowerCase() === c.name.toLowerCase()); if (i >= 0) return i; }
      return c.i < ncol ? c.i : -1;
    };
    const applyAccount = () => {
      const a = acct(accSel.value), m = a.csv_map;
      go_.textContent = `Import into ${a.name}`;
      el.querySelector('#m-saved').hidden = !m;
      el.querySelector('#m-head').checked = m ? m.head : defaults.head;
      el.querySelector('#m-df').value = m ? m.df : defaults.df;
      el.querySelector('#m-mode').value = m ? m.mode : defaults.mode;
      el.querySelector('#m-flip').checked = m ? m.flip : a.type === 'liability';
      Object.entries(COLS).forEach(([k, sel]) => { el.querySelector(sel).value = m ? findCol(m.cols[k], m.head) : defaults.cols[k]; });
    };
    const mapping = () => {
      const head = el.querySelector('#m-head').checked;
      const cols = {};
      Object.entries(COLS).forEach(([k, sel]) => { const i = g(sel); cols[k] = { i, name: head && i >= 0 ? first[i] || '' : '' }; });
      return { head, df: el.querySelector('#m-df').value, mode: el.querySelector('#m-mode').value, flip: el.querySelector('#m-flip').checked, cols };
    };
    el.querySelectorAll('select, input').forEach(x => { x.onchange = build; });
    accSel.onchange = () => { applyAccount(); build(); };
    applyAccount();
    build();
    el.querySelector('[data-x=no]').onclick = closeModal;
    go_.onclick = () => { const out = build(); if (!out.length) return toast('No usable lines found. Check the column choices.', true); closeModal(); onDone(out, accSel.value, mapping()); };
  });
}

// ---------------------------------------------------------------- recurring
const FREQ_LABEL = { weekly: 'Weekly', fortnightly: 'Fortnightly', monthly: 'Monthly', quarterly: 'Quarterly', yearly: 'Yearly' };
const RKIND = { sale: 'Sales invoice', bill: 'Supplier bill', spend: 'Spend money', receive: 'Receive money', partner: 'Partner contribution' };

async function recurringList() {
  const list = await GET('/api/recurring');
  page('Recurring transactions', 'Created automatically on their date whenever numbr is open (or the next time you open it)', `<a class="btn primary" href="#/recurring/new">New recurring</a>`,
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
      <div class="field r-share"><label>Your share %</label><input id="r-share" inputmode="decimal" value="${r ? +(r.share_bp / 100).toFixed(2) : 100}">
        <div class="hint">Less than 100 if it's shared. Reports show only your share.</div></div>
    </div>
    <div class="inline" style="gap:18px;margin-bottom:12px">
      <label class="check r-eom"><input type="checkbox" id="r-eom" ${r && r.anchor_day === 31 ? 'checked' : ''}> Always on the last day of the month</label>
      <label class="check"><input type="checkbox" id="r-active" ${!r || r.active ? 'checked' : ''}> Active</label></div>
    <div class="field r-amount"><label>Amount</label><input id="r-amount" class="num" inputmode="decimal" placeholder="0.00" value="${r && r.kind === 'partner' ? fmt(r.total || r.lines.reduce((s, l) => s + l.amount, 0)) : ''}"></div>
    <div id="r-lines"></div>
    <div class="field" style="margin-top:12px"><label>Notes / memo</label><input id="r-memo" value="${esc(r ? r.memo || '' : '')}"></div>
    <div class="hint">If the next date is today or in the past, saving will create the due transaction(s) straight away.</div></div>`);
  const ed = lineEditor($('#r-lines'), { lines: r && r.kind !== 'partner' ? r.lines : [], filter: a => postable(a) && !isBank(a) });
  const csel = $('#r-contact');
  const upd = () => {
    const k = val('#r-kind');
    const isP = k === 'partner';
    const inv = k === 'sale' || k === 'bill' || isP;
    $('.r-bank').hidden = inv; $('.r-terms').hidden = !inv; $('.r-share').hidden = !inv || isP;
    $('#r-lines').hidden = isP; $('.r-amount').hidden = !isP;
    $('#r-clabel').textContent = k === 'sale' ? 'Customer' : k === 'bill' ? 'Supplier' : isP ? 'Partner' : (k === 'spend' ? 'Paid to (optional)' : 'Received from (optional)');
    const cur = csel.value || (r ? r.contact_id : '');
    csel.innerHTML = contactOptions(cur, isP ? null : k === 'sale' || k === 'receive' ? 'customer' : 'supplier', inv ? 'Choose…' : 'None');
    $('.r-eom').hidden = ['weekly', 'fortnightly'].includes(val('#r-freq'));
  };
  wireContactSelect(csel, 'customer', () => { const c = contactById(csel.value); if (c) ed.setDefaultAccount(c.default_account_id); });
  $('#r-kind').onchange = upd; $('#r-freq').onchange = upd;
  upd();
  const partnerAmount = () => {
    const amt = parseMoney(val('#r-amount'));
    if (isNaN(amt) || amt <= 0) throw new Error('Enter the amount the partner is due to pay');
    return amt;
  };
  const payload = () => ({
    share_pct: ['sale', 'bill'].includes(val('#r-kind')) ? parsePct(val('#r-share'), 'Your share') : null,
    name: val('#r-name'), kind: val('#r-kind'), contact_id: csel.value || null, bank_account_id: val('#r-bank'), frequency: val('#r-freq'),
    next_date: val('#r-next'), end_date: val('#r-end'), terms_days: val('#r-terms'), memo: val('#r-memo'),
    ...(val('#r-kind') === 'partner' ? { amount: partnerAmount() } : { lines: ed.get() }),
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

// ---------------------------------------------------------------- bank rules
// Rules categorise statement lines when a bank CSV is imported on the Reconcile screen.
// The text pattern is built from parts here; the server turns the parts into a regex.
const RULE_MODE = { auto: 'Auto-apply', recommend: 'Recommend' };
const TOKEN_LABEL = { text: 'Text', number: 'Number', letters: 'Letters', date: 'Date', any: 'Anything' };
const DATE_FORMATS = ['DD/MM/YYYY', 'DD/MM/YY', 'DD/MM', 'DD-MM-YYYY', 'DDMMYY', 'DDMMYYYY', 'YYYY-MM-DD', 'DD MMM', 'DD MMM YYYY'];

function tokenText(t) {
  if (t.t === 'text') return `"${t.v || ''}"`;
  if (t.t === 'number') return t.count ? `a ${t.count}-digit number` : 'a number';
  if (t.t === 'letters') return t.count ? `${t.count} letters` : 'some letters';
  if (t.t === 'date') return `a date (${t.fmt || '?'})`;
  return 'anything';
}
function ruleSummary(r) {
  const parts = [];
  if (r.text_op) parts.push(`Text ${r.text_op === 'equals' ? 'is exactly' : 'contains'} ${r.text_tokens.map(tokenText).join(' then ')}`);
  const dir = { out: 'Money out', in: 'Money in' }[r.direction] || '';
  const op = { eq: 'of exactly', lt: 'under', gt: 'over' }[r.amount_op];
  if (op) parts.push(`${dir || 'Amount'} ${op} ${$fmt(r.amount)}`);
  else if (dir) parts.push(dir);
  return parts.join(' · ');
}
// Badge for a transaction a rule created. Links to the rule.
function rulePill(l) {
  if (!l.rule_id) return '';
  const review = l.rule_mode === 'recommend' && !l.reviewed;
  const label = l.rule_mode === 'auto' ? 'Auto' : review ? 'Review' : 'Suggested';
  const tip = l.rule_mode === 'auto' ? 'Categorised and ticked automatically by this rule' :
    review ? 'Categorised by this rule. Check the category, then reconcile it.' : 'Categorised by this rule and since reviewed';
  return ` <a class="pill ${l.rule_mode === 'auto' ? 'auto' : 'review'}" href="#/rules/${l.rule_id}" title="${esc(tip)}">${label} · ${esc(l.rule_name || 'rule')}</a>`;
}

async function rulesList() {
  const list = await GET('/api/rules');
  page('Bank rules', 'Categorise lines from an imported bank statement automatically. Rules are checked top to bottom and the first match wins.',
    `<a class="btn" href="#/banking">Banking</a><a class="btn primary" href="#/rules/new">New rule</a>`,
    `<div class="notice info"><b>Auto-apply</b> rules create the transaction and tick it for reconciliation. <b>Recommend</b> rules create the transaction but leave it unticked so you can check the category first. Rules run when you use <b>Import statement CSV</b> on the Reconcile screen.</div>
    <div class="panel flush">${list.length ? `<div class="table-wrap"><table><thead><tr><th style="width:72px">Order</th><th>Name</th><th>When the bank line</th><th>Categorise as</th><th>Bank accounts</th><th>Mode</th><th class="num">Used</th><th></th></tr></thead><tbody>
    ${list.map((r, i) => `<tr class="click" data-href="#/rules/${r.id}" data-i="${i}">
      <td><span class="inline" style="gap:4px"><button class="small ru-up" ${i ? '' : 'disabled'} title="Move up" aria-label="Move up">↑</button><button class="small ru-down" ${i < list.length - 1 ? '' : 'disabled'} title="Move down" aria-label="Move down">↓</button></span></td>
      <td>${esc(r.name)}</td><td>${esc(ruleSummary(r))}</td>
      <td>${isBank(acct(r.target_account_id) || {}) ? 'Transfer: ' : ''}${esc(r.target_name)}${r.contact_name ? ` <span class="muted">· ${esc(r.contact_name)}</span>` : ''}</td>
      <td>${r.account_ids.length ? esc(r.account_ids.map(id => (acct(id) || {}).name || '?').join(', ')) : '<span class="muted">All</span>'}</td>
      <td><span class="pill ${r.mode === 'auto' ? 'auto' : 'review'}">${RULE_MODE[r.mode]}</span></td>
      <td class="num">${r.used}</td><td>${r.active ? '' : '<span class="pill">Off</span>'}</td></tr>`).join('')}
    </tbody></table></div>` : '<div class="empty">No rules yet. Create one, or use <b>Make a rule</b> next to an unmatched line after importing a statement.</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = e => { if (!e.target.closest('button')) go(tr.dataset.href); });
  const move = async (i, d) => {
    const ids = list.map(r => r.id);
    [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
    try { await api('POST', '/api/rules/reorder', { ids }); rulesList(); } catch (e) { fail(e); }
  };
  $$('.ru-up').forEach(b => b.onclick = () => move(+b.closest('tr').dataset.i, -1));
  $$('.ru-down').forEach(b => b.onclick = () => move(+b.closest('tr').dataset.i, 1));
}

async function ruleForm(id) {
  const r = id ? await GET(`/api/rules/${id}`) : null;
  const pre = !r && PREFILL && PREFILL.rule ? PREFILL.rule : null;
  if (pre) PREFILL = null;
  const d = r || pre || {};
  const st = { tokens: (d.text_tokens || []).map(t => ({ ...t })) };
  const banks = S.accounts.filter(a => isBank(a) && (!a.archived || (d.account_ids || []).includes(a.id)));
  const some = (d.account_ids || []).length > 0;
  const mode = d.mode || 'recommend';
  const isXfer = !!d.target_account_id && isBank(acct(d.target_account_id) || {});
  const back = RETURN_TO || '#/rules';
  page(r ? esc(r.name) : 'New bank rule', r ? `Used ${r.used} time${r.used === 1 ? '' : 's'}` : '',
    `<a class="btn" href="${back}" id="ru-cancel">Cancel</a>${r ? '<button class="danger" id="ru-del">Delete</button>' : ''}<button class="primary" id="ru-save">Save rule</button>`,
    `<div class="panel"><div class="form-grid">
      <div class="field"><label>Rule name</label><input id="ru-name" value="${esc(d.name || '')}" placeholder="e.g. Coffee at Bean Cafe"></div></div>
      <label class="check"><input type="checkbox" id="ru-active" ${!r || r.active ? 'checked' : ''}> Active</label></div>
    <div class="panel"><h2>When a bank statement line…</h2>
      <div class="form-grid"><div class="field"><label>Bank text</label><select id="ru-op">
        <option value="" ${!d.text_op ? 'selected' : ''}>Any text (don't check)</option>
        <option value="contains" ${d.text_op === 'contains' || (!r && !pre) ? 'selected' : ''}>Contains</option>
        <option value="equals" ${d.text_op === 'equals' ? 'selected' : ''}>Is exactly</option></select></div></div>
      <div id="ru-textbox">
        <div id="ru-tokens" class="tokens"></div>
        <div class="actions" style="margin:8px 0 4px"><span class="muted" style="font-size:12.5px">Add:</span>
          ${Object.entries(TOKEN_LABEL).map(([k, v]) => `<button type="button" class="small ru-add" data-t="${k}">+ ${v}</button>`).join('')}</div>
        <div class="hint">Parts are matched in order, one straight after the other (spaces between them are optional). Upper or lower case doesn't matter.
          <b>Number</b> = digits, <b>Letters</b> = A–Z, <b>Anything</b> = any characters in between.</div>
      </div>
      <div class="form-grid" style="margin-top:14px">
        <div class="field"><label>Direction</label><select id="ru-dir">
          <option value="any" ${(d.direction || 'any') === 'any' ? 'selected' : ''}>Money in or out</option>
          <option value="out" ${d.direction === 'out' || (!r && !pre) ? 'selected' : ''}>Money out</option>
          <option value="in" ${d.direction === 'in' ? 'selected' : ''}>Money in</option></select></div>
        <div class="field"><label>Amount</label><select id="ru-aop">
          <option value="" ${!d.amount_op ? 'selected' : ''}>Any amount</option>
          <option value="eq" ${d.amount_op === 'eq' ? 'selected' : ''}>Equal to</option>
          <option value="lt" ${d.amount_op === 'lt' ? 'selected' : ''}>Less than</option>
          <option value="gt" ${d.amount_op === 'gt' ? 'selected' : ''}>More than</option></select></div>
        <div class="field" id="ru-amtbox"><label>Dollars</label><input id="ru-amt" class="num" inputmode="decimal" placeholder="0.00" value="${d.amount ? fmt(d.amount) : ''}"></div>
      </div>
      <div class="field"><label>In bank accounts</label>
        <div class="inline" style="gap:18px"><label class="check"><input type="radio" name="ru-scope" value="all" ${some ? '' : 'checked'}> All bank accounts</label>
        <label class="check"><input type="radio" name="ru-scope" value="some" ${some ? 'checked' : ''}> Only these:</label></div>
        <div id="ru-banks" class="inline" style="gap:14px;flex-wrap:wrap;margin-top:6px">${banks.map(b => `<label class="check"><input type="checkbox" class="ru-bank" value="${b.id}" ${(d.account_ids || []).includes(b.id) ? 'checked' : ''}> ${esc(b.name)}</label>`).join('')}</div></div>
    </div>
    <div class="panel"><h2>Then</h2>
      <div class="form-grid">
        <div class="field"><label>Action</label><select id="ru-kind">
          <option value="cat" ${isXfer ? '' : 'selected'}>Categorise it</option>
          <option value="xfer" ${isXfer ? 'selected' : ''}>Transfer to/from another bank account</option></select></div>
        <div class="field" id="ru-catbox"><label>Categorise as</label><select class="acct-pick" id="ru-target">${acctOptions(isXfer ? '' : d.target_account_id || '', a => postable(a) && !isBank(a) && !['re', 'hist'].includes(a.subtype))}</select></div>
        <div class="field" id="ru-xferbox"><label>Other bank account</label><select id="ru-to">${bankOptions(isXfer ? d.target_account_id : '', 'Choose account…')}</select>
          <div class="hint">Money out of the statement's account goes to this account; money in comes from it.</div></div>
        <div class="field"><label>Contact (optional)</label><select id="ru-contact"></select></div>
        <div class="field"><label>Memo (optional)</label><input id="ru-memo" value="${esc(d.memo || '')}" placeholder="Defaults to the bank text"></div>
      </div>
      <div class="field"><label>Mode</label>
        <label class="check" style="margin-bottom:6px"><input type="radio" name="ru-mode" value="auto" ${mode === 'auto' ? 'checked' : ''}> <span><b>Auto-apply</b> – create the transaction and tick it for reconciliation</span></label>
        <label class="check"><input type="radio" name="ru-mode" value="recommend" ${mode === 'recommend' ? 'checked' : ''}> <span><b>Recommend</b> – create the transaction but leave it unticked so I can check the category</span></label></div>
    </div>
    <div class="panel"><h2>Try it</h2>
      <div class="field"><label>Example bank text</label><input id="ru-sample" value="${esc(pre ? pre.sample || '' : '')}" placeholder="Paste a line from your bank statement"></div>
      <div id="ru-result"></div></div>`);
  $('#ru-cancel').onclick = () => { RETURN_TO = null; };
  if (r && r.archived) {
    main.querySelector('.page-head').insertAdjacentHTML('afterend', '<div class="notice warn">This rule has been deleted. It\'s kept so the transactions it created still show where they came from.</div>');
    ['#ru-save', '#ru-del'].forEach(s => { const b = $(s); if (b) b.remove(); });
  }
  const csel = $('#ru-contact');
  csel.innerHTML = contactOptions(d.contact_id || '', null, 'None');
  wireContactSelect(csel, 'supplier', () => {});

  let timer;
  const retest = () => { clearTimeout(timer); timer = setTimeout(runTest, 300); };
  const drawTokens = () => {
    const box = $('#ru-tokens');
    box.innerHTML = st.tokens.length ? st.tokens.map((t, i) => `<div class="token-chip" data-i="${i}">
      <span class="tk-kind">${i ? 'then ' : ''}${TOKEN_LABEL[t.t]}</span>
      ${t.t === 'text' ? `<input class="tk-v" value="${esc(t.v || '')}" placeholder="e.g. BEAN CAFE" aria-label="Text to look for">` : ''}
      ${t.t === 'number' || t.t === 'letters' ? `<label class="muted tk-sub">how many <input class="tk-count num" value="${t.count || ''}" placeholder="any" inputmode="numeric" aria-label="Exact count"></label>` : ''}
      ${t.t === 'date' ? `<input class="tk-fmt" list="ru-datefmts" value="${esc(t.fmt || '')}" aria-label="Date format" title="Pick a format or type your own using DD, MM, MMM, YY, YYYY">` : ''}
      <button type="button" class="tk-btn tk-left" ${i ? '' : 'disabled'} title="Move left" aria-label="Move left">←</button><button type="button" class="tk-btn tk-right" ${i < st.tokens.length - 1 ? '' : 'disabled'} title="Move right" aria-label="Move right">→</button><button type="button" class="tk-btn tk-rm" title="Remove" aria-label="Remove">×</button></div>`).join('')
      + `<datalist id="ru-datefmts">${DATE_FORMATS.map(f => `<option value="${f}">`).join('')}</datalist>`
      : '<div class="muted" style="font-size:13px">No parts yet. Add the text to look for, then a number, letters or a date if the bank adds one.</div>';
    $$('.token-chip', box).forEach(ch => {
      const i = +ch.dataset.i, t = st.tokens[i];
      const v = ch.querySelector('.tk-v'), c = ch.querySelector('.tk-count'), f = ch.querySelector('.tk-fmt');
      if (v) v.oninput = () => { t.v = v.value; retest(); };
      if (c) c.oninput = () => { const n = c.value.trim(); t.count = n === '' ? null : +n; retest(); };
      if (f) f.oninput = () => { t.fmt = f.value; retest(); };
      ch.querySelector('.tk-left').onclick = () => { [st.tokens[i - 1], st.tokens[i]] = [st.tokens[i], st.tokens[i - 1]]; drawTokens(); retest(); };
      ch.querySelector('.tk-right').onclick = () => { [st.tokens[i + 1], st.tokens[i]] = [st.tokens[i], st.tokens[i + 1]]; drawTokens(); retest(); };
      ch.querySelector('.tk-rm').onclick = () => { st.tokens.splice(i, 1); drawTokens(); retest(); };
    });
  };
  $$('.ru-add').forEach(b => b.onclick = () => {
    const t = { t: b.dataset.t };
    if (t.t === 'text') t.v = '';
    if (t.t === 'date') t.fmt = 'DD/MM/YYYY';
    st.tokens.push(t); drawTokens(); retest();
    const last = $$('.token-chip').pop(); const inp = last && last.querySelector('input'); if (inp) inp.focus();
  });
  const showParts = () => {
    $('#ru-textbox').hidden = !val('#ru-op');
    $('#ru-amtbox').hidden = !val('#ru-aop');
    $('#ru-banks').style.display = $('input[name=ru-scope][value=some]').checked ? 'flex' : 'none';
    const xfer = val('#ru-kind') === 'xfer';
    $('#ru-catbox').hidden = xfer; $('#ru-xferbox').hidden = !xfer;
  };
  $('#ru-kind').onchange = showParts;
  ['#ru-op', '#ru-aop', '#ru-dir'].forEach(s => { $(s).onchange = () => { showParts(); retest(); }; });
  $('#ru-amt').oninput = retest;
  $('#ru-amt').onblur = () => { const v = parseMoney(val('#ru-amt')); if (!isNaN(v) && val('#ru-amt')) $('#ru-amt').value = fmt(v); };
  $$('input[name=ru-scope], .ru-bank').forEach(x => { x.onchange = () => { showParts(); retest(); }; });
  $('#ru-sample').oninput = retest;

  // strict: refuse a missing amount (saving). Not strict: ignore the amount condition (testing while typing).
  const conditions = (strict = true) => {
    let amountOp = val('#ru-aop') || null;
    let amount = null;
    if (amountOp) {
      amount = parseMoney(val('#ru-amt'));
      if (isNaN(amount) || amount <= 0) {
        if (strict) throw new Error('Enter the dollar amount to compare against');
        amountOp = null; amount = null;
      }
    }
    const textOp = val('#ru-op') || null;
    return {
      text_op: textOp, text_tokens: textOp ? st.tokens : [], direction: val('#ru-dir'), amount_op: amountOp, amount,
      account_ids: $('input[name=ru-scope][value=some]').checked ? $$('.ru-bank').filter(x => x.checked).map(x => +x.value) : [],
    };
  };
  async function runTest() {
    const out = $('#ru-result');
    if (!out) return;
    const c = conditions(false);
    if (!c.text_op && !c.amount_op) { out.innerHTML = '<div class="hint">Add a bank text pattern or an amount to see what the rule matches.</div>'; return; }
    if (c.text_op && (!st.tokens.length || st.tokens.some(t => t.t === 'text' && !(t.v || '').trim()))) {
      out.innerHTML = '<div class="hint">Type the text to look for to see what the rule matches.</div>'; return;
    }
    try {
      const res = await api('POST', '/api/rules/test', { ...c, sample: val('#ru-sample') });
      const sample = val('#ru-sample').trim();
      const verdict = !c.text_op ? '<div class="hint">No text condition, so any bank text matches. The amount and direction still apply.</div>'
        : !sample ? '<div class="hint">Type an example above to check the text pattern.</div>'
        : res.sample_match ? '<div class="notice good" style="margin:0 0 10px">✓ This text matches the pattern.</div>'
        : '<div class="notice warn" style="margin:0 0 10px">✗ This text doesn\'t match the pattern.</div>';
      out.innerHTML = `${verdict}
        ${res.recent.length ? `<div class="hint" style="margin:10px 0 4px">Recent imported lines this rule would have caught:</div>
          <table><tbody>${res.recent.map(l => `<tr><td>${dfmt(l.date)}</td><td class="muted">${esc(l.account_name)}</td><td>${esc(l.bank_text)}</td><td class="num${l.amount < 0 ? ' neg' : ''}">${fmt(l.amount)}</td></tr>`).join('')}</tbody></table>`
          : '<div class="hint">None of your recently imported lines match.</div>'}
        ${res.regex ? `<details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:12.5px">Advanced: the pattern numbr uses</summary><code class="regex">${esc(res.regex)}</code></details>` : ''}`;
    } catch (e) { out.innerHTML = `<div class="notice warn" style="margin:0">${esc(e.message)}</div>`; }
  }
  if (!r && !pre) st.tokens.push({ t: 'text', v: '' });
  drawTokens(); showParts(); runTest();

  $('#ru-save').onclick = async () => {
    try {
      const c = conditions();
      if (c.account_ids.length === 0 && $('input[name=ru-scope][value=some]').checked) throw new Error('Tick at least one bank account, or choose All bank accounts');
      const xfer = val('#ru-kind') === 'xfer';
      const b = { ...c, name: val('#ru-name'), mode: $('input[name=ru-mode]:checked').value, target_account_id: val(xfer ? '#ru-to' : '#ru-target') || null,
        contact_id: csel.value || null, memo: val('#ru-memo'), active: $('#ru-active').checked };
      if (!b.target_account_id) throw new Error(xfer ? 'Choose the bank account the money moves to or from' : 'Choose the account to categorise these lines as');
      r ? await api('PUT', `/api/rules/${r.id}`, b) : await api('POST', '/api/rules', b);
      toast('Rule saved'); afterSave('#/rules');
    } catch (e) { fail(e); }
  };
  const del = $('#ru-del');
  if (del) del.onclick = async () => {
    if (!await confirmBox(`Delete the rule <b>${esc(r.name)}</b>? Transactions it already created are kept and still show the rule's name.`, 'Delete', true)) return;
    try { await api('DELETE', `/api/rules/${r.id}`); toast('Rule deleted'); go('#/rules'); } catch (e) { fail(e); }
  };
}

// ---------------------------------------------------------------- reports
const RF = {};
function reportHead(title, period) {
  return `<div class="report-title"><div class="biz-name">${esc(S.settings.business_name)}</div><h2>${title}</h2><div class="period">${period}</div></div>`;
}
// Report amounts: negatives in brackets, zero as a dash so the figures stand out.
const repCell = v => numCell(v, { paren: true, dashZero: true });
// Section heading / "None" rows: the label sits in its own cell so it stays in the frozen account column.
const repLabelRow = (label, cols, cls, tdCls = '') => `<tr class="${cls}"><td${tdCls ? ` class="${tdCls}"` : ''}>${label}</td><td colspan="${cols}"></td></tr>`;
// Scrolling table with the month header row and the account column frozen.
const repTable = (head, rowsHtml) => `<div class="table-wrap rep-grid"><table><thead>${head}</thead><tbody>${rowsHtml}</tbody></table></div>`;

// From/To month pickers, quick ranges and Groups only, shared by the P&L and Balance Sheet.
// Both default to this financial year (FY start to this month).
function monthRangeState(key) {
  RF[key] = RF[key] || { from: fyStartYM(S.today), to: S.today.slice(0, 7), groupsOnly: false };
  return RF[key];
}
function monthRangeFilters(key, hasGroups) {
  const cur = S.today.slice(0, 7), fy = fyStartYM(S.today), r = RF[key];
  return `<div class="filters">
    <div class="field"><label>From month</label><input type="month" id="f-from" value="${r.from}"></div>
    <div class="field"><label>To month</label><input type="month" id="f-to" value="${r.to}"></div>
    <div class="actions"><button class="small" data-r="${cur},${cur}">This month</button><button class="small" data-r="${addMonthsYM(cur, -1)},${addMonthsYM(cur, -1)}">Last month</button>
    <button class="small" data-r="${fy},${cur}">This financial year</button><button class="small" data-r="${addMonthsYM(fy, -12)},${addMonthsYM(fy, -1)}">Last financial year</button></div>
    ${hasGroups ? `<label class="check"><input type="checkbox" id="f-gonly" ${r.groupsOnly ? 'checked' : ''}> Groups only</label>` : ''}</div>`;
}
function wireMonthRange(key, redraw) {
  const r = RF[key];
  const apply = () => { r.from = val('#f-from') || r.from; r.to = val('#f-to') || r.to; redraw(); };
  $('#f-from').onchange = apply; $('#f-to').onchange = apply;
  const g = $('#f-gonly');
  if (g) g.onchange = () => { r.groupsOnly = g.checked; redraw(); };
  $$('[data-r]').forEach(b => b.onclick = () => { [r.from, r.to] = b.dataset.r.split(','); redraw(); });
}

async function reportPL() {
  const r = monthRangeState('pl');
  const d = await GET(`/api/reports/pl?from=${r.from}&to=${r.to}`);
  const multi = d.months.length > 1;
  const cols = d.months.length + (multi ? 1 : 0);
  const head = `<tr><th></th>${multi ? d.months.map(m => `<th class="num">${monthLabel(m)}</th>`).join('') : ''}<th class="num">${multi ? 'Total' : monthLabel(d.months[0])}</th></tr>`;
  const valsRow = (label, vals, total, cls = '', href = '', tdAttr = '') => `<tr class="${cls}${href ? ' click' : ''}" ${href ? `data-href="${href}"` : ''}><td ${tdAttr}>${label}</td>${multi ? vals.map(repCell).join('') : ''}${repCell(total)}</tr>`;
  const hasGroups = d.sections.some(s => groupsOfType(s.type).length);
  const lines = s => treeLines(groupTree(s.type, s.rows), hasGroups && !!r.groupsOnly);
  let rowsHtml = '';
  d.sections.forEach(s => {
    if (!s.rows.length && s.type === 'cos') return;
    rowsHtml += repLabelRow(s.title, cols, 'sechead');
    lines(s).forEach(l => {
      if (l.kind === 'row') rowsHtml += valsRow(esc(l.row.name), l.row.values, l.row.total, 'sub', `#/register/${l.row.account_id}?from=${d.from}&to=${d.to}`, treeCell(l, 28));
      else rowsHtml += valsRow(esc(l.group.name), l.values, l.total, `sub ${headClass(l)}`, '', treeCell(l, 28));
    });
    if (!s.rows.length) rowsHtml += repLabelRow('None', cols, 'sub', 'muted');
    rowsHtml += valsRow(`Total ${s.title.toLowerCase()}`, s.totals, s.total, 'total');
    if (s.type === 'cos' && s.rows.length) rowsHtml += valsRow('Gross profit', d.gross_profit, d.gross_profit_total, 'total');
  });
  rowsHtml += valsRow('Net profit', d.net_profit, d.net_profit_total, 'grand');
  page('Profit &amp; Loss', '', `<button id="x-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button>`,
    `${monthRangeFilters('pl', hasGroups)}
    <div class="report">${reportHead('Profit &amp; Loss', d.months.length === 1 ? monthLong(d.months[0]) : `${monthLong(d.months[0])} to ${monthLong(d.months[d.months.length - 1])}`)}
      ${repTable(head, rowsHtml)}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  wireMonthRange('pl', reportPL);
  $('#x-csv').onclick = () => {
    const out = [[S.settings.business_name], ['Profit & Loss'], [], ['Account', ...(multi ? d.months : []), multi ? 'Total' : d.months[0]]];
    d.sections.forEach(s => {
      out.push([s.title]);
      csvTreeLines(lines(s), x => x.name).forEach(([label, vals, total]) => out.push([label, ...(multi ? vals.map(c2d) : []), c2d(total)]));
      out.push([`Total ${s.title}`, ...(multi ? s.totals.map(c2d) : []), c2d(s.total)]);
    });
    out.push(['Net profit', ...(multi ? d.net_profit.map(c2d) : []), c2d(d.net_profit_total)]);
    download(`Profit-and-Loss-${r.from}-to-${r.to}.csv`, out);
  };
}

async function reportBS() {
  const r = monthRangeState('bs');
  const d = await GET(`/api/reports/bs?from=${r.from}&to=${r.to}`);
  const n = d.dates.length;
  // Same column headings as the P&L: one column per month, each the balance at that month's end.
  const head = `<tr><th></th>${d.months.map(m => `<th class="num">${monthLabel(m)}</th>`).join('')}</tr>`;
  const valsRow = (label, vals, cls = '', href = '', tdAttr = '') => `<tr class="${cls}${href ? ' click' : ''}" ${href ? `data-href="${href}"` : ''}><td ${tdAttr}>${label}</td>${vals.map(repCell).join('')}</tr>`;
  const hasGroups = d.sections.some(s => groupsOfType(s.type).length);
  const lines = s => treeLines(groupTree(s.type, s.rows), hasGroups && !!r.groupsOnly);
  const rowLabel = x => esc(x.name) + (x.computed ? ' <span class="muted">(this financial year)</span>' : '') + (x.partner ? ' <span class="muted">(the part of shared accounts, invoices and bills that belongs to the other owners)</span>' : '');
  const sectionRows = s => lines(s).forEach(l => {
    if (l.kind === 'row') rowsHtml += valsRow(rowLabel(l.row), l.row.values, 'sub', l.row.account_id ? `#/register/${l.row.account_id}?from=2000-01-01&to=${d.dates[n - 1]}` : '', treeCell(l, 28));
    else rowsHtml += valsRow(esc(l.group.name), l.values, `sub ${headClass(l)}`, '', treeCell(l, 28));
  });
  let rowsHtml = '';
  const [A, L, E] = d.sections;
  [A, L].forEach(s => {
    rowsHtml += repLabelRow(s.title, n, 'sechead');
    sectionRows(s);
    if (!s.rows.length) rowsHtml += repLabelRow('None', n, 'sub', 'muted');
    rowsHtml += valsRow(`Total ${s.title.toLowerCase()}`, s.totals, 'total');
  });
  rowsHtml += valsRow('Net assets', d.net_assets, 'grand');
  rowsHtml += repLabelRow(E.title, n, 'sechead');
  sectionRows(E);
  rowsHtml += valsRow('Total equity', E.totals, 'grand');
  const hist = E.rows.find(x => x.subtype === 'hist');
  const period = n === 1 ? `As at ${dfmt(d.dates[0])}` : `Month ends, ${monthLong(d.months[0])} to ${monthLong(d.months[n - 1])}`;
  page('Balance Sheet', '', `<button id="x-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button>`,
    `${monthRangeFilters('bs', hasGroups)}
    ${!d.balanced ? '<div class="notice bad">The balance sheet doesn\'t balance. This shouldn\'t happen. Please check the Trial Balance.</div>' : ''}
    ${hist && hist.values.some(v => v) ? '<div class="notice warn">Historical Balancing isn\'t zero. Your opening balances don\'t fully balance yet. <a href="#/opening">Review opening balances</a></div>' : ''}
    <div class="report">${reportHead('Balance Sheet', period)}${repTable(head, rowsHtml)}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  wireMonthRange('bs', reportBS);
  $('#x-csv').onclick = () => {
    const out = [[S.settings.business_name], ['Balance Sheet'], [], ['Account', ...d.dates]];
    d.sections.forEach(s => {
      out.push([s.title]);
      csvTreeLines(lines(s), x => x.name).forEach(([label, vals]) => out.push([label, ...vals.map(c2d)]));
      out.push([`Total ${s.title}`, ...s.totals.map(c2d)]);
    });
    out.push(['Net assets', ...d.net_assets.map(c2d)]);
    download(`Balance-Sheet-${r.from}-to-${r.to}.csv`, out);
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
    <div class="report">${reportHead('Trial Balance', `As at ${dfmt(d.as_at)}`)}<table><thead><tr><th>Account</th><th>Type</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>
    ${d.rows.map(r => `<tr${r.account_id ? ` class="click" data-href="#/register/${r.account_id}?from=2000-01-01&to=${d.as_at}"` : ''}><td>${esc(r.name)}</td><td class="muted">${TYPE_LABEL[r.type]}</td><td class="num">${fmt(r.debit, { blankZero: true })}</td><td class="num">${fmt(r.credit, { blankZero: true })}</td></tr>`).join('')}
    <tr class="grand"><td colspan="2">Total</td><td class="num">${fmt(d.debit)}</td><td class="num">${fmt(d.credit)}</td></tr></tbody></table></div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#f-at').onchange = () => { RF.tbAt = val('#f-at'); reportTB(); };
  $('#x-csv').onclick = () => download(`Trial-Balance-${d.as_at}.csv`, [['Account', 'Type', 'Debit', 'Credit'], ...d.rows.map(r => [r.name, TYPE_LABEL[r.type], c2d(r.debit), c2d(r.credit)]), ['Total', '', c2d(d.debit), c2d(d.credit)]]);
}

// ---------------------------------------------------------------- budgets
// The builder asks, per P&L account, how to make its budget. The server does all the maths (preview) and
// keeps those choices only to set up next year's builder. A built budget is plain monthly figures that
// are edited by hand on the budget page.
const BUDGET_METHOD = { average: 'Monthly average', last: 'Last month', schedule: 'My own amounts', manual: 'Type each month', none: "Don't budget" };
const BUDGET_SECTIONS = [['income', 'Income'], ['cos', 'Cost of Sales'], ['expense', 'Expenses']];
const SCHED_FREQ = { monthly: 'Every month', yearly: 'Total for the year', weekly: 'Every week', fortnightly: 'Every fortnight' };
const BY_MONTH = f => f === 'monthly' || f === 'yearly';  // segments with from/to months rather than dates
const SCHED_PER = { monthly: 'a month', weekly: 'a week', fortnightly: 'a fortnight' };
const BUDGET_METHOD_SHORT = { average: 'average', last: 'last month', schedule: 'own amounts', manual: 'typed' };
const PL_TYPE_SET = new Set(['income', 'cos', 'expense']);
const sumC = v => v.reduce((a, b) => a + b, 0);
// Twelve months as a compact grid: each cell is a month label over a value (or an input).
const monthGrid = (months, cell) => `<div class="mgrid">${months.map((m, i) => `<div><span>${monthLabel(m)}</span>${cell(i, m)}</div>`).join('')}</div>`;

async function budgetList() {
  const list = await GET('/api/budgets');
  page('Budgets', 'What you expect to earn and spend each month of a financial year', `<a class="btn primary" href="#/budgets/new">Build a budget</a>`,
    `<div class="panel flush">${list.length ? `<div class="table-wrap"><table><thead><tr><th>Budget</th><th>Year</th><th class="num">Income</th><th class="num">Expenses</th><th class="num">Net profit</th></tr></thead><tbody>
    ${list.map(b => `<tr class="click" data-href="#/budgets/${b.id}"><td>${esc(b.name)}</td><td>${esc(b.label)}</td>${numCell(b.income)}${numCell(b.expenses)}${numCell(b.net_profit)}</tr>`).join('')}
    </tbody></table></div>` : `<div class="empty">No budgets yet.<br>A budget is your plan for the year: how much you expect to earn and spend each month.
      The builder walks you through it one account at a time.</div>`}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

// Builder state: d = what the server sent, choice = {account_id: {method, config}}, preview = last server preview.
let BB = null;
async function budgetBuilder() {
  await refreshState();
  const fy = QS.get('fy');
  const d = await GET('/api/budgets/builder' + (fy ? `?fy=${encodeURIComponent(fy)}` : ''));
  // Each account starts with the server's suggestion: last year's choice where there is one,
  // else the monthly average when it has history, else not budgeted.
  BB = { d, step: 1, preview: null, name: d.name,
    choice: Object.fromEntries(d.accounts.map(a => [a.account_id, { method: a.method, config: a.config }])) };
  drawBuilder();
}
const builderChoices = () => BB.d.accounts.map(a => ({ account_id: a.account_id, ...BB.choice[a.account_id] }));
async function builderPreview() {
  BB.preview = await api('POST', '/api/budgets/preview', { fy: BB.d.fy, accounts: builderChoices() });
  BB.prev = Object.fromEntries(BB.preview.accounts.map(a => [a.account_id, a]));
}
function scheduleSummary(cfg, months) {
  if (!cfg || !cfg.length) return '';
  return cfg.map(s => {
    const amt = `${$fmt(s.amount)} ${SCHED_PER[s.freq]}`;
    if (s.freq === 'yearly') {
      const n = months.indexOf(s.to) - months.indexOf(s.from) + 1;
      return `${$fmt(s.amount)} spread evenly over ${n === 12 ? 'the year' : `${monthLabel(s.from)} – ${monthLabel(s.to)}`}`;
    }
    if (s.freq === 'monthly') return s.from === months[0] && s.to === months[11] ? amt : `${amt}, ${monthLabel(s.from)} – ${monthLabel(s.to)}`;
    return `${amt} from ${dfmt(s.first)}${s.to ? ` to ${dfmt(s.to)}` : ''}`;
  }).join('; then ');
}

function drawBuilder() {
  const d = BB.d, st = BB.step;
  const steps = `<div class="steps">${[1, 2, 3].map(i => `<span class="${i <= st ? 'on' : ''}"></span>`).join('')}</div>`;
  const head = (t, sub) => page(`Build a budget`, `${esc(d.label)} · step ${st} of 3`, `<a class="btn" href="#/budgets">Cancel</a>`, `<div class="${st < 2 ? 'setup' : ''}">${steps}<div class="panel" id="bb">${t ? `<h2>${t}</h2>` : ''}${sub ? `<p class="muted">${sub}</p>` : ''}<div id="bb-body"></div></div></div>`);
  const foot = (back, next, nextLabel = 'Continue') => `<div class="modal-foot">${back ? '<button id="bb-back">Back</button>' : ''}<button class="primary" id="bb-next">${nextLabel}</button></div>`;
  const wire = (onNext) => {
    const b = $('#bb-back'); if (b) b.onclick = () => { BB.step--; drawBuilder(); };
    $('#bb-next').onclick = onNext;
  };
  if (st === 1) {
    head('Which year is this budget for?', 'A budget is your plan for the year: what you expect to earn and spend each month. Later you can compare it with what really happens.');
    const opts = d.years.map(y => `<option value="${y.fy}" ${y.fy === d.fy ? 'selected' : ''} ${y.budget_id ? 'disabled' : ''}>${esc(y.label)} (${monthLabel(y.fy)} – ${monthLabel(addMonthsYM(y.fy, 11))})${y.budget_id ? ' – already has a budget' : ''}</option>`).join('');
    $('#bb-body').innerHTML = `<div class="field"><label>Financial year</label><select id="bb-fy">${opts}</select></div>${foot(false, true)}`;
    $('#bb-fy').onchange = () => go(`#/budgets/new?fy=${val('#bb-fy')}`);
    wire(async () => { try { await builderPreview(); BB.step = 2; drawBuilder(); } catch (e) { fail(e); } });
  } else if (st === 2) {
    head('Choose how to budget each account', d.history
      ? `Each account starts with a suggestion: ${d.prior ? `the method you used for ${esc(d.prior.label)}, or ` : ''}its monthly average from ${monthLong(d.history.from)} to ${monthLong(d.history.to)}. Change any of them; the yearly total updates as you go.`
      : 'Pick a method for each account. Choose "My own amounts" for amounts that repeat (monthly, weekly or fortnightly) or a total for the year, or "Type each month" to enter all 12 months.');
    drawBuilderAccounts();
  } else {
    head('Check your budget', 'This is your budget month by month. Once it is built you can still change any figure by hand.');
    const p = BB.preview;
    const rowsFor = type => d.accounts.filter(a => a.type === type && BB.choice[a.account_id].method !== 'none')
      .map(a => ({ account_id: a.account_id, name: a.name, group_id: a.group_id, values: BB.prev[a.account_id].values, total: BB.prev[a.account_id].total }));
    const t = p.totals, gp = t.income.map((v, i) => v - t.cos[i]);
    $('#bb-body').innerHTML = `<div class="field" style="max-width:360px"><label>Budget name</label><input id="bb-name" value="${esc(BB.name)}"></div>
      <div class="report">${budgetTable(p.months, BUDGET_SECTIONS.map(([type, title]) => ({ type, title, rows: rowsFor(type), totals: t[type] })), gp, t.net_profit)}</div>
      ${foot(true, true, 'Build budget')}`;
    $('#bb-name').oninput = () => { BB.name = val('#bb-name'); };
    wire(async () => {
      try {
        const res = await api('POST', '/api/budgets', { fy: d.fy, name: BB.name, accounts: builderChoices() });
        toast('Budget built'); BB = null; go(`#/budgets/${res.id}`);
      } catch (e) { fail(e); }
    });
  }
}

function drawBuilderAccounts() {
  const d = BB.d, h = d.history, p = BB.prev;
  const opt = (a, k) => {
    const c = BB.choice[a.account_id];
    const extra = k === 'average' ? ` (${$fmt(a.average)})` : k === 'last' ? ` (${$fmt(a.last)})` : '';
    return `<option value="${k}" ${c.method === k ? 'selected' : ''}>${BUDGET_METHOD[k]}${extra}</option>`;
  };
  const methods = h ? Object.keys(BUDGET_METHOD) : ['schedule', 'manual', 'none'];
  const cols = h ? 4 : 2;  // columns after the account name
  let body = '';
  BUDGET_SECTIONS.forEach(([type, title]) => {
    const rs = d.accounts.filter(a => a.type === type).map(a => ({ ...a, values: [p[a.account_id].total], total: p[a.account_id].total }));
    if (!rs.length) return;
    body += repLabelRow(title, cols, 'sechead');
    treeLines(groupTree(type, rs)).forEach(l => {
      if (l.kind === 'head') { body += `<tr class="sub ${headClass(l)}"><td ${treeCell(l, 28)}>${esc(l.group.name)}</td>${h ? '<td></td><td></td>' : ''}<td></td>${numCell(l.total)}</tr>`; return; }
      const a = l.row, c = BB.choice[a.account_id], pr = p[a.account_id];
      const own = c.method === 'schedule' || c.method === 'manual';
      const note = own && !c.config ? '<div class="hint neg">Click Set up… to enter the amounts</div>'
        : pr.error ? `<div class="hint neg">${esc(pr.error)}</div>` : c.method === 'schedule' ? `<div class="hint">${esc(scheduleSummary(c.config, d.months))}</div>` : '';
      body += `<tr class="sub" data-a="${a.account_id}"><td ${treeCell(l, 28)}>${esc(a.name)}${a.from_prior ? ' <span class="pill" title="Same choice as last year">as last year</span>' : ''}</td>
        ${h ? `<td class="num muted">${a.has_history ? fmt(a.average) : '–'}</td><td class="num muted" title="${a.last_month ? monthLong(a.last_month) : ''}">${a.last_month ? `${fmt(a.last)} <span class="small-note">${monthLabel(a.last_month)}</span>` : '–'}</td>` : ''}
        <td><div class="inline"><select class="bb-method" style="width:auto">${methods.map(k => opt(a, k)).join('')}</select>${own ? `<button class="small bb-setup">${c.config ? 'Change…' : 'Set up…'}</button>` : ''}</div>${note}</td>
        ${numCell(pr.total)}</tr>`;
    });
  });
  const t = BB.preview.totals;
  const yr = k => sumC(t[k]);
  body += `<tr class="total"><td>Total income</td>${h ? '<td></td><td></td>' : ''}<td></td>${numCell(yr('income'))}</tr>
    <tr class="total"><td>Total costs and expenses</td>${h ? '<td></td><td></td>' : ''}<td></td>${numCell(yr('cos') + yr('expense'))}</tr>
    <tr class="grand"><td>Net profit</td>${h ? '<td></td><td></td>' : ''}<td></td>${numCell(sumC(t.net_profit))}</tr>`;
  const errors = BB.preview.accounts.filter(a => a.error).length;
  $('#bb-body').innerHTML = `<div class="filters"><div class="field"><label>Set every account to</label><select id="bb-all" style="width:auto"><option value="">Choose…</option>
      ${h ? '<option value="average">Monthly average</option><option value="last">Last month</option>' : ''}<option value="none">Don't budget</option></select></div>
      <div class="hint" style="margin:0 0 8px">Tip: "My own amounts" handles things like "$100 a month until October, then $110", a weekly amount from a start date, or a total for the year spread evenly.</div></div>
    <div class="table-wrap"><table class="bb-table"><thead><tr><th>Account</th>${h ? '<th class="num">Average a month</th><th class="num">Last month</th>' : ''}<th>How to budget</th><th class="num">Year total</th></tr></thead>
    <tbody>${body}</tbody></table></div>
    ${errors ? `<div class="notice warn" style="margin-top:12px">${errors} account${errors > 1 ? 's need' : ' needs'} attention (see the red notes above) before you can continue.</div>` : ''}
    <div class="modal-foot"><button id="bb-back">Back</button><button class="primary" id="bb-next" ${errors ? 'disabled' : ''}>Continue</button></div>`;
  const refresh = async () => { try { await builderPreview(); drawBuilderAccounts(); } catch (e) { fail(e); } };
  $('#bb-back').onclick = () => { BB.step = 1; drawBuilder(); };
  $('#bb-next').onclick = () => { BB.step = 3; drawBuilder(); };
  $('#bb-all').onchange = () => {
    const m = val('#bb-all'); if (!m) return;
    d.accounts.forEach(a => { BB.choice[a.account_id] = { method: m, config: null }; });
    refresh();
  };
  $$('tr[data-a]').forEach(tr => {
    const aid = +tr.dataset.a, a = d.accounts.find(x => x.account_id === aid);
    tr.querySelector('.bb-method').onchange = e => {
      const m = e.target.value, c = BB.choice[aid];
      const keep = m === c.method || (m === 'schedule' && Array.isArray(c.config)) || (m === 'manual' && c.config && c.config.amounts);
      BB.choice[aid] = { method: m, config: keep ? c.config : null };
      refresh().then(() => { if ((m === 'schedule' || m === 'manual') && !BB.choice[aid].config) ownAmountsModal(a, refresh); });
    };
    const b = tr.querySelector('.bb-setup');
    if (b) b.onclick = () => ownAmountsModal(a, refresh);
  });
}

// Modal for "My own amounts" (a list of repeating amounts) or "Type each month" (12 inputs).
function ownAmountsModal(a, done) {
  const d = BB.d, months = d.months, c = BB.choice[a.account_id], manual = c.method === 'manual';
  let segs = !manual && Array.isArray(c.config) ? c.config.map(s => ({ ...s })) : [];
  let amounts = manual && c.config ? [...c.config.amounts] : (BB.prev[a.account_id] ? [...BB.prev[a.account_id].values] : Array(12).fill(0));
  if (!manual && !segs.length) segs = [{ freq: 'monthly', amount: null, from: months[0], to: months[11] }];
  const monthSel = (cls, v) => `<select class="${cls}">${months.map(m => `<option value="${m}" ${m === v ? 'selected' : ''}>${monthLabel(m)}</option>`).join('')}</select>`;
  const segRow = (s, i) => `<div class="seg" data-i="${i}">
      <div class="field"><label>How often</label><select class="s-freq">${Object.entries(SCHED_FREQ).map(([k, v]) => `<option value="${k}" ${s.freq === k ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
      <div class="field"><label>${s.freq === 'yearly' ? 'Total amount' : 'Amount'}</label><input class="s-amt num" inputmode="decimal" placeholder="0.00" value="${s.amount === null || s.amount === undefined ? '' : fmt(s.amount)}"></div>
      ${BY_MONTH(s.freq)
        ? `<div class="field"><label>From</label>${monthSel('s-from', s.from || months[0])}</div><div class="field"><label>To</label>${monthSel('s-to', s.to || months[11])}</div>`
        : `<div class="field"><label>First date</label><input type="date" class="s-first" value="${s.first || ''}"></div><div class="field"><label>Until (optional)</label><input type="date" class="s-until" value="${s.to || ''}"></div>`}
      <div class="field"><label>&nbsp;</label><button class="small danger s-del" title="Remove this amount" ${segs.length < 2 ? 'disabled' : ''}>Remove</button></div></div>`;
  // Read the form back into segs/amounts. strict: throw on anything unfinished (when saving).
  const read = (el, strict) => {
    if (manual) {
      amounts = [...el.querySelectorAll('.m-amt')].map((inp, i) => {
        const v = parseMoney(inp.value);
        if (isNaN(v)) { if (strict) throw new Error(`${monthLong(months[i])}: enter an amount like 120.50`); return 0; }
        return v;
      });
      return { amounts };
    }
    segs = [...el.querySelectorAll('.seg')].map((row, i) => {
      const q = s => row.querySelector(s), freq = q('.s-freq').value, raw = q('.s-amt').value;
      const amount = raw.trim() === '' ? null : parseMoney(raw);
      if (strict && (amount === null || isNaN(amount))) throw new Error(`Amount ${i + 1}: enter an amount like 120.50`);
      return BY_MONTH(freq) ? { freq, amount: isNaN(amount) ? null : amount, from: q('.s-from') ? q('.s-from').value : months[0], to: q('.s-to') ? q('.s-to').value : months[11] }
        : { freq, amount: isNaN(amount) ? null : amount, first: q('.s-first') ? q('.s-first').value : '', ...(q('.s-until') && q('.s-until').value ? { to: q('.s-until').value } : {}) };
    });
    return segs.map(s => ({ ...s, amount: s.amount || 0 }));
  };
  const title = manual ? 'Type each month' : 'My own amounts';
  modal(`<h2>${esc(a.name)}: ${title}</h2>
    ${manual ? `<p class="hint">Enter what you expect for each month of ${esc(d.label)}.</p><div id="oa-body"></div>
        <div class="actions" style="margin-top:8px"><button class="small" id="oa-copy">Copy ${monthLabel(months[0])} to every month</button></div>`
      : `<p class="hint">Add each amount and how often it happens. For example: $100 every month from July to October, then another line for $110 every month from November. "Total for the year" splits an amount evenly across the months you choose (e.g. $1,200 becomes $100 a month). Weekly and fortnightly amounts start on the first date you give and land in the month they fall in.</p>
        <div id="oa-body"></div><button class="small" id="oa-add">+ Add another amount</button>`}
    ${manual ? '<div class="hint" style="margin-top:10px">Year total: <b id="oa-total"></b></div>'
      : '<h2 style="margin-top:16px;font-size:14px">What this gives you</h2><div id="oa-prev" class="muted">…</div>'}
    <div class="modal-foot"><button id="oa-cancel">Cancel</button><button class="primary" id="oa-ok">Use these amounts</button></div>`, el => {
    const body = el.querySelector('#oa-body');
    let timer;
    const preview = () => {
      // Typed months need no preview (it would repeat what was typed): just keep the year total current.
      if (manual) { el.querySelector('#oa-total').textContent = $fmt(sumC(read(el, false).amounts)); return; }
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const cfg = read(el, false);
          const res = await api('POST', '/api/budgets/preview', { fy: d.fy, accounts: [{ account_id: a.account_id, method: manual ? 'manual' : 'schedule', config: cfg }] });
          const r = res.accounts[0];
          el.querySelector('#oa-prev').innerHTML = r.error ? `<div class="notice warn">${esc(r.error)}</div>`
            : `${monthGrid(months, i => `<b class="${r.values[i] < 0 ? 'neg' : ''}">${fmt(r.values[i])}</b>`)}<div class="hint">Year total: <b>${$fmt(r.total)}</b></div>`;
        } catch (e) { el.querySelector('#oa-prev').innerHTML = `<div class="notice warn">${esc(e.message)}</div>`; }
      }, 250);
    };
    const draw = () => {
      body.innerHTML = manual ? monthGrid(months, i => `<input class="m-amt num" inputmode="decimal" value="${amounts[i] ? fmt(amounts[i]) : ''}" placeholder="0.00">`)
        : segs.map(segRow).join('');
      body.querySelectorAll('input, select').forEach(x => { x.oninput = preview; x.onchange = preview; });
      body.querySelectorAll('.s-freq').forEach(x => x.onchange = () => {
        read(el, false);
        const s = segs[+x.closest('.seg').dataset.i];
        s.freq = x.value;
        if (!BY_MONTH(s.freq) && !s.first) s.first = `${months[0]}-01`;
        draw(); preview();
      });
      body.querySelectorAll('.s-del').forEach(x => x.onclick = () => { read(el, false); segs.splice(+x.closest('.seg').dataset.i, 1); draw(); preview(); });
    };
    draw(); preview();
    const add = el.querySelector('#oa-add');
    if (add) add.onclick = () => {
      read(el, false);
      const last = segs[segs.length - 1];
      const from = last && BY_MONTH(last.freq) && last.to < months[11] ? addMonthsYM(last.to, 1) : months[0];
      segs.push({ freq: 'monthly', amount: null, from, to: months[11] });
      draw(); preview();
      const inps = body.querySelectorAll('.s-amt'); inps[inps.length - 1].focus();
    };
    const copy = el.querySelector('#oa-copy');
    if (copy) copy.onclick = () => { const inps = body.querySelectorAll('.m-amt'); inps.forEach(x => { x.value = inps[0].value; }); preview(); };
    el.querySelector('#oa-cancel').onclick = () => { closeModal(); done(); };
    el.querySelector('#oa-ok').onclick = async () => {
      try {
        const method = manual ? 'manual' : 'schedule', config = read(el, true);
        const res = await api('POST', '/api/budgets/preview', { fy: d.fy, accounts: [{ account_id: a.account_id, method, config }] });
        if (res.accounts[0].error) throw new Error(res.accounts[0].error);
        BB.choice[a.account_id] = { method, config }; closeModal(); done();
      } catch (e) { fail(e); }
    };
  });
}

// Budget figures laid out like the P&L. Sections: [{type, title, rows: [{account_id, name, group_id, values, total}], totals}].
// cell(row, i) draws an account's month cell (read-only by default); keys let the budget page update totals in place.
function budgetTable(months, sections, gp, np, cell = (r, i) => repCell(r.values[i]), nameExtra = () => '') {
  const n = months.length + 1;
  const vc = (key, vals) => vals.map((v, i) => `<td class="num${v < 0 ? ' neg' : ''}" data-k="${key}:${i}">${fmt(v, { paren: true, dashZero: true })}</td>`).join('') +
    `<td class="num" data-k="${key}:12">${fmt(sumC(vals), { paren: true, dashZero: true })}</td>`;
  let h = '';
  sections.forEach(s => {
    if (!s.rows.length && s.type === 'cos') return;
    h += repLabelRow(s.title, n, 'sechead');
    treeLines(groupTree(s.type, s.rows)).forEach(l => {
      if (l.kind === 'head') h += `<tr class="sub ${headClass(l)}"><td ${treeCell(l, 28)}>${esc(l.group.name)}</td>${vc(`g${l.group.id}`, l.values)}</tr>`;
      else h += `<tr class="sub" data-a="${l.row.account_id}"><td ${treeCell(l, 28)}>${esc(l.row.name)}${nameExtra(l.row)}</td>${months.map((_, i) => cell(l.row, i)).join('')}<td class="num" data-k="a${l.row.account_id}:12">${fmt(l.row.total, { paren: true, dashZero: true })}</td></tr>`;
    });
    if (!s.rows.length) h += repLabelRow('None', n, 'sub', 'muted');
    h += `<tr class="total"><td>Total ${s.title.toLowerCase()}</td>${vc(`s${s.type}`, s.totals)}</tr>`;
    if (s.type === 'cos' && s.rows.length) h += `<tr class="total"><td>Gross profit</td>${vc('gp', gp)}</tr>`;
  });
  h += `<tr class="grand"><td>Net profit</td>${vc('np', np)}</tr>`;
  return repTable(`<tr><th></th>${months.map(m => `<th class="num">${monthLabel(m)}</th>`).join('')}<th class="num">Total</th></tr>`, h);
}

async function budgetView(id) {
  await refreshState();
  const b = await GET(`/api/budgets/${id}`);
  // Model: account rows per section with their 12 values; edits change it and mark the cell dirty.
  const rowsOf = {}, dirty = new Map();
  b.sections.forEach(s => { rowsOf[s.type] = s.rows.map(r => ({ ...r, values: [...r.values] })); });
  const allRows = () => Object.values(rowsOf).flat();
  const sections = () => BUDGET_SECTIONS.map(([type, title]) => {
    const rows = rowsOf[type].map(r => ({ ...r, total: sumC(r.values) }));
    return { type, title, rows, totals: b.months.map((_, i) => sumC(rows.map(r => r.values[i]))) };
  });
  const profit = secs => {
    const t = Object.fromEntries(secs.map(s => [s.type, s.totals]));
    const gp = t.income.map((v, i) => v - t.cos[i]);
    return [gp, gp.map((v, i) => v - t.expense[i])];
  };
  const inBudget = () => new Set(allRows().map(r => r.account_id));
  const draw = () => {
    const secs = sections(), [gp, np] = profit(secs);
    page(esc(b.name), `${esc(b.label)} · ${monthLong(b.months[0])} to ${monthLong(b.months[11])}`,
      `<a class="btn" href="#/budgets">All budgets</a><button id="bv-rename">Rename</button><button id="bv-csv">Export CSV</button><button onclick="window.print()">Print / PDF</button><button class="danger" id="bv-del">Delete</button><button class="primary" id="bv-save" disabled>Save changes</button>`,
      `<div class="notice info no-print">Change any figure by typing over it, then <b>Save changes</b>. Use <b>⇉</b> next to an account to copy its first month to every month.
        Changes here don't affect how next year's builder starts: that uses the choices you made when you built this budget${b.created_at ? ` on ${dfmt(b.created_at.slice(0, 10))}` : ''}.</div>
      <div class="filters no-print"><div class="field"><label>Add an account to this budget</label><select class="acct-pick" id="bv-acct">${acctOptions('', a => PL_TYPE_SET.has(a.type) && !inBudget().has(a.id) && !a.archived, 'Choose account…')}</select></div>
        <button id="bv-add">Add</button></div>
      <div class="report">${reportHead(esc(b.name), `${monthLong(b.months[0])} to ${monthLong(b.months[11])}`)}
      ${budgetTable(b.months, secs, gp, np,
        (r, i) => `<td class="num bcell"><input class="num" data-a="${r.account_id}" data-i="${i}" value="${r.values[i] ? fmt(r.values[i]) : ''}" placeholder="–" aria-label="${esc(r.name)} ${monthLabel(b.months[i])}"><span class="print-only">${fmt(r.values[i], { paren: true, dashZero: true })}</span></td>`,
        r => ` <button class="link small no-print bv-fill" data-a="${r.account_id}" title="Copy the first month to every month">⇉</button>${r.method && r.method !== 'none' ? ` <span class="small-note no-print" title="How it was built">${BUDGET_METHOD_SHORT[r.method] || ''}</span>` : ''}`)}</div>`);
    wire();
  };
  const recalc = () => {
    const secs = sections(), [gp, np] = profit(secs), set = (k, v) => { const td = $(`[data-k="${k}"]`); if (td) { td.textContent = fmt(v, { paren: true, dashZero: true }); td.classList.toggle('neg', v < 0); } };
    const put = (key, vals) => { vals.forEach((v, i) => set(`${key}:${i}`, v)); set(`${key}:12`, sumC(vals)); };
    secs.forEach(s => {
      put(`s${s.type}`, s.totals);
      s.rows.forEach(r => set(`a${r.account_id}:12`, r.total));
      treeLines(groupTree(s.type, s.rows)).forEach(l => { if (l.kind === 'head') put(`g${l.group.id}`, l.values); });
    });
    put('gp', gp); put('np', np);
    $('#bv-save').disabled = !dirty.size;
  };
  // Draw the page again from the model, keeping unsaved edits marked.
  const redraw = () => {
    draw();
    dirty.forEach((_, k) => { const [aid, i] = k.split(':'); const inp = $(`.bcell input[data-a="${aid}"][data-i="${i}"]`); if (inp) inp.classList.add('changed'); });
    recalc();
  };
  const rowById = aid => allRows().find(r => r.account_id === aid);
  const setCell = (inp, cents) => {
    const r = rowById(+inp.dataset.a), i = +inp.dataset.i;
    r.values[i] = cents;
    dirty.set(`${r.account_id}:${i}`, { account_id: r.account_id, month: b.months[i], amount: cents });
    inp.classList.add('changed');
  };
  const wire = () => {
    $$('.bcell input').forEach(inp => {
      inp.onchange = () => {
        const v = parseMoney(inp.value);
        if (isNaN(v)) { inp.classList.add('bad'); toast('Enter an amount like 120.50', true); return; }
        inp.classList.remove('bad');
        setCell(inp, v);
        inp.value = v ? fmt(v) : '';
        recalc();
      };
      inp.onfocus = () => inp.select();
      inp.onkeydown = e => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const all = $$(`.bcell input[data-i="${inp.dataset.i}"]`), k = all.indexOf(inp) + (e.shiftKey ? -1 : 1);
        if (all[k]) all[k].focus();
      };
    });
    $$('.bv-fill').forEach(btn => btn.onclick = () => {
      const ins = $$(`.bcell input[data-a="${btn.dataset.a}"]`);
      const v = parseMoney(ins[0].value);
      if (isNaN(v)) return toast('Fix the first month first', true);
      ins.forEach(inp => { inp.value = v ? fmt(v) : ''; inp.classList.remove('bad'); setCell(inp, v); });
      recalc();
    });
    $('#bv-add').onclick = () => {
      const a = acct(val('#bv-acct'));
      if (!a) return toast('Choose an account to add', true);
      rowsOf[a.type].push({ account_id: a.id, name: a.name, group_id: a.group_id, values: Array(12).fill(0), method: null });
      redraw();
      const first = $(`.bcell input[data-a="${a.id}"]`); if (first) first.focus();
    };
    $('#bv-save').onclick = async () => {
      if ($('.bcell input.bad')) return toast('Some amounts are not valid (shown in red)', true);
      try {
        await api('PUT', `/api/budgets/${b.id}`, { amounts: [...dirty.values()] });
        toast('Budget saved'); budgetView(b.id);
      } catch (e) { fail(e); }
    };
    $('#bv-rename').onclick = () => modal(`<h2>Rename budget</h2><div class="field"><label>Name</label><input id="bv-name" value="${esc(b.name)}"></div>
      <div class="modal-foot"><button onclick="closeModal()">Cancel</button><button class="primary" id="bv-name-ok">Save</button></div>`, el => {
      el.querySelector('#bv-name-ok').onclick = async () => {
        try {
          const name = el.querySelector('#bv-name').value;
          await api('PUT', `/api/budgets/${b.id}`, { name });
          b.name = name.trim(); closeModal(); toast('Renamed'); redraw();
        } catch (e) { fail(e); }
      };
    });
    $('#bv-csv').onclick = () => {
      const secs = sections(), [gp, np] = profit(secs);
      const out = [[S.settings.business_name], [b.name], [], ['Account', ...b.months, 'Total']];
      secs.forEach(s => {
        if (!s.rows.length && s.type === 'cos') return;
        out.push([s.title]);
        csvTreeLines(treeLines(groupTree(s.type, s.rows)), x => x.name).forEach(([label, vals, total]) => out.push([label, ...vals.map(c2d), c2d(total)]));
        out.push([`Total ${s.title}`, ...s.totals.map(c2d), c2d(sumC(s.totals))]);
        if (s.type === 'cos' && s.rows.length) out.push(['Gross profit', ...gp.map(c2d), c2d(sumC(gp))]);
      });
      out.push(['Net profit', ...np.map(c2d), c2d(sumC(np))]);
      download(`${b.name.replace(/[^\w -]+/g, '')}.csv`, out);
    };
    $('#bv-del').onclick = async () => {
      if (!await confirmBox(`Delete <b>${esc(b.name)}</b>? Its figures and the choices you made when building it are removed. You can then build ${esc(b.label)} again from the start.`, 'Delete budget', true)) return;
      try { await api('DELETE', `/api/budgets/${b.id}`); toast('Budget deleted'); go('#/budgets'); } catch (e) { fail(e); }
    };
  };
  draw();
}
// ---------------------------------------------------------------- contacts
async function contactsList() {
  await refreshState();
  const f = QS.get('kind') || 'all';
  const list = S.contacts.filter(c => (f === 'all' || c.kind === f || c.kind === 'both') && (QS.get('archived') ? true : !c.archived));
  page('Contacts', '', `<a class="btn primary" href="#/contacts/new">New contact</a>`,
    `<div class="tabs">${[['all', 'All'], ['customer', 'Customers'], ['supplier', 'Suppliers']].map(([k, l]) => `<a href="#/contacts?kind=${k}" class="${k === f ? 'active' : ''}">${l}</a>`).join('')}</div>
    <div class="panel flush">${list.length ? `<table><thead><tr><th>Name</th><th>Type</th><th>Phone</th><th class="num">Owes you</th><th class="num">You owe</th><th class="num">Contribution due</th></tr></thead><tbody>
    ${list.map(c => `<tr class="click" data-href="#/contacts/${c.id}"><td>${esc(c.name)}${c.archived ? ' <span class="pill">archived</span>' : ''}</td><td>${c.kind[0].toUpperCase() + c.kind.slice(1)}</td><td>${esc(c.phone || '')}</td>
      <td class="num">${fmt(c.receivable, { blankZero: true })}</td><td class="num">${fmt(c.payable, { blankZero: true })}</td><td class="num">${fmt(c.contribution_due, { blankZero: true })}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No contacts yet</div>'}</div>`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
}

async function contactForm(id) {
  const c = id ? contactById(id) : null;
  if (id && !c) throw new Error('Contact not found');
  const [sales, bills, contribs] = c ? await Promise.all(['sale', 'bill', 'partner'].map(k => GET(`/api/invoices?kind=${k}&status=all&contact_id=${c.id}`))) : [[], [], []];
  const items = [...sales, ...bills, ...contribs].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 30);
  page(c ? esc(c.name) : 'New contact', '', `<a class="btn" href="#/contacts">Cancel</a>${c ? '<button class="danger" id="c-del">Delete</button>' : ''}<button class="primary" id="c-save">Save</button>`,
    `<div class="panel"><div class="form-grid">
      <div class="field"><label>Name</label><input id="c-name" value="${esc(c ? c.name : '')}"></div>
      <div class="field"><label>Type</label><select id="c-kind">${['customer', 'supplier', 'both'].map(k => `<option value="${k}" ${(c ? c.kind : (QS.get('kind') || 'customer')) === k ? 'selected' : ''}>${k[0].toUpperCase() + k.slice(1)}</option>`).join('')}</select></div>
      <div class="field"><label>Phone</label><input id="c-phone" value="${esc(c ? c.phone || '' : '')}"></div>
      <div class="field"><label>Default account</label><select class="acct-pick" id="c-acct">${acctOptions(c ? c.default_account_id : '', a => postable(a) && !isBank(a), 'None')}</select><div class="hint">Pre-fills new invoices, bills and spends</div></div>
    </div>
    <div class="grid g2"><div class="field"><label>Address</label><textarea id="c-addr">${esc(c ? c.address || '' : '')}</textarea></div>
    <div class="field"><label>Notes</label><textarea id="c-notes">${esc(c ? c.notes || '' : '')}</textarea></div></div>
    ${c ? `<label class="check"><input type="checkbox" id="c-arch" ${c.archived ? 'checked' : ''}> Archived (hidden from lists)</label>` : ''}</div>
    ${c ? `<div class="panel flush"><h2>Recent invoices &amp; bills</h2>${items.length ? `<table><tbody>${items.map(i => `<tr class="click" data-href="${KIND[kindOf(i)].base}/${i.id}">
      <td>${KIND[kindOf(i)].one} ${esc(i.number || '')}</td><td>${dfmt(i.date)}</td><td class="num">${fmt(i.total)}</td><td class="num">${i.balance ? fmt(i.balance) + ' owing' : ''}</td><td><span class="pill ${i.state}">${i.state}</span></td></tr>`).join('')}</tbody></table>` : '<div class="empty">None yet</div>'}</div>` : ''}`);
  $$('tr[data-href]').forEach(tr => tr.onclick = () => go(tr.dataset.href));
  $('#c-save').onclick = async () => {
    try {
      const b = { name: val('#c-name'), kind: val('#c-kind'), phone: val('#c-phone'), default_account_id: val('#c-acct') || null,
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

// ---------------------------------------------------------------- account groups
// Groups are headings with subtotals in the chart of accounts and reports. They never change balances.
const groupById = id => (S.groups || []).find(g => g.id == id);
const groupsOfType = t => (S.groups || []).filter(g => g.type === t);
function groupPath(g) { const p = []; for (let x = g; x; x = groupById(x.parent_id)) p.unshift(x.name); return p.join(' › '); }
function groupWithin(g, ancestorId) { for (let x = g; x; x = groupById(x.parent_id)) if (x.id == ancestorId) return true; return false; }
// <option>s for the groups of one type, shown as paths. exceptId hides that group and everything inside it.
function groupOptions(type, selected, exceptId) {
  return groupsOfType(type).filter(g => !exceptId || !groupWithin(g, exceptId))
    .map(g => [groupPath(g), g]).sort((x, y) => x[0].localeCompare(y[0]))
    .map(([path, g]) => `<option value="${g.id}" ${g.id == selected ? 'selected' : ''}>${esc(path)}</option>`).join('');
}

// Arrange report rows (each with group_id, name and values[]) into their groups for one account type.
// At every level, groups come first A-Z, then accounts A-Z; computed lines (Current Year Earnings,
// Other owners' share) stay last in the order given. Group values are summed in cents.
function groupTree(type, rows, keepEmpty = false) {
  const width = rows.length ? rows[0].values.length : 1;
  const nodes = new Map(groupsOfType(type).map(g => [g.id, { group: g, children: [] }]));
  const top = [];
  rows.forEach(r => { const p = r.group_id != null && nodes.get(r.group_id); (p ? p.children : top).push({ row: r }); });
  nodes.forEach(n => { const p = n.group.parent_id != null && nodes.get(n.group.parent_id); (p ? p.children : top).push(n); });
  const rank = x => (x.row ? (x.row.computed || x.row.partner ? 2 : 1) : 0);
  const order = (a, b) => rank(a) - rank(b) ||
    (a.group ? byName(a.group, b.group) : rank(a) === 1 ? byName(a.row, b.row) : 0);
  const finish = list => {
    const out = [];
    list.forEach(x => {
      if (x.row) { out.push(x); return; }
      x.children = finish(x.children);
      if (!x.children.length && !keepEmpty) return;
      x.values = Array(width).fill(0); x.total = 0;
      x.children.forEach(c => {
        const v = c.row ? c.row.values : c.values;
        v.forEach((n, i) => { x.values[i] += n; });
        x.total += c.row ? (c.row.total ?? v.reduce((a, b) => a + b, 0)) : c.total;
      });
      out.push(x);
    });
    return out.sort(order);
  };
  return finish(top);
}
// Flatten a group tree into lines to draw: 'row' (an account) or 'head' (a group, carrying its subtotal).
// A head is 'open' when its contents follow it; with groupsOnly each top-level group is one closed line.
function treeLines(nodes, groupsOnly = false, depth = 0, out = []) {
  nodes.forEach(n => {
    if (n.row) { out.push({ kind: 'row', row: n.row, depth }); return; }
    const open = !groupsOnly && n.children.length > 0;
    out.push({ kind: 'head', group: n.group, values: n.values, total: n.total, depth, open });
    if (!groupsOnly) treeLines(n.children, false, depth + 1, out);
  });
  return out;
}
// Attributes for the name cell of a tree line: the indent, a dot before a group's name (CSS ::before at --x)
// and a rail down the left of each enclosing group, drawn as background stripes so rows join up.
const RAIL_STEP = 20;
function treeCell(l, base) {
  const x = k => base + RAIL_STEP * k + 4, rail = 'linear-gradient(var(--accent),var(--accent))';
  const img = [], pos = [], size = [];
  for (let k = 0; k < l.depth; k++) { img.push(rail); pos.push(`${x(k)}px 0`); size.push('2px 100%'); }
  if (l.kind === 'head' && l.open) { img.push(rail); pos.push(`${x(l.depth)}px 100%`); size.push('2px 50%'); }
  const head = l.kind === 'head';
  const style = `padding-left:${base + RAIL_STEP * l.depth + (head ? 18 : 0)}px;--x:${base + RAIL_STEP * l.depth}px;` +
    (img.length ? `background-image:${img.join(',')};background-position:${pos.join(',')};background-size:${size.join(',')};background-repeat:no-repeat;` : '');
  return `class="${head ? 'gname' : ''}" style="${style}"`;
}
const headClass = l => `ghead gl${Math.min(l.depth, 2)}`;
// The same lines for a CSV export: [label, values, total].
function csvTreeLines(lines, rowLabel) {
  const pad = depth => '  '.repeat(depth);
  return lines.map(l => l.kind === 'row' ? [pad(l.depth) + rowLabel(l.row).trim(), l.row.values, l.row.total]
    : [pad(l.depth) + l.group.name, l.values, l.total]);
}

// ---------------------------------------------------------------- accounts
async function accountsPage() {
  await refreshState();
  const showArch = QS.get('archived') === '1';
  let rowsHtml = '';
  const kind = a => a.subtype === 'bank' ? (a.type === 'asset' ? 'Bank account' : 'Credit card') + (isShared(a) ? ` · shared, yours ${pctText(a.share_bp)}` : '') : a.system ? 'System' : '';
  TYPE_GROUPS.forEach(([t, label]) => {
    const list = S.accounts.filter(a => a.type === t && (showArch || !a.archived));
    if (!list.length && !groupsOfType(t).length) return;
    rowsHtml += `<tr class="sechead" data-type="${t}"><td colspan="3">${label}</td></tr>`;
    // Each line carries its type and enclosing group ids so the search box can show the headings above a match.
    const anc = [];
    treeLines(groupTree(t, list.map(a => ({ ...a, values: [a.balance] })), true)).forEach(l => {
      anc.length = l.depth;
      const attrs = `data-type="${t}" data-anc="${anc.join(' ')}"`;
      if (l.kind === 'row') {
        const a = l.row;
        rowsHtml += `<tr class="click sub" data-id="${a.id}" ${attrs} data-name="${esc(a.name)}"><td ${treeCell(l, 12)}>${esc(a.name)}
          ${a.archived ? ' <span class="pill">archived</span>' : ''}</td><td class="muted">${kind(a)}</td>
          <td class="num">${a.used ? `<a href="#/register/${a.id}?from=2000-01-01" onclick="event.stopPropagation()">${fmt(a.balance)}</a>` : ''}</td></tr>`;
      } else {
        rowsHtml += `<tr class="click sub ${headClass(l)}" data-gid="${l.group.id}" ${attrs} data-name="${esc(l.group.name)}"><td ${treeCell(l, 12)}>${esc(l.group.name)}</td><td class="muted">Group</td><td class="num">${fmt(l.values[0])}</td></tr>`;
        anc.push(l.group.id);
      }
    });
  });
  const dups = duplicateAccountNames();
  page('Chart of accounts', 'Open an account to put it in a group. Groups show a subtotal here and on the reports.',
    `<a class="btn" href="#/accounts${showArch ? '' : '?archived=1'}">${showArch ? 'Hide' : 'Show'} archived</a><button id="g-new">New group</button><button class="primary" id="a-new">New account</button>`,
    `${dups.length ? `<div class="notice warn">More than one account is called ${dups.map(n => `“${esc(n)}”`).join(', ')}. Rename one of each so they can't be mixed up.</div>` : ''}
    <div class="filters"><div class="field"><label>Find an account</label><input type="search" id="a-find" placeholder="Type part of a name" autocomplete="off"></div></div>
    <div class="panel flush"><table><thead><tr><th>Name</th><th>Kind</th><th class="num">Balance</th></tr></thead><tbody>${rowsHtml}</tbody></table>
    <div class="muted" id="a-none" style="padding:12px 14px" hidden>No accounts match.</div></div>`);
  $$('tr[data-id]').forEach(tr => tr.onclick = () => accountModal(acct(tr.dataset.id)));
  $$('tr[data-gid]').forEach(tr => tr.onclick = () => groupModal(groupById(tr.dataset.gid)));
  $('#a-new').onclick = () => accountModal(null);
  $('#g-new').onclick = () => groupModal(null);
  $('#a-find').oninput = () => filterChart(val('#a-find'));
}
// Show only chart lines whose name has every typed word, plus the headings above them and,
// for a matching group, everything inside it.
function filterChart(q) {
  const lines = $$('tr[data-name]'), words = searchWords(q);
  if (!words.length) { $$('tbody tr').forEach(tr => { tr.hidden = false; }); $('#a-none').hidden = true; return; }
  const show = new Set(), groups = new Set(), types = new Set();
  lines.forEach(tr => {
    if (!wordsMatch(words, tr.dataset.name)) return;
    show.add(tr); types.add(tr.dataset.type);
    tr.dataset.anc.split(' ').filter(Boolean).forEach(g => groups.add(g));
    if (tr.dataset.gid) groups.add(`in:${tr.dataset.gid}`);
  });
  lines.forEach(tr => {
    const anc = tr.dataset.anc.split(' ').filter(Boolean);
    if (anc.some(g => groups.has(`in:${g}`))) { show.add(tr); if (tr.dataset.gid) groups.add(`in:${tr.dataset.gid}`); }
    if (tr.dataset.gid && groups.has(tr.dataset.gid)) show.add(tr);
  });
  lines.forEach(tr => { tr.hidden = !show.has(tr); });
  $$('tr.sechead').forEach(tr => { tr.hidden = !types.has(tr.dataset.type); });
  $('#a-none').hidden = show.size > 0;
}

function groupModal(g) {
  const typeSel = TYPE_GROUPS.map(([t, l]) => `<option value="${t}" ${g && g.type === t ? 'selected' : ''}>${l}</option>`).join('');
  modal(`<h2>${g ? 'Edit group' : 'New group'}</h2>
    <p class="muted" style="margin-top:0">A group puts a heading and a subtotal around its accounts in the chart of accounts, Profit &amp; Loss and Balance Sheet. It doesn't change any balances.</p>
    <div class="form-grid">
      <div class="field"><label>Name</label><input id="gm-name" value="${esc(g ? g.name : '')}" placeholder="e.g. Motor vehicle"></div>
      <div class="field"><label>Account type</label><select id="gm-type" ${g ? 'disabled' : ''}>${typeSel}</select></div>
      <div class="field"><label>Inside group</label><select id="gm-parent"></select><div class="hint">Optional. Put this group inside another one for a subtotal within a subtotal.</div></div>
    </div>
    <div class="field"><label>Accounts in this group</label><div class="gm-list" id="gm-list"></div></div>
    <div class="modal-foot">${g ? '<button class="danger" data-x="del" style="margin-right:auto">Delete group</button>' : ''}<button data-x="no">Cancel</button><button class="primary" data-x="yes">Save</button></div>`, el => {
    const fill = () => {
      const t = val('#gm-type', el);
      el.querySelector('#gm-parent').innerHTML = `<option value="">None (top level)</option>${groupOptions(t, g && g.parent_id, g && g.id)}`;
      const list = S.accounts.filter(a => a.type === t && (!a.archived || (g && a.group_id == g.id))).sort(byName);
      el.querySelector('#gm-list').innerHTML = list.length ? list.map(a => {
        const other = a.group_id && (!g || a.group_id != g.id) ? groupById(a.group_id) : null;
        return `<label class="check"><input type="checkbox" value="${a.id}" ${g && a.group_id == g.id ? 'checked' : ''}> ${esc(a.name)}${other ? ` <span class="muted">(now in ${esc(groupPath(other))})</span>` : ''}</label>`;
      }).join('') : '<div class="muted">No accounts of this type yet.</div>';
    };
    el.querySelector('#gm-type').onchange = fill; fill();
    el.querySelector('[data-x=no]').onclick = closeModal;
    const del = el.querySelector('[data-x=del]');
    if (del) del.onclick = async () => {
      const where = g.parent_id ? `“${esc(groupById(g.parent_id).name)}”` : 'no group';
      if (!await confirmBox(`Delete the group “${esc(g.name)}”? Its accounts and any groups inside it move to ${where}. No transactions or balances change.`, 'Delete group', true)) return;
      try { await api('DELETE', `/api/groups/${g.id}`); toast('Group deleted'); accountsPage(); } catch (e) { fail(e); }
    };
    el.querySelector('[data-x=yes]').onclick = async () => {
      try {
        const b = { name: val('#gm-name', el), parent_id: val('#gm-parent', el) || null,
          account_ids: [...el.querySelectorAll('#gm-list input:checked')].map(i => +i.value) };
        if (g) await api('PUT', `/api/groups/${g.id}`, b); else await api('POST', '/api/groups', { ...b, type: val('#gm-type', el) });
        closeModal(); toast('Saved'); accountsPage();
      } catch (e) { fail(e); }
    };
  });
}

function accountModal(a) {
  const sys = a && a.system;
  modal(`<h2>${a ? 'Edit account' : 'New account'}</h2>
    ${sys ? '<div class="notice info">This is a system account used by invoices, bills or reports. You can rename it, but not change its type.</div>' : ''}
    <div class="form-grid">
      <div class="field"><label>Name</label><input id="am-name" value="${esc(a ? a.name : '')}"></div>
      <div class="field"><label>Type</label><select id="am-type" ${sys ? 'disabled' : ''}>${TYPE_GROUPS.map(([t, l]) => `<option value="${t}" ${a && a.type === t ? 'selected' : ''}>${l.replace(/s$/, '').replace('Liabilitie', 'Liability').replace('Cost of Sale', 'Cost of Sales')}</option>`).join('')}</select></div>
      <div class="field"><label>Kind</label><select id="am-sub" ${sys ? 'disabled' : ''}><option value="other">Normal</option><option value="bank" ${a && a.subtype === 'bank' ? 'selected' : ''}>Bank / credit card (reconcilable)</option></select></div>
      <div class="field am-share"><label>Your share %</label><input id="am-share" inputmode="decimal" value="${a ? +(a.share_bp / 100).toFixed(2) : 100}"><div class="hint">Less than 100 for a shared account. Reports show only your share.</div></div>
      <div class="field"><label>Group</label><select id="am-group"></select><div class="hint">Groups add a heading and subtotal on the chart and reports.</div></div>
      <div class="field am-newgrp"><label>New group name</label><input id="am-gname" placeholder="e.g. Motor vehicle"></div>
      <div class="field am-newgrp"><label>Put new group inside</label><select id="am-gparent"></select></div>
    </div>
    ${a && !sys ? `<label class="check"><input type="checkbox" id="am-arch" ${a.archived ? 'checked' : ''}> Archived</label>` : ''}
    <div class="modal-foot">${a && !sys && !a.used ? '<button class="danger" data-x="del" style="margin-right:auto">Delete</button>' : ''}<button data-x="no">Cancel</button><button class="primary" data-x="yes">Save</button></div>`, el => {
    el.querySelector('[data-x=no]').onclick = closeModal;
    const shareVis = () => { el.querySelector('.am-share').hidden = val('#am-sub', el) !== 'bank'; };
    el.querySelector('#am-sub').onchange = shareVis; shareVis();
    const newVis = () => { const on = val('#am-group', el) === 'new'; el.querySelectorAll('.am-newgrp').forEach(f => { f.hidden = !on; }); };
    const fillGroups = () => {
      const t = val('#am-type', el), keep = val('#am-group', el) || (a && a.type === t ? a.group_id : '');
      el.querySelector('#am-group').innerHTML = `<option value="">No group</option>${groupOptions(t, keep)}<option value="new" ${keep === 'new' ? 'selected' : ''}>+ New group…</option>`;
      el.querySelector('#am-gparent').innerHTML = `<option value="">Top level (no other group)</option>${groupOptions(t)}`;
      newVis();
    };
    el.querySelector('#am-group').onchange = () => { newVis(); if (val('#am-group', el) === 'new') el.querySelector('#am-gname').focus(); };
    el.querySelector('#am-type').onchange = () => { el.querySelector('#am-group').value = ''; fillGroups(); };
    fillGroups();
    const del = el.querySelector('[data-x=del]');
    if (del) del.onclick = async () => { try { await api('DELETE', `/api/accounts/${a.id}`); closeModal(); toast('Deleted'); accountsPage(); } catch (e) { fail(e); } };
    el.querySelector('[data-x=yes]').onclick = async () => {
      try {
        const b = { name: val('#am-name', el), type: val('#am-type', el), subtype: val('#am-sub', el) };
        if (b.subtype === 'bank') b.share_pct = parsePct(val('#am-share', el), b.name || 'Account');
        const grp = val('#am-group', el);
        if (grp === 'new') b.new_group = { name: val('#am-gname', el), parent_id: val('#am-gparent', el) || null };
        else b.group_id = grp || null;
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
      <p class="muted" style="margin-top:0">numbr keeps all your data in one file (<code>data\\books.db</code> inside the numbr folder) and copies it to <code>data\\backups</code> every time it starts, keeping the latest 40.
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
  page('Audit log', 'The last 300 changes made in numbr', `<button onclick="history.back()">Back</button>`,
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
    main.innerHTML = `<div class="notice bad">Couldn't connect to numbr. Make sure the numbr window is still open, then refresh this page.<br><small>${esc(e.message)}</small></div>`;
  }
})();
