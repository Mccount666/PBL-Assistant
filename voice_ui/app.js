/* ============================================================
   PBL 讨论助手 — 前端逻辑（暗色高级感 UI 版）
   ============================================================ */
'use strict';

const state = {
  token: localStorage.getItem('pbl_token') || '',
  caseData: null,
  team: '',
  releasedActs: 0,
  includeFinal: false,
  pendingActions: [],
  listening: false,
  thinking: false,
  speaking: false,
  buffer: [],
  flushTimer: null,
  recog: null,
  quietMs: 2200,
  rendered: new Set(),
  starting: false,
  expandedHyps: new Set(),
  expandedKnowledge: new Set(),
  board: [],
  issues: [],
  boardView: 'map',
  openNode: null,
  mmHighlight: null,
  lastData: null,
};

const $ = (id) => document.getElementById(id);

/* ================= HTTP ================= */
async function api(path, payload) {
  let url = path;
  const opts = { method: payload ? 'POST' : 'GET' };
  if (payload) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify({ token: state.token, ...payload });
  } else if (state.token) {
    url += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(state.token);
  }
  const res = await fetch(url, opts);
  let data;
  try {
    data = await res.json();
  } catch (e) {
    // 服务端返回了非 JSON（错误页 / 网关超时等）
    data = { ok: false, error: `服务异常（HTTP ${res.status}）` };
  }
  if (!data.ok && res.status === 401) {
    localStorage.removeItem('pbl_token');
    state.token = '';
    showPicker();
    toast('会话已失效，请重新开始');
  }
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function fmt(s) {
  return esc(s).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>');
}

function toast(msg, ms = 2400) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add('hidden'), ms);
}

/* ================= 视图切换 ================= */
function showPicker() {
  $('picker-view').classList.remove('hidden');
  $('discuss-view').classList.add('hidden');
  stopListening(true);
}
function showDiscuss() {
  $('picker-view').classList.add('hidden');
  $('discuss-view').classList.remove('hidden');
  if (!state.listening) $('subtitle-text').textContent = '点击左侧圆点开始聆听';
  requestAnimationFrame(() => { if (window.MindMap && state.boardView === 'map') MindMap.fit(); });
}

/* ================= 案例选择 ================= */
async function loadCases() {
  const data = await api('/api/cases');
  if (!data.ok) return;
  const list = $('case-list');
  list.innerHTML = data.cases.map((c) => `
    <div class="case-item" data-case="${esc(c.case_id)}" onclick="startDiscussion('${esc(c.case_id)}')">
      <div class="case-name">${esc(c.title)}${c.source === 'imported' ? ' <span class="case-badge">导入</span>' : ''}</div>
      <div class="case-desc">${esc(c.course)} · 难度 ${esc(c.difficulty)} · 约 ${c.estimated_minutes} 分钟 · ${c.act_count} 幕</div>
    </div>`).join('');
  setConn(data.engine || '');
}

function setConn(engine) {
  const el = $('conn-status');
  if (!el) return;
  const isApi = engine && (engine.indexOf('AI 助手') >= 0 || engine.indexOf('AI 导师') >= 0 || engine.indexOf('API 模式') >= 0);
  if (isApi) {
    el.textContent = 'AI 已连接';
    el.classList.add('live');
  } else {
    el.textContent = engine ? '规则引擎' : '';
    el.classList.remove('live');
  }
}

async function startDiscussion(caseId) {
  if (state.starting) return;        // 防重复点击
  state.starting = true;

  const cards = document.querySelectorAll('.case-item');
  cards.forEach((el) => el.classList.add('starting'));
  const clicked = document.querySelector(`.case-item[data-case="${caseId}"]`);
  if (clicked) clicked.classList.add('active');
  toast('正在启动讨论…', 1500);

  try {
    const team = $('team-input').value.trim();
    const data = await api('/api/start', { case_id: caseId, team });
    if (!data.ok) {
      toast(data.error || '开始失败，请检查服务是否正常');
      return;
    }
    state.token = data.token || '';
    if (state.token) localStorage.setItem('pbl_token', state.token);
    await refreshAll();
    showDiscuss();
    toast('已开始，点击圆点开启麦克风');
  } catch (e) {
    toast('启动失败：' + (e.message || e));
  } finally {
    state.starting = false;
    document.querySelectorAll('.case-item').forEach((el) => {
      el.classList.remove('starting');
      el.classList.remove('active');
    });
  }
}

/* ================= 状态刷新 ================= */
async function refreshAll() {
  const data = await api('/api/state');
  if (!data.ok) return;
  state.caseData = data.case;
  state.team = data.team;
  state.releasedActs = data.released_acts;
  state.includeFinal = data.include_final;
  state.board = data.board || [];
  state.issues = data.issues || [];
  $('review-toggle').checked = state.includeFinal;
  setConn(data.engine || '');

  $('case-label').textContent = (data.team ? data.team + ' · ' : '') + data.case.title;
  renderCase();
  renderDialogue(data.dialogue || []);
  renderBoard(data);
  updateReleaseBtn();
}

function renderCase() {
  if (!state.caseData) return;
  const acts = state.caseData.acts.slice(0, state.releasedActs);
  $('case-body').innerHTML = acts.map((a) => {
    const imgs = (a.images || []).map((u) =>
      `<div class="case-figure"><img src="${esc(u)}" loading="lazy" alt=""></div>`).join('');
    return `<div><b style="color:#E8EDF7;">${esc(a.title)}</b>\n${fmt(a.presentation)}${imgs}</div>`;
  }).join('<div style="height:8px"></div>');
  $('case-toggle').textContent = '案例正文（' + acts.length + ' 幕）';
}

