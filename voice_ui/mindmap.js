/* ============================================================
   PBL 白板 —— 思维导图渲染器（原生 SVG，无外部依赖）
   中心 = 案例；分支 = 假设 / 知识点 / 议题 / 导师提示。
   支持平移、缩放、折叠分支、点击节点查看详情、新节点高亮。
   ============================================================ */
'use strict';

window.MindMap = (function () {
  const CATS = [
    { key: 'hypothesis', label: '假设',     color: '#8B9DFF', side: 1 },
    { key: 'knowledge',  label: '知识点',   color: '#3DDC97', side: 1 },
    { key: 'issue',      label: '议题',     color: '#FCD34D', side: -1 },
    { key: 'insight',    label: '导师提示', color: '#C084FC', side: -1 },
  ];
  const STATUS_COLOR = { proposed: '#FCD34D', supported: '#3DDC97', refuted: '#FB7185', pending: '#A8B6CE' };

  const ROOT_W = 200, ROOT_H = 68;
  const CAT_W = 132, CAT_H = 44;
  const LEAF_W = 196, LEAF_H = 38;
  const ROW_H = 50, CAT_GAP = 40, CAT_X = 258, LEAF_X = 520;

  let svg, viewport, linksG, nodesG, container;
  let view = { x: 0, y: 0, k: 1 };
  let collapsed = new Set();
  let fitted = false;
  let highlight = null;
  let drag = null;
  let bound = false;

  function trunc(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function buildCategories(data) {
    const canvas = data.canvas || [];
    const map = {
      hypothesis: (data.board || []).map(h => ({ id: h.id, type: 'hypothesis', title: h.text.replace(/^假设：/, ''), status: h.status, ev: (h.evidence || []).length })),
      knowledge: canvas.filter(n => n.type === 'knowledge').map(n => ({ id: n.id, type: 'knowledge', title: n.text })),
      issue: (data.issues || []).map(i => ({ id: i.id, type: 'issue', title: i.text.replace(/^议题：/, ''), status: i.status })),
      insight: canvas.filter(n => n.type === 'insight').map(n => ({ id: n.id, type: 'insight', title: n.text.replace(/^导师：/, '') })),
    };
    return CATS.map(c => ({ ...c, items: map[c.key] || [] }));
  }

  function layoutSide(cats, dir) {
    let totalH = 0;
    cats.forEach(c => {
      const n = collapsed.has(c.key) ? 1 : Math.max(c.items.length, 1);
      c.h = n * ROW_H;
      totalH += c.h;
    });
    totalH += CAT_GAP * Math.max(cats.length - 1, 0);
    let y = -totalH / 2;
    cats.forEach(c => {
      c.x = dir * CAT_X;
      c.cy = y + c.h / 2;
      if (!collapsed.has(c.key)) {
        let ly = y + ROW_H / 2;
        c.items.forEach(it => { it.x = dir * LEAF_X; it.y = ly; ly += ROW_H; });
      }
      y += c.h + CAT_GAP;
    });
  }

  function curve(x0, y0, x1, y1) {
    const dx = (x1 - x0) * 0.5;
    return `M ${x0} ${y0} C ${x0 + dx} ${y0}, ${x1 - dx} ${y1}, ${x1} ${y1}`;
  }

  function nodeRect(w, h, fill, stroke, extra) {
    return `<rect x="${-w / 2}" y="${-h / 2}" width="${w}" height="${h}" rx="${h / 2 > 14 ? 12 : h / 2}" fill="${fill}" stroke="${stroke}" stroke-width="1.2" ${extra || ''}/>`;
  }

  function render(data, opts) {
    if (!svg) return;
    opts = opts || {};
    if (opts.highlight) { highlight = opts.highlight; collapsed.delete(highlight.type); }
    const cats = buildCategories(data);
    const right = cats.filter(c => c.side === 1);
    const left = cats.filter(c => c.side === -1);
    layoutSide(right, 1);
    layoutSide(left, -1);

    const caseTitle = (data.case && data.case.title) || '案例';
    let links = '', nodes = '';
    let minX = -ROOT_W / 2, maxX = ROOT_W / 2, minY = -ROOT_H / 2, maxY = ROOT_H / 2;
    const track = (x, y, w, h) => {
      minX = Math.min(minX, x - w / 2); maxX = Math.max(maxX, x + w / 2);
      minY = Math.min(minY, y - h / 2); maxY = Math.max(maxY, y + h / 2);
    };

    // 根节点
    nodes += `<g class="mm-node mm-root" data-node="root" transform="translate(0,0)">
      ${nodeRect(ROOT_W, ROOT_H, 'url(#mmRootGrad)', 'rgba(139,157,255,0.55)', 'filter="url(#mmGlow)"')}
      <text class="mm-root-label" text-anchor="middle" y="-6">${esc(trunc(caseTitle, 12))}</text>
      <text class="mm-root-sub" text-anchor="middle" y="14">PBL 讨论白板</text>
    </g>`;
    track(0, 0, ROOT_W, ROOT_H);

    cats.forEach(c => {
      const dir = c.side;
      const isCollapsed = collapsed.has(c.key);
      // 根 → 分支
      links += `<path class="mm-link" d="${curve(dir * ROOT_W / 2, 0, c.x - dir * CAT_W / 2, c.cy)}" stroke="${c.color}" stroke-opacity="0.4"/>`;
      // 分支节点
      const count = c.items.length;
      nodes += `<g class="mm-node mm-cat" data-node="cat:${c.key}" transform="translate(${c.x},${c.cy})">
        ${nodeRect(CAT_W, CAT_H, hexA(c.color, 0.14), hexA(c.color, 0.5))}
        <circle cx="${-CAT_W / 2 + 16}" cy="0" r="4" fill="${c.color}"/>
        <text class="mm-cat-label" x="${-CAT_W / 2 + 28}" y="4">${c.label}</text>
        <text class="mm-cat-count" x="${CAT_W / 2 - 14}" y="4" text-anchor="end">${isCollapsed ? '▸ ' + count : count}</text>
      </g>`;
      track(c.x, c.cy, CAT_W, CAT_H);

      if (!isCollapsed) {
        c.items.forEach(it => {
          links += `<path class="mm-link" d="${curve(c.x + dir * CAT_W / 2, c.cy, it.x - dir * LEAF_W / 2, it.y)}" stroke="${c.color}" stroke-opacity="0.28"/>`;
          const dotColor = it.type === 'hypothesis' ? (STATUS_COLOR[it.status] || c.color) : c.color;
          const isHl = highlight && highlight.type === it.type && String(highlight.id) === String(it.id);
          const done = (it.type === 'hypothesis' && it.status === 'refuted') || (it.type === 'issue' && it.status === 'resolved');
          const evBadge = it.type === 'hypothesis' && it.ev ? `<text class="mm-leaf-ev" x="${LEAF_W / 2 - 12}" y="4" text-anchor="end">${it.ev}依据</text>` : '';
          nodes += `<g class="mm-node mm-leaf ${isHl ? 'mm-hl' : ''} ${done ? 'mm-done' : ''}" data-node="${it.type}:${it.id}" transform="translate(${it.x},${it.y})">
            ${nodeRect(LEAF_W, LEAF_H, 'rgba(22,32,58,0.85)', hexA(c.color, isHl ? 0.9 : 0.3), isHl ? 'filter="url(#mmGlow)"' : '')}
            <circle cx="${-LEAF_W / 2 + 15}" cy="0" r="4" fill="${dotColor}"/>
            <text class="mm-leaf-label" x="${-LEAF_W / 2 + 27}" y="4">${esc(trunc(it.title, 11))}</text>
            ${evBadge}
            <title>${esc(it.title)}</title>
          </g>`;
          track(it.x, it.y, LEAF_W, LEAF_H);
        });
      }
    });

    linksG.innerHTML = links;
    nodesG.innerHTML = nodes;
    svg._bbox = { minX, maxX, minY, maxY };
    if (!fitted || opts.fit || opts.highlight) {
      fit();
      if (container.clientWidth > 1 && container.clientHeight > 1) fitted = true;
    } else {
      applyTransform();
    }
    if (highlight) {
      // 高亮节点所在分支自动展开已在 buildCategories 前处理；此处清除标记延时
      setTimeout(() => { highlight = null; const el = nodesG.querySelector('.mm-hl'); if (el) el.classList.remove('mm-hl'); }, 2600);
    }
  }

  function hexA(hex, a) {
    const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  function applyTransform() {
    viewport.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);
  }

  function fit() {
    if (!svg || !svg._bbox) return;
    const cw = container.clientWidth, ch = container.clientHeight;
    if (cw < 2 || ch < 2) return;
    const b = svg._bbox;
    const w = b.maxX - b.minX + 80, h = b.maxY - b.minY + 80;
    const k = Math.min(cw / w, ch / h, 1.15);
    view.k = k;
    view.x = cw / 2 - k * (b.minX + b.maxX) / 2;
    view.y = ch / 2 - k * (b.minY + b.maxY) / 2;
    applyTransform();
  }

  function zoomAt(factor, cx, cy) {
    const nk = Math.max(0.3, Math.min(view.k * factor, 2.5));
    const ratio = nk / view.k;
    view.x = cx - (cx - view.x) * ratio;
    view.y = cy - (cy - view.y) * ratio;
    view.k = nk;
    applyTransform();
  }

  function bind() {
    if (bound || !svg) return;
    bound = true;
    svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1.12 : 0.89, e.clientX - rect.left, e.clientY - rect.top);
    }, { passive: false });

    svg.addEventListener('pointerdown', (e) => {
      if (e.target.closest('[data-node]')) return;
      drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
      svg.setPointerCapture(e.pointerId);
      svg.classList.add('mm-grabbing');
    });
    svg.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      view.x = drag.vx + dx; view.y = drag.vy + dy;
      applyTransform();
    });
    const endDrag = (e) => { if (drag) { drag = null; svg.classList.remove('mm-grabbing'); } };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);

    svg.addEventListener('click', (e) => {
      const g = e.target.closest('[data-node]');
      if (!g) return;
      const key = g.getAttribute('data-node');
      if (key === 'root') { fit(); return; }
      if (key.startsWith('cat:')) {
        const ck = key.slice(4);
        if (collapsed.has(ck)) collapsed.delete(ck); else collapsed.add(ck);
        render(window.__mmLastData || { canvas: [] });
        return;
      }
      const idx = key.indexOf(':');
      const type = key.slice(0, idx), id = key.slice(idx + 1);
      if (typeof openNodeDetail === 'function') openNodeDetail(type, id);
    });
  }

  function init() {
    container = document.getElementById('mindmap-view');
    svg = document.getElementById('mm-svg');
    if (!svg) return;
    viewport = document.getElementById('mm-viewport');
    linksG = document.getElementById('mm-links');
    nodesG = document.getElementById('mm-nodes');
    bind();
  }

  return {
    init,
    render(data, opts) { window.__mmLastData = data; render(data, opts); },
    fit,
    zoomIn() { const r = svg.getBoundingClientRect(); zoomAt(1.2, r.width / 2, r.height / 2); },
    zoomOut() { const r = svg.getBoundingClientRect(); zoomAt(0.83, r.width / 2, r.height / 2); },
    expandAll() { collapsed.clear(); if (window.__mmLastData) render(window.__mmLastData); },
    collapseAll() { CATS.forEach(c => collapsed.add(c.key)); if (window.__mmLastData) render(window.__mmLastData); },
    reset() { fitted = false; collapsed.clear(); },
  };
})();