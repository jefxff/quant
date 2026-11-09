/* QUANT 本地操作台 — 交互逻辑
   零依赖：只用浏览器原生 API。所有命令由服务端白名单执行，页面只负责编排与呈现。
   离线闭环固定使用演示数据区间；用户改动的日期只作用于单步的联网/回测操作。 */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const PARAM_IDS = ['pStart', 'pEnd', 'pLimit', 'pPool', 'pStrategy', 'pDate', 'pDb', 'pRunId'];
  const PARAM_KEYS = { pStart: 'start', pEnd: 'end', pLimit: 'limit', pPool: 'pool',
                       pStrategy: 'strategy', pDate: 'date', pDb: 'db', pRunId: 'run_id' };
  const FALLBACK_PIPELINE = ['data.update.fixture', 'data.status', 'backtest',
                             'paper.run.dry', 'paper.run', 'paper.run', 'report'];
  const RAIL_SHORT = ['灌数据', '数据审计', '事件回测', '模拟盘预演', '模拟盘运行', '幂等复跑', '重渲染报告'];
  const CHIP_KEYS = [
    ['run_id', '报告 ID'], ['kind', '类型'], ['rows_written', '写入行'],
    ['trading_days', '交易日'], ['symbols', '标的'], ['trades', '成交笔数'],
    ['cum_return', '累计收益'], ['max_drawdown', '最大回撤'], ['sharpe', '夏普'],
    ['cost_ratio', '成本占比'], ['missing_days', '缺失日'],
    ['orders_created', '新建订单'], ['trades_created', '新成交'],
    ['already_processed', '幂等命中'], ['waiting_for_next_bar', '等待 T+1'],
    ['assumptions_count', '假设条数'],
  ];
  const RATE_KEYS = new Set(['cum_return', 'max_drawdown', 'cost_ratio']);

  const state = {
    meta: null, busy: false, total: 0, ok: 0, bad: 0, lastMs: null,
    reports: [], activeReport: null, toastTimer: null,
  };

  // ------------------------------------------------------------ 工具
  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /** 逐字符着色 JSON / 普通文本：所有非 token 字符都经过转义，避免 XSS。 */
  function highlight(text) {
    const src = String(text ?? '');
    let out = '', i = 0;
    while (i < src.length) {
      const ch = src[i];
      if (ch === '"') {
        let j = i + 1, token = '"';
        while (j < src.length) {
          const c = src[j];
          token += c;
          if (c === '\\') { token += src[j + 1] || ''; j += 2; continue; }
          j += 1;
          if (c === '"') break;
        }
        const colon = /^\s*:/.exec(src.slice(j));
        out += `<span class="${colon ? 'j-key' : 'j-str'}">${escapeHtml(token)}</span>`;
        if (colon) { out += `<span class="j-punc">:</span>`; j += colon[0].length; }
        i = j;
        continue;
      }
      if (ch === '-' || (ch >= '0' && ch <= '9')) {
        const num = /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(src.slice(i));
        if (num) { out += `<span class="j-num">${num[0]}</span>`; i += num[0].length; continue; }
      }
      const lit = /^(true|false|null)/.exec(src.slice(i));
      if (lit) { out += `<span class="j-lit">${lit[0]}</span>`; i += lit[0].length; continue; }
      out += escapeHtml(ch);
      i += 1;
    }
    return out;
  }

  /** CLI 会先打印一行中文提示再打印 JSON；从末尾回溯找出最后一个合法对象。 */
  function extractJson(text) {
    const src = String(text ?? '');
    const end = src.lastIndexOf('}');
    if (end < 0) return null;
    const starts = [];
    for (let i = 0; i <= end; i += 1) if (src[i] === '{') starts.push(i);
    for (let k = starts.length - 1; k >= 0; k -= 1) {
      try {
        const parsed = JSON.parse(src.slice(starts[k], end + 1));
        if (parsed && typeof parsed === 'object') return parsed;
      } catch (_) { /* 继续向前找 */ }
    }
    return null;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, Object.assign({ cache: 'no-store' }, options));
    let data;
    try { data = await res.json(); }
    catch (_) { throw new Error(`HTTP ${res.status}：响应不是 JSON`); }
    if (!res.ok && data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function toast(message, kind = '') {
    const box = $('#toast');
    box.textContent = message;
    box.className = `toast show ${kind}`;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { box.className = 'toast'; }, 3800);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        () => toast('命令已复制', 'ok'),
        () => fallbackCopy(text),
      );
    } else fallbackCopy(text);
  }
  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast('命令已复制', 'ok'); }
    catch (_) { toast('复制失败，请手动选择文本', 'bad'); }
    document.body.removeChild(ta);
  }

  function fmtElapsed(ms) {
    if (ms == null) return '—';
    return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
  }

  // ------------------------------------------------------------ 元数据 / 参数
  async function loadMeta() {
    const meta = await api('/api/meta');
    state.meta = meta;
    $('#metaRoot').textContent = meta.project_root;
    $('#metaRoot').title = meta.project_root;
    $('#metaPy').textContent = `Python ${meta.python}`;
    if (meta.demo_range) {
      $('#demoHint').textContent = `${meta.demo_range.start} ~ ${meta.demo_range.end}`;
    }
    for (const id of PARAM_IDS) {
      const key = PARAM_KEYS[id];
      if (meta.defaults && meta.defaults[key] != null) $(`#${id}`).value = meta.defaults[key];
    }
    renderActions(meta.actions || []);
    renderRail(meta.pipeline || FALLBACK_PIPELINE);
    return meta;
  }

  function collectParams() {
    const params = {};
    for (const id of PARAM_IDS) params[PARAM_KEYS[id]] = $(`#${id}`).value.trim();
    return params;
  }

  /** 离线闭环固定在演示数据区间执行，避免用户改日期后灌数据失败。 */
  function demoParams() {
    const params = collectParams();
    const range = state.meta?.demo_range;
    if (range) { params.start = range.start; params.end = range.end; }
    return params;
  }

  function renderActions(actions) {
    const host = $('#actionGroups');
    host.innerHTML = '';
    const groups = new Map();
    actions.forEach((a) => {
      if (!groups.has(a.group)) groups.set(a.group, []);
      groups.get(a.group).push(a);
    });
    $('#actionCount').textContent = `${actions.length} 个`;
    for (const [group, items] of groups) {
      const section = document.createElement('div');
      section.className = 'action-group';
      const title = document.createElement('div');
      title.className = 'group-title';
      title.textContent = group;
      section.appendChild(title);
      for (const item of items) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'act';
        btn.dataset.action = item.id;
        btn.innerHTML = `<span class="act-label">${escapeHtml(item.label)}</span>` +
          (item.network ? '<span class="act-tag">需联网</span>' : '') +
          `<span class="act-desc">${escapeHtml(item.desc)}</span>`;
        section.appendChild(btn);
      }
      host.appendChild(section);
    }
  }

  function actionLabel(id) {
    const found = (state.meta?.actions || []).find((a) => a.id === id);
    return found ? found.label : id;
  }

  // ------------------------------------------------------------ 进度（纯文字 + 细线）
  function renderRail(pipeline) {
    const ol = $('#railSteps');
    ol.innerHTML = '';
    pipeline.forEach((id, i) => {
      const li = document.createElement('li');
      const short = RAIL_SHORT[i] || actionLabel(id);
      li.innerHTML = `<span class="step-num">0${i + 1}</span>${escapeHtml(short)}`;
      ol.appendChild(li);
    });
    $('#railFill').style.width = '0%';
    $('#railStatus').textContent = `待运行 · ${pipeline.length} 步`;
    $('#railStatus').className = '';
  }
  function resetRail() {
    $$('#railSteps li').forEach((li) => { li.className = ''; });
    $('#railFill').style.width = '0%';
  }
  function setRail(index, status) {
    const li = $$('#railSteps li')[index];
    if (li) li.className = status;
    const total = (state.meta?.pipeline || FALLBACK_PIPELINE).length;
    const done = $$('#railSteps li.done').length;
    $('#railFill').style.width = `${(done / total) * 100}%`;
  }
  function railStatus(text, kind = '') {
    const el = $('#railStatus');
    el.textContent = text;
    el.className = kind;
  }

  function setBusy(busy) {
    state.busy = busy;
    $$('.act, #btnPipeline').forEach((b) => { b.disabled = busy; });
    $('#btnPipeline').querySelector('strong').textContent = busy ? '命令执行中…' : '运行离线闭环';
  }

  // ------------------------------------------------------------ 执行与回执
  async function postRun(action, params) {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, params }),
    });
    return res.json();
  }

  async function runAction(actionId, options = {}) {
    if (state.busy && !options.fromPipeline) { toast('已有命令正在运行，请稍候', 'bad'); return null; }
    const spec = (state.meta?.actions || []).find((a) => a.id === actionId);
    if (spec?.network && !options.fromPipeline) {
      const ok = window.confirm(
        `「${spec.label}」会访问免费的公开行情接口（Baostock / AKShare）。\n` +
        '回测与模拟盘本身不联网；网络不可达时会以退出码 3 结束。是否继续？');
      if (!ok) return null;
    }
    if (!state.busy) setBusy(true);
    if (options.railIndex != null) setRail(options.railIndex, 'active');
    let result;
    try {
      result = await postRun(actionId, options.params || collectParams());
    } catch (err) {
      result = {
        ok: false, action: actionId, label: actionLabel(actionId), cmd: '—',
        exit_code: 1, elapsed_ms: 0, stdout: '', stderr: String(err),
        error: '请求本地服务失败',
      };
    }
    if (!result.label) result.label = actionLabel(actionId);
    renderReceipt(result);
    state.total += 1;
    if (result.ok) state.ok += 1; else state.bad += 1;
    if (typeof result.elapsed_ms === 'number') state.lastMs = result.elapsed_ms;
    updateCounters();
    if (result.ok && ['backtest', 'paper.run', 'report'].includes(actionId)) loadReports().catch(() => {});
    if (!result.ok && !options.fromPipeline) toast(`${result.label} 失败：${result.error || '退出码 ' + result.exit_code}`, 'bad');
    if (!options.fromPipeline) setBusy(false);
    return result;
  }

  async function runPipeline() {
    if (state.busy) return;
    const pipeline = state.meta?.pipeline || FALLBACK_PIPELINE;
    const demo = demoParams();
    setBusy(true); resetRail();
    let done = 0;
    for (let i = 0; i < pipeline.length; i += 1) {
      railStatus(`执行 ${i + 1}/${pipeline.length} · ${actionLabel(pipeline[i])}`, 'running');
      const res = await runAction(pipeline[i], { fromPipeline: true, railIndex: i, params: demo });
      if (res && res.ok) { setRail(i, 'done'); done += 1; }
      else {
        setRail(i, 'fail');
        railStatus(`第 ${i + 1} 步失败，闭环已停止`, 'fail');
        toast(`闭环在第 ${i + 1} 步停止，请查看运行回执`, 'bad');
        break;
      }
    }
    setBusy(false);
    if (done === pipeline.length) {
      railStatus(`闭环完成 · ${done}/${pipeline.length}`, 'done');
      toast(`离线闭环完成 ${done}/${pipeline.length}，报告已生成`, 'ok');
      loadReports().catch(() => {});
    }
  }

  function chipsFrom(stdout) {
    const obj = extractJson(stdout);
    if (!obj) return '';
    const flat = Object.assign({}, obj, obj.audit || {}, obj.metrics?.full || {});
    const chips = [];
    for (const [key, label] of CHIP_KEYS) {
      if (flat[key] == null) continue;
      const value = flat[key];
      let shown, cls = '';
      if (typeof value === 'boolean') { shown = value ? '是' : '否'; cls = value ? 'flag' : ''; }
      else if (typeof value === 'number') {
        if (RATE_KEYS.has(key)) {
          shown = `${(value * 100).toFixed(2)}%`;
          if (key === 'cum_return') cls = value > 0 ? 'up' : (value < 0 ? 'down' : '');
        } else if (key === 'sharpe') shown = value.toFixed(3);
        else shown = Number.isInteger(value) ? String(value) : value.toFixed(4);
      } else shown = String(value);
      chips.push(`<span class="chip">${escapeHtml(label)}<b class="${cls}">${escapeHtml(shown)}</b></span>`);
    }
    return chips.length ? `<div class="chips">${chips.join('')}</div>` : '';
  }

  function renderReceipt(r) {
    $('#emptyState')?.remove();
    const host = $('#receipts');
    const code = r.exit_code == null ? '—' : r.exit_code;
    const article = document.createElement('article');
    article.className = 'receipt';
    article.dataset.code = String(r.exit_code ?? -1);
    const stamp = r.started_at ? new Date(r.started_at).toLocaleTimeString('zh-CN', { hour12: false }) : '';
    const stdout = r.stdout || '';
    const stderr = r.stderr || '';
    const showStderrFirst = !stdout && !!stderr;
    article.innerHTML = `
      <header>
        <span class="idx">#${String(state.total + 1).padStart(2, '0')}</span>
        <span class="r-label">${escapeHtml(r.label || r.action || '运行')}</span>
        <span class="badge ${r.ok ? 'ok' : 'bad'}">${r.ok ? '成功' : '失败'} · EXIT ${escapeHtml(code)}</span>
        <span class="r-elapsed">${fmtElapsed(r.elapsed_ms)}</span>
        <span class="r-spacer"></span>
        <span class="r-time">${escapeHtml(stamp)}</span>
        <button type="button" class="r-btn copy" aria-label="复制命令">复制命令</button>
      </header>
      ${r.note ? `<div class="r-note">${escapeHtml(r.note)}</div>` : ''}
      <div class="cmdline">${escapeHtml(r.cmd || '—')}</div>
      <div class="out-tabs">
        <button type="button" data-out="stdout" class="${showStderrFirst ? '' : 'active'}">STDOUT</button>
        <button type="button" data-out="stderr" class="${showStderrFirst ? 'active' : ''}">STDERR${stderr ? ' · 有内容' : ''}</button>
      </div>
      <pre class="out stdout ${showStderrFirst ? 'hidden' : ''}">${highlight(stdout || '（空）')}</pre>
      <pre class="out stderr ${showStderrFirst ? '' : 'hidden'}">${highlight(stderr || '（空）')}</pre>
      ${chipsFrom(stdout)}
      ${r.error ? `<div class="chips"><span class="chip">错误<b class="down">${escapeHtml(r.error)}</b></span></div>` : ''}
    `;
    host.insertBefore(article, host.firstChild);
    $('.copy', article).addEventListener('click', () => copyText(r.cmd || ''));
    $$('.out-tabs button', article).forEach((btn) => {
      btn.addEventListener('click', () => {
        $$('.out-tabs button', article).forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const which = btn.dataset.out;
        $('.stdout', article).classList.toggle('hidden', which !== 'stdout');
        $('.stderr', article).classList.toggle('hidden', which !== 'stderr');
      });
    });
    while (host.children.length > 24) host.removeChild(host.lastChild);
  }

  function updateCounters() {
    $('#cTotal').textContent = state.total;
    $('#cOk').textContent = state.ok;
    $('#cBad').textContent = state.bad;
    $('#cLast').textContent = fmtElapsed(state.lastMs);
  }

  // ------------------------------------------------------------ 报告浏览
  async function loadReports(preferRunId) {
    const data = await api('/api/reports');
    state.reports = data.reports || [];
    renderReportList();
    if (preferRunId) return loadReport(preferRunId);
    return null;
  }

  function renderReportList() {
    const ul = $('#reportList');
    ul.innerHTML = '';
    if (!state.reports.length) {
      ul.innerHTML = '<li class="muted">暂无报告，先运行回测或模拟盘。</li>';
      return;
    }
    for (const rep of state.reports) {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `report-item${state.activeReport === rep.run_id ? ' active' : ''}`;
      const when = rep.mtime ? rep.mtime.slice(0, 16).replace('T', ' ') : '';
      btn.innerHTML = `<b>${escapeHtml(rep.run_id)}</b><small>${escapeHtml(rep.kind)} · ${escapeHtml(when)}</small>`;
      btn.addEventListener('click', () => loadReport(rep.run_id));
      li.appendChild(btn);
      ul.appendChild(li);
    }
  }

  async function loadReport(runId) {
    state.activeReport = runId;
    renderReportList();
    const view = $('#reportView');
    view.innerHTML = '<div class="empty"><p>正在读取报告…</p></div>';
    try {
      const rep = await api(`/api/report?run_id=${encodeURIComponent(runId)}`);
      const meta = rep.meta || {};
      const metaChips = [
        ['run_id', rep.run_id], ['kind', meta.kind], ['strategy', meta.strategy],
        ['start', meta.start], ['end', meta.end],
      ].filter(([, v]) => v != null && v !== '').map(
        ([k, v]) => `<span class="chip">${escapeHtml(k)}<b>${escapeHtml(v)}</b></span>`).join('');
      const files = (rep.files || []).map((f) =>
        `<a href="${encodeURI(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.name)} · ${(f.size / 1024).toFixed(1)}K</a>`).join('');
      const png = (rep.files || []).find((f) => f.name === 'equity.png');
      const assumptions = rep.assumptions
        ? `<details><summary style="cursor:pointer;color:var(--accent);margin:14px 0">程序化「假设与限制」原始数据</summary><pre>${highlight(JSON.stringify(rep.assumptions, null, 2))}</pre></details>`
        : '';
      view.innerHTML = `
        <div class="report-meta">${metaChips}</div>
        ${png ? `<img class="equity-img" src="${encodeURI(png.url)}" alt="资金曲线">` : ''}
        <div class="report-files">${files}</div>
        ${assumptions}
        <article class="md">${renderMarkdown(rep.markdown || '（报告正文为空）')}</article>
      `;
    } catch (err) {
      view.innerHTML = `<div class="empty"><h3>读取失败</h3><p>${escapeHtml(err.message)}</p></div>`;
    }
  }

  /** 极简 Markdown 渲染：先整体转义，再生成安全标签（无外部库、无 innerHTML 注入）。 */
  function renderMarkdown(src) {
    const lines = escapeHtml(src).split(/\r?\n/);
    const inline = (s) => s
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    let html = '', i = 0, inCode = false;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) { html += inCode ? '</code></pre>' : '<pre><code>'; inCode = !inCode; i += 1; continue; }
      if (inCode) { html += `${line}\n`; i += 1; continue; }
      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        const n = heading[1].length;
        html += `<h${n}>${inline(heading[2])}</h${n}>`; i += 1; continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const head = cells(line).map((c) => `<th>${inline(c)}</th>`).join('');
        i += 2;
        let rows = '';
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows += `<tr>${cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`;
          i += 1;
        }
        html += `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
        continue;
      }
      if (/^\s*&gt;\s?/.test(line)) {
        html += `<blockquote>${inline(line.replace(/^\s*&gt;\s?/, ''))}</blockquote>`; i += 1; continue;
      }
      const isBullet = /^\s*[-*]\s+/.test(line), isNumbered = /^\s*\d+\.\s+/.test(line);
      if (isBullet || isNumbered) {
        const tag = isNumbered ? 'ol' : 'ul';
        let items = '';
        while (i < lines.length && (/^\s*[-*]\s+/.test(lines[i]) || /^\s*\d+\.\s+/.test(lines[i]))) {
          items += `<li>${inline(lines[i].replace(/^\s*(?:[-*]|\d+\.)\s+/, ''))}</li>`;
          i += 1;
        }
        html += `<${tag}>${items}</${tag}>`;
        continue;
      }
      if (/^\s*---+\s*$/.test(line)) { html += '<hr>'; i += 1; continue; }
      if (line.trim() === '') { i += 1; continue; }
      html += `<p>${inline(line)}</p>`;
      i += 1;
    }
    if (inCode) html += '</code></pre>';
    return html;
  }

  // ------------------------------------------------------------ 事件绑定
  function switchTab(name) {
    $$('.tabs button[role="tab"]').forEach((b) => {
      b.setAttribute('aria-selected', String(b.dataset.tab === name));
    });
    ['console', 'reports', 'guide'].forEach((n) => {
      $(`#panel${n[0].toUpperCase()}${n.slice(1)}`).classList.toggle('hidden', n !== name);
    });
    if (name === 'reports' && !state.reports.length) loadReports().catch(() => {});
  }

  function initTheme() {
    // 极简版默认浅色；只有用户显式选过深色才沿用。
    const saved = localStorage.getItem('quant-ui-theme');
    document.documentElement.dataset.theme = saved === 'dark' ? 'dark' : 'light';
    const btn = $('#btnTheme');
    const sync = () => {
      btn.textContent = document.documentElement.dataset.theme === 'dark' ? '切换浅色' : '切换深色';
    };
    sync();
    btn.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      localStorage.setItem('quant-ui-theme', next);
      sync();
    });
  }

  function bindEvents() {
    $('#actionGroups').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.act');
      if (btn && !btn.disabled) runAction(btn.dataset.action);
    });
    $('#btnPipeline').addEventListener('click', runPipeline);
    $('#btnResetParams').addEventListener('click', () => {
      for (const id of PARAM_IDS) {
        const key = PARAM_KEYS[id];
        if (state.meta?.defaults?.[key] != null) $(`#${id}`).value = state.meta.defaults[key];
      }
      toast('已恢复离线演示参数', 'ok');
    });
    $$('.tabs button[role="tab"]').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
    $('#btnClear').addEventListener('click', () => {
      $('#receipts').innerHTML = '<div class="empty" id="emptyState"><h3>还没有执行记录</h3><p>点击左侧任意操作，或运行「离线闭环」。</p></div>';
      state.total = state.ok = state.bad = 0; state.lastMs = null;
      updateCounters(); resetRail();
      railStatus(`待运行 · ${(state.meta?.pipeline || FALLBACK_PIPELINE).length} 步`);
    });
    $('#btnRefreshReports').addEventListener('click', () => {
      loadReports().then(() => toast('报告列表已刷新', 'ok')).catch((e) => toast(e.message, 'bad'));
    });
  }

  async function init() {
    initTheme();
    bindEvents();
    try {
      await loadMeta();
    } catch (err) {
      toast(`无法读取项目信息：${err.message}`, 'bad');
      railStatus('服务异常', 'fail');
    }
    loadReports().catch(() => {});
  }

  document.addEventListener('DOMContentLoaded', init);
})();