function updateReleaseBtn() {
  const btn = $('release-btn');
  if (!state.caseData) return;
  if (state.releasedActs >= state.caseData.acts.length) {
    btn.textContent = '已全部发放';
    btn.disabled = true;
  } else {
    btn.textContent = '发放下一幕';
    btn.disabled = false;
  }
}

function toggleCase() {
  const body = $('case-body');
  const toggle = $('case-toggle');
  body.classList.toggle('open');
  toggle.classList.toggle('open');
}

/* ================= 对话渲染 ================= */
function renderDialogue(dialogue) {
  const box = $('chat-box');
  const roleName = { student: '学生', coach: '助手' };
  const avatarMap = { student: '学', coach: '助' };
  box.innerHTML = dialogue.map((d) => {
    if (d.role === 'system') {
      return `<div class="chat-msg sys">
        <div class="chat-avatar">✦</div>
        <div class="chat-bubble">${esc(d.content)}</div>
      </div>`;
    }
    const cls = d.role === 'student' ? 'user' : 'tutor';
    let refs = '';
    if (d.role === 'coach' && d.meta) {
      try {
        const m = JSON.parse(d.meta);
        if (m.refs && m.refs.length) {
          refs = '<div class="chat-refs"><div class="chat-refs-title">可查文献</div>' +
            m.refs.map((it) => `<a class="chat-ref" href="${esc(it.url)}" target="_blank" rel="noopener">
              <span class="chat-ref-title">${esc(it.title)}</span>
              <span class="chat-ref-meta">${esc(it.journal || '')}${it.year ? ' · ' + esc(it.year) : ''}${it.source ? ' · ' + esc(it.source) : ''}</span>
            </a>`).join('') + '</div>';
        }
      } catch (e) { refs = ''; }
    }
    return `<div class="chat-msg ${cls}">
      <div class="chat-avatar">${avatarMap[d.role] || '·'}</div>
      <div class="chat-bubble">${fmt(d.content)}${refs}</div>
    </div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

/* ================= 画板渲染 ================= */
const STATUS = {
  proposed:  { dot: 'open',      label: '已提出' },
  supported: { dot: 'confirmed', label: '支持增强' },
  refuted:   { dot: 'rejected',  label: '已推翻' },
  pending:   { dot: 'open',      label: '待定' },
};

function safeJson(s) {
  try { return JSON.parse(s || '{}') || {}; } catch (e) { return {}; }
}

function renderBoard(data) {
  state.lastData = data;
  // 假设/议题以 hypotheses/issues 表为准；知识点与导师提示来自画布
  const canvas = data.canvas || [];
  renderInsights(canvas.filter((n) => n.type === 'insight'));
  renderKnowledge(canvas.filter((n) => n.type === 'knowledge'));
  renderHyps(data.board || []);
  renderIssues(data.issues || []);
  if (state.boardView === 'map' && window.MindMap) {
    MindMap.render(data, state.mmHighlight ? { highlight: state.mmHighlight } : undefined);
    state.mmHighlight = null;
  }
  if (state.openNode) renderNodeDetail();
}

function setBoardView(mode) {
  state.boardView = mode;
  $('view-map').classList.toggle('active', mode === 'map');
  $('view-list').classList.toggle('active', mode === 'list');
  $('mindmap-view').classList.toggle('hidden', mode !== 'map');
  $('board-list-view').classList.toggle('hidden', mode !== 'list');
  $('map-controls').classList.toggle('hidden', mode !== 'map');
  if (mode === 'map') {
    if (window.MindMap) {
      MindMap.reset();
      if (state.lastData) {
        MindMap.render(state.lastData, state.mmHighlight ? { highlight: state.mmHighlight } : undefined);
        state.mmHighlight = null;
      }
    }
  } else {
    closeNodeDetail();
  }
}

const NODE_KIND = { hypothesis: '假设', knowledge: '知识点', issue: '议题', insight: '助手提示' };

function openNodeDetail(type, id) {
  state.openNode = { type, id: String(id) };
  renderNodeDetail();
}

function closeNodeDetail() {
  state.openNode = null;
  const panel = $('node-detail');
  panel.classList.add('hidden');
  panel.innerHTML = '';
}

function renderNodeDetail() {
  const panel = $('node-detail');
  const o = state.openNode;
  if (!o) { panel.classList.add('hidden'); return; }
  const d = state.lastData || {};
  const canvas = d.canvas || [];
  let node = null;
  if (o.type === 'hypothesis') node = (d.board || []).find((x) => String(x.id) === o.id);
  else if (o.type === 'issue') node = (d.issues || []).find((x) => String(x.id) === o.id);
  else node = canvas.find((x) => x.type === o.type && String(x.id) === o.id);
  if (!node) { closeNodeDetail(); return; }

  const title = o.type === 'hypothesis' ? node.text.replace(/^假设：/, '')
    : o.type === 'issue' ? node.text.replace(/^议题：/, '')
    : o.type === 'insight' ? node.text.replace(/^(导师|助手)：/, '') : node.text;
  let html = `<div class="nd-head"><span class="nd-kind ${o.type}">${NODE_KIND[o.type] || ''}</span>
    <button class="nd-close" onclick="closeNodeDetail()" title="关闭">✕</button></div>
    <div class="nd-title">${esc(title)}</div>`;

  if (o.type === 'hypothesis') {
    const statuses = ['proposed', 'supported', 'pending', 'refuted'];
    html += `<div class="nd-status-row">` + statuses.map((s) => {
      const st = STATUS[s];
      return `<button class="nd-status ${st.dot} ${node.status === s ? 'active' : ''}" onclick="setHypStatus(${node.id},'${s}')">${st.label}</button>`;
    }).join('') + `</div>`;
    html += `<div class="nd-section-title">依据</div><div class="hyp-ev-list">${renderEvidence(node.evidence || [])}</div>
      <form class="mini-form" onsubmit="return addEvidence(event, ${node.id})">
        <input type="text" placeholder="＋ 补充案例依据" maxlength="200">
      </form>
      <div class="nd-actions">
        <button class="btn-text" onclick="editHyp(${node.id})">编辑</button>
        <button class="btn-text nd-danger" onclick="deleteHyp(${node.id})">删除</button>
      </div>`;
  } else if (o.type === 'knowledge') {
    const det = safeJson(node.detail);
    const diseases = det.diseases || [], types = det.types || [], notes = det.notes || [];
    html += `<div class="nd-knowledge">
      ${diseases.length ? '<div class="kd-row"><span class="kd-label">相关疾病</span><span>' + diseases.map(esc).join('、') + '</span></div>' : ''}
      ${types.length ? '<div class="kd-row"><span class="kd-label">分类分型</span><span>' + types.map(esc).join('、') + '</span></div>' : ''}
      ${notes.length ? '<div class="kd-row"><span class="kd-label">追问要点</span><span>' + notes.map(esc).join('；') + '</span></div>' : ''}
      ${!diseases.length && !types.length && !notes.length ? '<div class="hyp-ev-empty">暂无展开信息</div>' : ''}
    </div>
    <div class="nd-actions"><button class="btn-text" onclick="loadRefs(${node.id},'nd-krefs-${node.id}')">检索文献</button></div>
    <div class="knowledge-refs-box hidden" id="nd-krefs-${node.id}"></div>`;
  } else if (o.type === 'issue') {
    const done = node.status === 'resolved';
    html += `<label class="check-row nd-resolve"><input type="checkbox" ${done ? 'checked' : ''}
      onchange="toggleIssue(${node.id},'${done ? 'open' : 'resolved'}')"> 已解决</label>
      <div class="nd-actions">
        <button class="btn-text" onclick="editIssue(${node.id})">编辑</button>
        <button class="btn-text nd-danger" onclick="deleteIssue(${node.id})">删除</button>
      </div>`;
  } else {
    html += `<div class="nd-text">${esc(title)}</div>`;
  }

  panel.innerHTML = html;
  panel.classList.remove('hidden');
}

function renderInsights(items) {
  const sec = $('insight-section');
  const box = $('insight-list');
  $('ins-count').textContent = items.length ? items.length + ' 条' : '';
  if (!items.length) {
    sec.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  sec.classList.remove('hidden');
  const recent = items.slice(-8).reverse();
  box.innerHTML = recent.map((n) => {
    const text = n.text.replace(/^(导师|助手)：/, '');
    return '<div class="insight-item"><span class="insight-dot"></span><span>' + esc(text) + '</span></div>';
  }).join('');
}

function renderKnowledge(items) {
  const box = $('knowledge-grid');
  $('k-count').textContent = items.length ? items.length + ' 条' : '';
  if (!items.length) {
    box.innerHTML = '<div class="knowledge-empty">讨论开始后自动提取</div>';
    return;
  }
  box.innerHTML = items.map((n) => {
    const d = safeJson(n.detail);
    const diseases = d.diseases || [], types = d.types || [], notes = d.notes || [];
    const tag = diseases.length ? '<span class="knowledge-tag disease">' + esc(diseases[0]) + '</span>'
      : types.length ? '<span class="knowledge-tag type">' + esc(types[0]) + '</span>'
      : '<span class="knowledge-tag symptom">症状</span>';
    const hasDetail = diseases.length || types.length || notes.length;
    const detail = `<div class="knowledge-detail hidden" id="kdetail-${n.id}">
        ${diseases.length ? '<div class="kd-row"><span class="kd-label">相关疾病</span><span>' + diseases.map(esc).join('、') + '</span></div>' : ''}
        ${types.length ? '<div class="kd-row"><span class="kd-label">分类分型</span><span>' + types.map(esc).join('、') + '</span></div>' : ''}
        ${notes.length ? '<div class="kd-row"><span class="kd-label">追问要点</span><span>' + notes.map(esc).join('；') + '</span></div>' : ''}
      </div>`;
    return `<div class="knowledge-block" id="kblock-${n.id}">
      <div class="knowledge-item">
        <span class="knowledge-text" onclick="toggleKnowledge(${n.id})" title="点击展开详情">${esc(n.text)}</span>
        ${tag}
        ${hasDetail ? '<button class="knowledge-expand" onclick="toggleKnowledge(' + n.id + ')" title="展开详情">▾</button>' : ''}
        <button class="knowledge-refs" onclick="loadRefs(${n.id})" title="检索相关文献">文献</button>
      </div>
      ${hasDetail ? detail : ''}
      <div class="knowledge-refs-box hidden" id="krefs-${n.id}"></div>
    </div>`;
  }).join('');
  state.expandedKnowledge.forEach((id) => {
    const el = $('kdetail-' + id);
    if (el) el.classList.remove('hidden');
  });
}

function toggleKnowledge(id) {
  const el = $('kdetail-' + id);
  if (!el) return;
  if (el.classList.toggle('hidden')) state.expandedKnowledge.delete(id);
  else state.expandedKnowledge.add(id);
}

async function loadRefs(nodeId, boxId) {
  const box = $(boxId || ('krefs-' + nodeId));
  if (!box) return;
  if (!box.classList.contains('hidden')) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = '<div class="knowledge-empty">检索文献中…</div>';
  try {
    const data = await api('/api/knowledge/refs', { node_id: nodeId });
    if (!data.ok || !data.items || !data.items.length) {
      box.innerHTML = '<div class="knowledge-empty">未找到相关文献</div>';
      return;
    }
    box.innerHTML = data.items.map((it) => `<a class="knowledge-item" href="${esc(it.url)}" target="_blank" rel="noopener">
        <span class="knowledge-text" style="color:var(--accent);">${esc(it.title)}</span>
        <span style="font-size:10.5px;color:var(--muted);white-space:nowrap;">${esc(it.journal || '')}${it.year ? ' · ' + esc(it.year) : ''}${it.cited ? ' · 被引 ' + it.cited : ''}</span>
      </a>`).join('');
  } catch (e) {
    box.innerHTML = '<div class="knowledge-empty">检索失败：' + esc(e.message) + '</div>';
  }
}

function renderHyps(items) {
  const box = $('hyp-list');
  $('h-count').textContent = items.length ? items.length + ' 条' : '';
  if (!items.length) {
    box.innerHTML = '<div class="mini-empty">暂无，讨论中提出或手动添加</div>';
    return;
  }
  // 保留正在输入的依据，避免刷新时丢失
  const prevVals = {};
  box.querySelectorAll('.hyp-block').forEach((b) => {
    const id = b.id.replace('hyp-', '');
    const inp = b.querySelector('.hyp-detail input');
    if (inp && inp.value) prevVals[id] = inp.value;
  });
  box.innerHTML = items.map((h) => {
    const st = STATUS[h.status] || STATUS.proposed;
    const text = h.text.replace(/^假设：/, '');
    const done = h.status === 'refuted';
    const ev = h.evidence || [];
    return `<div class="hyp-block" id="hyp-${h.id}">
      <div class="hyp-item">
        <span class="hyp-dot ${st.dot}" title="${st.label}"></span>
        <span class="hyp-text ${done ? 'rejected' : ''}" onclick="toggleHypDetail(${h.id})" title="点击展开依据">${esc(text)}</span>
        <span class="hyp-status-label ${st.dot}">${st.label}</span>
        ${ev.length ? '<span class="hyp-ev-count" onclick="toggleHypDetail(' + h.id + ')" title="查看依据">' + ev.length + ' 依据</span>' : ''}
        <button class="hyp-status-btn" onclick="setHypStatus(${h.id},'supported')" title="支持增强">✓</button>
        <button class="hyp-status-btn" onclick="setHypStatus(${h.id},'refuted')" title="推翻">✗</button>
        <button class="mini-edit" onclick="editHyp(${h.id})" title="编辑措辞">✎</button>
        <button class="mini-del" onclick="deleteHyp(${h.id})" title="删除">✕</button>
      </div>
      <div class="hyp-detail hidden" id="hyp-detail-${h.id}">
        ${renderHypTimeline(h.events)}
        <div class="hyp-ev-list">${renderEvidence(ev)}</div>
        <form class="mini-form" onsubmit="return addEvidence(event, ${h.id})">
          <input type="text" placeholder="＋ 补充依据：案例里的哪条信息支持它？" maxlength="200">
        </form>
      </div>
    </div>`;
  }).join('');
  state.expandedHyps.forEach((id) => {
    const d = $('hyp-detail-' + id);
    if (d) {
      d.classList.remove('hidden');
      if (prevVals[id]) {
        const inp = d.querySelector('input');
        if (inp) inp.value = prevVals[id];
      }
    }
  });
}

function renderEvidence(ev) {
  if (!ev || !ev.length) {
    return '<div class="hyp-ev-empty">还没有依据 —— 补充案例里支持它的信息</div>';
  }
  return ev.map((e) => '<div class="hyp-ev-item"><span class="hyp-ev-act">第' + e.act_no + '幕</span><span>' + esc(e.note) + '</span></div>').join('');
}

function renderHypTimeline(events) {
  // 只有状态发生过变化（>1 条事件）才展示演变轨迹
  if (!events || events.length <= 1) return '';
  const items = events.map((e) => {
    const st = STATUS[e.status] || STATUS.proposed;
    const note = (e.note && e.note !== '提出假设') ? '<span class="hyp-tl-note">' + esc(e.note) + '</span>' : '';
    return '<div class="hyp-tl-item"><span class="hyp-tl-act">第' + e.act_no + '幕</span>'
      + '<span class="hyp-dot ' + st.dot + '"></span>'
      + '<span class="hyp-tl-label">' + st.label + '</span>' + note + '</div>';
  }).join('');
  return '<div class="hyp-timeline"><div class="hyp-tl-title">状态演变</div>' + items + '</div>';
}

function toggleHypDetail(id) {
  const el = $('hyp-detail-' + id);
  if (!el) return;
  if (el.classList.toggle('hidden')) state.expandedHyps.delete(id);
  else state.expandedHyps.add(id);
}

async function addEvidence(ev, id) {
  ev.preventDefault();
  const input = ev.target.querySelector('input');
  const note = input.value.trim();
  if (!note) return false;
  await api('/api/board/evidence', { id, note });
  state.expandedHyps.add(id);
  await refreshAll();
  return false;
}

function renderIssues(items) {
  const box = $('issue-list');
  $('i-count').textContent = items.length ? items.length + ' 条' : '';
  if (!items.length) {
    box.innerHTML = '<div class="mini-empty">暂无</div>';
    return;
  }
  box.innerHTML = items.map((it) => {
    const done = it.status === 'resolved';
    const text = it.text.replace(/^议题：/, '');
    return `<div class="issue-item">
      <input type="checkbox" class="issue-check" ${done ? 'checked' : ''} onchange="toggleIssue(${it.id},'${done ? 'open' : 'resolved'}')">
      <span class="issue-text ${done ? 'done' : ''}">${esc(text)}</span>
      <button class="mini-edit" onclick="editIssue(${it.id})" title="编辑措辞">✎</button>
      <button class="mini-del" onclick="deleteIssue(${it.id})" title="删除">✕</button>
    </div>`;
  }).join('');
}

function renderQuickActions() {
  const box = $('quick-actions');
  if (!state.pendingActions.length) { box.innerHTML = ''; return; }
  box.innerHTML = state.pendingActions.map((a, i) => {
    const label = a.type === 'add_hypothesis' ? '＋ 假设：' + esc(a.text) : '＋ 议题：' + esc(a.text);
    return `<button class="quick-action-btn" onclick="acceptAction(${i})">${label}</button>`;
  }).join('');
}

async function acceptAction(i) {
  const a = state.pendingActions[i];
  if (!a) return;
  state.pendingActions.splice(i, 1);
  renderQuickActions();
  await sendDiscussion('写进白板：' + a.text, false, true);
}

/* ================= 语音识别（浏览器） ================= */
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

function initSpeech() {
  if (!SpeechRec) return null;
  const r = new SpeechRec();
  r.lang = 'zh-CN';
  r.continuous = true;
  r.interimResults = true;
  r.maxAlternatives = 1;
  r.onresult = (ev) => {
    let interim = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const res = ev.results[i];
      const txt = res[0].transcript.trim();
      if (!txt) continue;
      if (res.isFinal) {
        state.buffer.push(txt);
        $('subtitle-text').textContent = state.buffer.join(' ');
        scheduleFlush();
      } else {
        interim += txt;
      }
    }
    if (interim) $('subtitle-text').textContent = (state.buffer.join(' ') + ' ' + interim).trim();
  };
  r.onerror = (ev) => {
    if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
      setVoiceState('麦克风权限被拒绝', '请在地址栏允许麦克风后重试', false);
      state.listening = false;
      updateMic();
    }
  };
  r.onend = () => {
    if (state.listening && !state.speaking) {
      try { r.start(); } catch (e) {}
    }
  };
  return r;
}

function scheduleFlush() {
  clearTimeout(state.flushTimer);
  state.flushTimer = setTimeout(flushBuffer, state.quietMs);
}

async function flushBuffer() {
  if (!state.buffer.length) return;
  const text = state.buffer.join(' ').trim();
  state.buffer = [];
  if (text) await sendDiscussion(text);
}

async function sendDiscussion(text, force = false, forceWrite = false) {
  if (!state.token || state.thinking) return;
  state.thinking = true;
  updateMic();
  setVoiceState(forceWrite ? '正在美化写入白板…' : '助手思考中…',
                forceWrite ? 'AI 正基于案例整理这条内容' : '助手正在结合案例整理回应');

  try {
    const data = await api('/api/discuss', { text, force, force_write: forceWrite });
    if (data.ok) {
      state.pendingActions = data.actions || [];
      renderQuickActions();
      if (data.written) {
        state.mmHighlight = data.written;
        toast('已写进白板：' + data.written.title);
      }
      await refreshAll();
      if (data.reply) speak(data.reply);
      else if (state.listening) setVoiceState('聆听中…', '继续讨论');
    }
  } catch (e) {
    toast('提交失败：' + e.message);
  } finally {
    state.thinking = false;
    updateMic();
  }
}

async function writeInputToBoard() {
  const text = $('say-input').value.trim();
  if (!text) { toast('先输入要写进白板的内容'); return; }
  $('say-input').value = '';
  setBoardView('map');
  await sendDiscussion(text, false, true);
}

/* ================= TTS ================= */
function pickChineseVoice() {
  const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
  return voices.find((v) => /zh[-_]CN/i.test(v.lang) && /Xiaoxiao|Yaoyao|Huihui|Microsoft/i.test(v.name))
      || voices.find((v) => /zh/i.test(v.lang))
      || null;
}

function speak(text) {
  if (!window.speechSynthesis || !$('tts-toggle').checked) return;
  const clean = String(text).replace(/\*\*/g, '').replace(/\n+/g, '。');
  const u = new SpeechSynthesisUtterance(clean);
  u.lang = 'zh-CN';
  u.rate = 1.05;
  const v = pickChineseVoice();
  if (v) u.voice = v;
  state.speaking = true;
  if (state.recog && state.listening) { try { state.recog.abort(); } catch (e) {} }
  setVoiceState('助手发言中…', '');
  u.onend = u.onerror = () => {
    state.speaking = false;
    if (state.listening) {
      try { state.recog.start(); } catch (e) {}
      setVoiceState('聆听中…', '继续讨论');
    } else {
      setVoiceState('麦克风未开启', '点击左侧圆点开始聆听');
    }
  };
  speechSynthesis.speak(u);
}

/* ================= 实时语音（StepFun） ================= */
const rt = { ws: null, micCtx: null, playCtx: null, stream: null, node: null, nextPlay: 0, active: false, aiText: '' };

function voiceMode() {
  return ($('voice-mode') && $('voice-mode').value) || 'realtime';
}

function abToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

function playPcm16(b64) {
  try {
    if (!rt.playCtx) rt.playCtx = new AudioContext({ sampleRate: 24000 });
    const ctx = rt.playCtx;
    if (ctx.state === 'suspended') ctx.resume();
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const i16 = new Int16Array(bytes.buffer);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, 24000);
    buf.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (rt.nextPlay < now + 0.05) rt.nextPlay = now + 0.05;
    src.start(rt.nextPlay);
    rt.nextPlay += buf.duration;
  } catch (e) {}
}

function handleRealtimeEvent(msg) {
  const t = msg.type || '';
  if (t === 'session.created') {
    setVoiceState('实时语音已连接', '直接开始讨论即可');
  } else if (t === 'input_audio_buffer.speech_started') {
    setVoiceState('聆听中…', '请继续发言');
  } else if (t === 'input_audio_buffer.speech_stopped') {
    setVoiceState('助手思考中…', '');
  } else if (t === 'response.audio.delta') {
    if (msg.delta) playPcm16(msg.delta);
    setVoiceState('助手发言中…', '');
  } else if (t === 'response.audio_transcript.delta') {
    rt.aiText += msg.delta || '';
    $('subtitle-text').textContent = rt.aiText;
  } else if (t === 'response.audio_transcript.done') {
    rt.aiText = '';
    setVoiceState('聆听中…', '继续讨论');
  } else if (t === 'board.updated') {
    refreshAll();
  } else if (t === 'conversation.item.input_audio_transcription.completed') {
    refreshAll();
  }
}

async function connectRealtime() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = proto + '//' + location.host + '/ws/realtime/' + state.token;
  rt.ws = new WebSocket(url);
  rt.ws.onopen = () => {
    rt.active = true;
    setVoiceState('实时语音连接中…', '');
  };
  rt.ws.onmessage = (ev) => {
    try { handleRealtimeEvent(JSON.parse(ev.data)); } catch (e) {}
  };
  rt.ws.onclose = () => {
    rt.active = false;
    if (state.listening) setVoiceState('实时语音已断开', '请检查网络或 API Key');
  };
  rt.ws.onerror = () => {
    rt.active = false;
    setVoiceState('实时语音连接失败', '请检查网络或 API Key');
  };
}

function startMicCapture() {
  navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 24000, channelCount: 1, echoCancellation: true, noiseSuppression: true } })
    .then((stream) => {
      rt.stream = stream;
      rt.micCtx = new AudioContext({ sampleRate: 24000 });
      const src = rt.micCtx.createMediaStreamSource(stream);
      const node = rt.micCtx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (ev) => {
        if (!rt.ws || rt.ws.readyState !== WebSocket.OPEN) return;
        const data = ev.inputBuffer.getChannelData(0);
        const i16 = new Int16Array(data.length);
        for (let i = 0; i < data.length; i++) {
          i16[i] = Math.max(-32768, Math.min(32767, data[i] * 32768));
        }
        rt.ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: abToBase64(i16.buffer) }));
      };
      src.connect(node);
      node.connect(rt.micCtx.destination);
      rt.node = node;
    })
    .catch((err) => {
      setVoiceState('麦克风权限被拒绝', '请在地址栏允许麦克风后重试', false);
      state.listening = false;
      updateMic();
    });
}

function stopMicCapture() {
  if (rt.node) { try { rt.node.disconnect(); } catch (e) {} rt.node = null; }
  if (rt.micCtx) { try { rt.micCtx.close(); } catch (e) {} rt.micCtx = null; }
  if (rt.stream) { rt.stream.getTracks().forEach((t) => t.stop()); rt.stream = null; }
  if (rt.ws) { try { rt.ws.close(); } catch (e) {} rt.ws = null; }
  rt.active = false;
}

/* ================= 语音控制 ================= */
function setVoiceState(text, sub) {
  $('voice-state').textContent = text;
  if (sub !== undefined) $('subtitle-text').textContent = sub;
}

function updateMic() {
  const btn = $('mic-btn');
  btn.classList.toggle('active', state.listening);
  btn.classList.toggle('speaking', state.speaking);
  btn.classList.toggle('thinking', state.thinking && !state.speaking);
  $('mic-icon').textContent = state.listening ? '◉' : '●';
}

function toggleMic() {
  if (state.listening) {
    stopListening();
  } else {
    startListening();
  }
}

function startListening() {
  if (!state.token) { toast('请先选择案例开始讨论'); return; }
  state.listening = true;
  updateMic();

  if (voiceMode() === 'realtime') {
    setVoiceState('实时语音连接中…', '');
    connectRealtime();
    startMicCapture();
  } else {
    if (!state.recog) state.recog = initSpeech();
    if (!state.recog) {
      setVoiceState('浏览器不支持语音识别', '请切换到实时语音模式');
      state.listening = false;
      updateMic();
      return;
    }
    try {
      state.recog.start();
      setVoiceState('聆听中…', '请开始讨论');
    } catch (e) {
      setVoiceState('语音识别启动失败', e.message);
    }
  }
}

function stopListening(silent) {
  state.listening = false;
  state.buffer = [];
  if (state.recog) { try { state.recog.abort(); } catch (e) {} }
  stopMicCapture();
  if (!silent) setVoiceState('麦克风未开启', '点击左侧圆点开始聆听');
  updateMic();
}

/* ================= 操作 ================= */
async function releaseAct() {
  const data = await api('/api/release', {});
  if (!data.ok) { toast(data.error || '发放失败'); return; }
  toast(data.message || '已发放');
  await refreshAll();
}

async function exportMinutes() {
  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: state.token, include_final: $('review-toggle').checked }),
    });
    const ctype = res.headers.get('Content-Type') || '';
    if (!res.ok || !ctype.includes('word')) {
      let msg = '导出失败';
      try { const d = await res.json(); msg = d.error || msg; } catch (e) { /* 非 JSON */ }
      toast(msg);
      return;
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    let fname = 'PBL纪要.docx';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if (m) { try { fname = decodeURIComponent(m[1]); } catch (e) { /* 保留默认 */ } }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('纪要已开始下载');
  } catch (e) {
    toast('导出异常：' + e.message);
  }
}

async function sendSay() {
  const text = $('say-input').value.trim();
  if (!text) return;
  $('say-input').value = '';
  await sendDiscussion(text, true);
}

function setReviewMode(on) {
  state.includeFinal = on;
}

async function submitHyp(ev) {
  ev.preventDefault();
  const text = $('hyp-input').value.trim();
  if (!text) return false;
  $('hyp-input').value = '';
  await sendDiscussion('请把假设「' + text + '」写进白板', false, true);
  return false;
}

async function submitIssue(ev) {
  ev.preventDefault();
  const text = $('issue-input').value.trim();
  if (!text) return false;
  $('issue-input').value = '';
  await sendDiscussion('请把议题「' + text + '」写进白板', false, true);
  return false;
}

async function setHypStatus(id, status) {
  await api('/api/board/status', { id, status });
  await refreshAll();
}

async function deleteHyp(id) {
  if (!confirm('删除该假设？')) return;
  await api('/api/board/delete', { id });
  await refreshAll();
}

async function toggleIssue(id, status) {
  await api('/api/issue/status', { id, status });
  await refreshAll();
}

async function deleteIssue(id) {
  if (!confirm('删除该议题？')) return;
  await api('/api/issue/delete', { id });
  await refreshAll();
}

async function editHyp(id) {
  const h = (state.board || []).find((x) => x.id === id);
  if (!h) return;
  const input = prompt('编辑假设：', h.text);
  if (input === null) return;
  const text = input.trim();
  if (!text || text === h.text) return;
  await api('/api/board/edit', { id, text });
  await refreshAll();
}

async function editIssue(id) {
  const it = (state.issues || []).find((x) => x.id === id);
  if (!it) return;
  const input = prompt('编辑议题：', it.text);
  if (input === null) return;
  const text = input.trim();
  if (!text || text === it.text) return;
  await api('/api/issue/edit', { id, text });
  await refreshAll();
}

/* ================= 设置 ================= */
let savedSettings = {};

async function openSettings() {
  const data = await api('/api/settings');
  if (data.ok) {
    // 兼容两种返回形状：{settings:{...}} 或扁平字段
    const s = data.settings || data;
    savedSettings = s;
    $('set-base').value = s.api_base || s.base_url || '';
    $('set-model').value = s.model || '';
    $('set-utility-model').value = s.utility_model || '';
    $('key-state').textContent = s.key_masked ? '已配置：' + s.key_masked : '尚未配置';
    $('voice-mode').value = s.voice_mode || 'realtime';
    $('tts-toggle').checked = s.tts_enabled !== false;
  }
  $('settings-modal').classList.remove('hidden');
  $('test-result').classList.add('hidden');
}

function closeSettings() {
  $('settings-modal').classList.add('hidden');
}

async function saveSettings() {
  const payload = {
    api_base: $('set-base').value.trim(),
    model: $('set-model').value.trim(),
    utility_model: $('set-utility-model').value.trim(),
    api_key: $('set-key').value.trim(),
    voice_mode: $('voice-mode').value,
    tts_enabled: $('tts-toggle').checked,
  };
  const data = await api('/api/settings', payload);
  if (data.ok) {
    toast('设置已保存');
    closeSettings();
    if (state.listening) {
      stopListening(true);
      startListening();
    }
  } else {
    toast(data.error || '保存失败');
  }
}

async function testConnection() {
  const btn = $('test-btn');
  btn.disabled = true;
  btn.textContent = '检测中…';
  const box = $('test-result');
  box.className = 'test-result';
  box.classList.remove('hidden');
  try {
    const data = await api('/api/test_connection', {});
    const t = data.test || {};
    if (t.ok) {
      box.classList.add('ok');
      box.textContent = '连接成功 · 延迟 ' + t.latency_ms + ' ms';
      $('set-key').value = '';
      $('key-state').textContent = '已配置：' + (savedSettings.key_masked || '');
    } else {
      box.classList.add('fail');
      box.textContent = '连接失败：' + (t.error || '未知错误');
    }
  } catch (e) {
    box.classList.add('fail');
    box.textContent = '检测异常：' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '检测';
  }
}

/* ================= 案例导入 ================= */
let importDraft = null;

function openImport() {
  importDraft = null;
  $('import-files').value = '';
  $('import-preview').classList.add('hidden');
  $('import-preview').innerHTML = '';
  $('import-error').classList.add('hidden');
  $('import-confirm-btn').disabled = true;
  $('import-parse-btn').disabled = false;
  $('import-parse-btn').textContent = '解析';
  $('import-modal').classList.remove('hidden');
}

function closeImport() {
  $('import-modal').classList.add('hidden');
}

function importError(msg) {
  const box = $('import-error');
  box.textContent = msg;
  box.classList.remove('hidden');
}

async function parseImportFiles() {
  const input = $('import-files');
  if (!input.files || !input.files.length) {
    toast('请先选择文件');
    return;
  }
  const btn = $('import-parse-btn');
  btn.disabled = true;
  btn.textContent = '解析中…';
  $('import-error').classList.add('hidden');
  $('import-preview').classList.add('hidden');
  $('import-confirm-btn').disabled = true;

  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  try {
    const res = await fetch('/api/cases/parse', { method: 'POST', body: fd });
    let data;
    try {
      data = await res.json();
    } catch (e) {
      data = { ok: false, error: `服务异常（HTTP ${res.status}）` };
    }
    if (!data.ok) throw new Error(data.error || '解析失败');
    importDraft = data;
    renderImportPreview(data.case || {}, data.warnings || []);
    $('import-confirm-btn').disabled = false;
  } catch (e) {
    importError(e.message || '解析失败');
  } finally {
    btn.disabled = false;
    btn.textContent = '解析';
  }
}

function renderImportPreview(c, warnings) {
  const acts = (c.acts || []).map((a) => {
    const text = a.presentation || '';
    const thumbs = (a.images || []).map((u) =>
      `<img src="${esc(u)}" loading="lazy" alt="">`).join('');
    return `<div class="import-act">
      <div class="import-act-head">
        <span>${esc(a.title || '未命名幕')}</span>
        <span>${text.length} 字 · ${(a.discussion_prompts || []).length} 个问题 · ${(a.images || []).length} 张图</span>
      </div>
      <div class="import-act-body">${esc(text.slice(0, 140))}${text.length > 140 ? '…' : ''}</div>
      ${thumbs ? `<div class="import-thumbs">${thumbs}</div>` : ''}
    </div>`;
  }).join('');
  const warn = (warnings && warnings.length)
    ? `<div class="import-warn">${warnings.map(esc).join('<br>')}</div>` : '';
  $('import-preview').innerHTML = `
    <div class="import-summary"><b>${esc(c.title || '导入案例')}</b> · ${esc(c.course || '')} · ${(c.acts || []).length} 幕</div>
    ${warn}
    <div class="import-acts">${acts}</div>`;
  $('import-preview').classList.remove('hidden');
}

async function confirmImport() {
  if (!importDraft) return;
  const btn = $('import-confirm-btn');
  btn.disabled = true;
  btn.textContent = '导入中…';
  try {
    const data = await api('/api/cases/confirm', { draft_id: importDraft.draft_id });
    if (!data.ok) throw new Error(data.error || '导入失败');
    toast('案例已导入');
    closeImport();
    await loadCases();
  } catch (e) {
    importError(e.message || '导入失败');
    btn.disabled = false;
  } finally {
    btn.textContent = '确认导入';
  }
}

/* ================= 启动 ================= */
(function init() {
  loadCases();
  if (window.speechSynthesis) {
    speechSynthesis.getVoices();
    speechSynthesis.onvoiceschanged = () => speechSynthesis.getVoices();
  }
  $('mic-btn').addEventListener('click', toggleMic);
  const sayBtn = $('say-btn');
  if (sayBtn) sayBtn.addEventListener('click', sendSay);
  const askBtn = $('ask-btn');
  if (askBtn) askBtn.addEventListener('click', () => {
    const buf = state.buffer.join(' ').trim();
    state.buffer = [];
    sendDiscussion(buf || '请助手结合目前讨论给一个引导性问题', true);
  });
  $('release-btn').addEventListener('click', releaseAct);
  $('export-btn').addEventListener('click', exportMinutes);
  $('settings-btn').addEventListener('click', openSettings);
  $('write-btn').addEventListener('click', writeInputToBoard);
  if (window.MindMap) MindMap.init();
  setBoardView('map');
  let mmResizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(mmResizeTimer);
    mmResizeTimer = setTimeout(() => {
      if (state.boardView === 'map' && window.MindMap) MindMap.fit();
    }, 150);
  });
  $('import-files').addEventListener('change', () => {
    $('import-confirm-btn').disabled = true;
    $('import-preview').classList.add('hidden');
  });
  $('say-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendSay();
  });
  $('voice-mode').addEventListener('change', () => {
    if (state.listening) stopListening(true);
    setVoiceState('麦克风未开启', '点击左侧圆点开始聆听');
  });

  if (state.token) {
    api('/api/state').then((data) => {
      if (data.ok) refreshAll().then(() => showDiscuss());
      else { localStorage.removeItem('pbl_token'); state.token = ''; }
    }).catch(() => { localStorage.removeItem('pbl_token'); state.token = ''; });
  }
})();