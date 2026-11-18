/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async 'reconcile-day'() {
    const dayInput = document.getElementById('recon-day');
    const day = dayInput ? dayInput.value : '';
    if (!day) {
      throw new Error('请先选择生效日');
    }
    const result = await getJson(`/api/reconciliation/by-day?effective_day=${encodeURIComponent(day)}`);
    renderReconciliation(result);
    return `生效日 ${day} 采用批次 ${result.run_id}`;
  },

  async 'signed-lookup'() {
    const dayInput = document.getElementById('recon-day');
    const day = dayInput ? dayInput.value : '';
    if (!day) {
      throw new Error('请先选择生效日');
    }
    const result = await getJson(`/api/reconciliation/signed/${encodeURIComponent(day)}`);
    renderReconciliation(result);
    return `已签收批次 ${result.run_id}`;
  },

  async 'signoff-day'() {
    const dayInput = document.getElementById('recon-day');
    const day = dayInput ? dayInput.value : '';
    const noteInput = document.getElementById('recon-note');
    const result = await request('/api/reconciliation/signoff', {
      effective_day: day,
      note: noteInput ? noteInput.value : '',
    });
    renderReconciliation(result);
    return `已签收批次 ${result.run_id}`;
  },
};

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

async function getJson(url) {
  const response = await fetch(url);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

const REASON_LABELS = {
  WINDOW_COVERS_DAY: '生效区间覆盖当天',
  WINDOW_LATEST_WINS: '重叠区间取最新批次',
  WINDOW_SIGNED_OFF_LOCKED: '已签收批次锁定',
  WINDOW_SIGNED_OFF_QUERY: '按已签收批次查询',
  WINDOW_CORRECTION_REPLACES: '更正批次替换原批',
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function renderReconciliation(result) {
  const panel = document.getElementById('recon-result');
  if (!panel) return;
  const selection = result.selection || {};
  const reconcile = result.reconcile || {};
  const superseded = (selection.superseded || [])
    .map((item) => `<li class="mono">${esc(item.run_id)}（序号 ${esc(item.version_seq)}，区间 ${esc(item.window)}）</li>`)
    .join('');
  const signoff = result.signoff;
  const signoffLine = signoff
    ? `<p class="notice">已签收：<span class="mono">${esc(signoff.run_id)}</span>，${esc(signoff.signed_at)}${signoff.note ? `，备注：${esc(signoff.note)}` : ''}</p>`
    : `
      <div class="actions" style="margin-top:8px;">
        <input type="text" id="recon-note" maxlength="200" placeholder="签收备注（可选）" style="min-width:240px;">
        <button type="button" class="btn btn-warn" data-action="signoff-day">签收当天对账</button>
      </div>`;
  panel.style.display = 'block';
  panel.innerHTML = `
    <div class="panel-head">
      <h3 class="panel-title">生效日 <span class="mono">${esc(result.effective_day)}</span> 的对账</h3>
      <span class="badge ${result.signed_off ? 'badge-warn' : 'badge-ok'}">
        ${esc(REASON_LABELS[selection.reason] || selection.reason || '')}
      </span>
    </div>
    <dl class="manifest">
      <div class="manifest-row"><dt>采用批次</dt><dd class="mono">${esc(result.run_id)}（序号 ${esc(result.version_seq)}）</dd></div>
      <div class="manifest-row"><dt>生效区间</dt><dd class="mono">${esc((selection.window || {}).from)} → ${esc((selection.window || {}).to || '∞')}</dd></div>
      <div class="manifest-row"><dt>缘由</dt><dd>${esc(selection.explanation || '')}</dd></div>
      <div class="manifest-row"><dt>候选批次</dt><dd class="mono">${esc((selection.candidates || []).join('、'))}</dd></div>
      <div class="manifest-row"><dt>账平</dt><dd>${reconcile.balanced && reconcile.balanced.all ? '平' : '不平'}</dd></div>
    </dl>
    ${superseded ? `<p class="page-meta">被替换的原批：</p><ul>${superseded}</ul>` : ''}
    ${signoffLine}
    <p class="page-meta"><a class="link mono" href="/api/reconciliation/${esc(result.reconcile_id)}">复核记录 JSON</a></p>`;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run') {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
