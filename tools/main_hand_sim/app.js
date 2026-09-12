/* メインハンド干渉シミュレータ — 画面と状態。幾何/段/描画は Geom / Steps / Render に委ねる */
(function () {
  'use strict';

  var STORE_KEY = 'mainhand-sim-v1';
  var $ = function (id) { return document.getElementById(id); };

  /* ---------- 依存チェック（欠けていても真っ白にしない） ---------- */
  var missing = [];
  if (!window.Geom) missing.push('geometry.js -> window.Geom');
  if (!window.Steps) missing.push('steps.js -> window.Steps');
  if (!window.Render) missing.push('render.js -> window.Render');
  if (missing.length) {
    var b = $('boot');
    b.innerHTML = '<div>依存モジュールが読み込めていません。同じディレクトリに置かれているか確認してください。</div>';
    var d = document.createElement('div');
    d.className = 'mono';
    d.style.cssText = 'font-weight:400;margin-top:6px;white-space:pre-wrap';
    d.textContent = missing.join('\n') + (window.__bootErrors.length ? '\n\n' + window.__bootErrors.join('\n') : '');
    b.appendChild(d);
    return;
  }

  var Geom = window.Geom, Steps = window.Steps, Render = window.Render;

  /* ---------- 保存 ---------- */
  function loadStore() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return {};
      var o = JSON.parse(raw);
      return o && typeof o === 'object' ? o : {};
    } catch (e) { return {}; }
  }
  var saveTimer = null;
  function saveStore() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      try {
        localStorage.setItem(STORE_KEY, JSON.stringify({
          params: state.params, positions: state.positions, pose: state.pose, ui: state.ui
        }));
      } catch (e) { /* プライベートウィンドウ等では保存できないだけで動作は続ける */ }
    }, 200);
  }
  function dropStore() { try { localStorage.removeItem(STORE_KEY); } catch (e) {} }

  function clone(v) { try { return JSON.parse(JSON.stringify(v)); } catch (e) { return v; } }
  function num(v, fallback) { var n = parseFloat(v); return isFinite(n) ? n : fallback; }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function fmt(v, dp) {
    if (typeof v !== 'number' || !isFinite(v)) return String(v);
    var s = v.toFixed(dp === undefined ? 1 : dp);
    return s.replace(/\.0+$/, function (m) { return dp === 0 ? '' : m; });
  }

  /* ---------- 範囲 ---------- */
  var R = Steps.RANGES || {};
  var RY = R.y_axis || { min: 0, max: 650 };
  var RR = R.rotate || { min: 0, max: 185 };

  /* ---------- 状態 ---------- */
  var saved = loadStore();
  var defaults = Geom.DEFAULT_PARAMS || {};
  var meta = Geom.PARAM_META || {};

  function mergeParams(base, over) {
    var out = clone(base);
    if (!over || typeof over !== 'object') return out;
    Object.keys(out).forEach(function (k) {
      if (!(k in over)) return;
      if (typeof over[k] === typeof out[k]) out[k] = over[k];
    });
    return out;
  }
  function mergePositions(base, over) {
    var out = clone(base);
    if (!over || typeof over !== 'object') return out;
    ['y_axis', 'rotate'].forEach(function (axis) {
      if (!out[axis] || !over[axis]) return;
      Object.keys(out[axis]).forEach(function (name) {
        var v = over[axis][name];
        if (typeof v === 'number' && isFinite(v)) out[axis][name] = v;
      });
    });
    return out;
  }

  var state = {
    params: mergeParams(defaults, saved.params),
    positions: mergePositions(Steps.POSITIONS || {}, saved.positions),
    pose: {
      y: clamp(num(saved.pose && saved.pose.y, 0), RY.min, RY.max),
      rotate: clamp(num(saved.pose && saved.pose.rotate, 5), RR.min, RR.max),
      wallF: clamp(num(saved.pose && saved.pose.wallF, posVal('wall_f', 'initial', 270)), 80, 270)
    },
    ui: {
      theme: (saved.ui && saved.ui.theme) || 'auto',
      plan: (saved.ui && saved.ui.plan) || 'active',
      bothOnly: !!(saved.ui && saved.ui.bothOnly),
      hitsFirst: !!(saved.ui && saved.ui.hitsFirst),
      csRes: (saved.ui && saved.ui.csRes) || 'normal',
      csLabels: saved.ui && 'csLabels' in saved.ui ? !!saved.ui.csLabels : true
    },
    cs: null,
    csError: '',
    selected: null,
    cursor: null,
    rects: [],
    obstacles: []
  };

  function posVal(axis, name, fallback) {
    var P = Steps.POSITIONS || {};
    var v = P[axis] && P[axis][name];
    return typeof v === 'number' && isFinite(v) ? v : fallback;
  }
  function opts() { return { wallF: state.pose.wallF }; }
  function labelOf(name) {
    for (var i = 0; i < state.obstacles.length; i++) {
      if (state.obstacles[i].name === name) return state.obstacles[i].label || name;
    }
    return name;
  }
  function namesText(names) {
    if (!names || !names.length) return '—';
    return names.map(labelOf).join(', ');
  }

  /* ---------- テーマ（色は CSS の --sim-* を render.js が canvas から読む） ---------- */
  function applyTheme() {
    var t = state.ui.theme;
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
  }

  /* ---------- canvas（DPR と実寸は render.js 側が見るので触らない） ---------- */
  function drawTop() {
    var c = $('top-canvas');
    var hit = safeHitTest(state.pose.y, state.pose.rotate);
    var ghosts = [];
    var s = state.selected;
    if (s && !s.unresolved) {
      ghosts = [
        { y: s.y0, rotate: s.r0, label: '始' },
        { y: s.y1, rotate: s.r1, label: '終' },
        { y: s.y0, rotate: s.r1 },
        { y: s.y1, rotate: s.r0 }
      ];
      if (s.verdict && s.verdict.worst) {
        ghosts.push({ y: s.verdict.worst.y, rotate: s.verdict.worst.rotate, label: '最悪' });
      }
    }
    try {
      $('top-err').textContent = '';
      Render.topView(c, {
        y: state.pose.y,
        rotate: state.pose.rotate,
        wallF: state.pose.wallF,
        params: state.params,
        ghosts: ghosts,
        highlight: hit.names || [],
        yRange: [RY.min, RY.max]
      });
    } catch (e) {
      $('top-err').textContent = '上面図の描画に失敗: ' + (e && e.message);
    }
  }

  function drawCspace() {
    var c = $('cs-canvas');
    if (!state.cs) {
      $('cs-err').textContent = state.csError || 'C 空間を計算中…';
    }
    var rects = visibleRects().filter(function (r) { return !r.unresolved; });
    var sel = -1;
    if (state.selected) {
      for (var i = 0; i < rects.length; i++) if (rects[i].index === state.selected.index) sel = i;
    }
    try {
      if (state.cs) $('cs-err').textContent = '';
      Render.cspace(c, {
        cspace: state.cs,
        params: state.params,
        rects: rects,
        selected: sel >= 0 ? sel : null,
        cursor: state.cursor || { y: state.pose.y, rotate: state.pose.rotate },
        positions: state.positions,
        showLabels: state.ui.csLabels
      });
    } catch (e) {
      $('cs-err').textContent = 'C 空間マップの描画に失敗: ' + (e && e.message);
    }
  }

  var CS_RES = { coarse: { nY: 66, nR: 38 }, normal: { nY: 131, nR: 75 }, fine: { nY: 261, nR: 149 } };
  var csTimer = null;
  function scheduleCspace() {
    if (csTimer) clearTimeout(csTimer);
    $('cs-status').textContent = '再計算待ち…';
    csTimer = setTimeout(function () {
      var res = CS_RES[state.ui.csRes] || CS_RES.normal;
      var t0 = performance.now();
      try {
        state.cs = Geom.cspace(state.params, opts(), res);
        state.csError = '';
        $('cs-status').textContent = res.nY + '×' + res.nR + ' / ' + Math.round(performance.now() - t0) + 'ms';
      } catch (e) {
        state.cs = null;
        state.csError = 'C 空間の計算に失敗: ' + (e && e.message);
        $('cs-status').textContent = '';
      }
      drawCspace();
    }, 120);
  }

  /* ---------- 幾何の呼び出し（失敗しても画面を殺さない） ---------- */
  function safeHitTest(y, r) {
    try {
      var h = Geom.hitTest(y, r, state.params, opts());
      return h && typeof h === 'object' ? h : { hit: false, names: [] };
    } catch (e) { return { hit: false, names: [], error: e && e.message }; }
  }
  function safeSweep(rect, wallF) {
    try {
      var o = { wallF: typeof wallF === 'number' ? wallF : state.pose.wallF };
      var s = Geom.sweepHit({ y0: rect.y0, y1: rect.y1, r0: rect.r0, r1: rect.r1 }, state.params, o);
      return s && typeof s === 'object' ? s : { hit: false, names: [], worst: null };
    } catch (e) { return { hit: false, names: [], worst: null, error: e && e.message }; }
  }

  /* ---------- 1. 校正照合 ---------- */
  function renderCalib() {
    var rows;
    try { rows = Geom.calibration(state.params, opts()) || []; } catch (e) { rows = []; }
    var tb = $('calib-table').tBodies[0];
    tb.textContent = '';
    var allOk = rows.length > 0;
    rows.forEach(function (r) {
      if (!r.ok) allOk = false;
      var tr = document.createElement('tr');
      if (!r.ok) tr.className = 'bad';
      tr.appendChild(td(tag(r.ok ? 'ok' : 'ng', r.ok ? '○ ok' : '× ng')));
      tr.appendChild(td(r.label || ''));
      var p = r.point || {};
      tr.appendChild(td(mono('y=' + fmt(p.y) + ', rot=' + fmt(p.rotate))));
      tr.appendChild(td(r.expectHit ? '干渉する' : '干渉しない'));
      tr.appendChild(td(r.actualHit ? '干渉する' : '干渉しない'));
      tr.appendChild(td(namesText(r.names)));
      tb.appendChild(tr);
    });
    if (!rows.length) {
      var tr2 = document.createElement('tr');
      var c = td('Geom.calibration() が結果を返しませんでした');
      c.colSpan = 6;
      tr2.appendChild(c);
      tb.appendChild(tr2);
    }

    var wb = $('warn-banner');
    wb.textContent = '';
    if (!allOk) {
      var div = document.createElement('div');
      div.className = 'banner';
      div.textContent = 'この寸法は実機と合っていません。下の図の判定は信用できません。'
        + '（校正照合が ' + rows.filter(function (r) { return !r.ok; }).length + ' 件 NG）';
      wb.appendChild(div);
    }
    renderBoundary();
  }

  function renderBoundary() {
    var box = $('boundary');
    box.textContent = '';
    var yHome = posVal2('y_axis', 'home', 0);
    var rHome = posVal2('rotate', 'home', 5);
    var rPlace = posVal2('rotate', 'place', 5);

    var p = document.createElement('div');
    p.innerHTML = '<code>HOME</code> と <code>place</code> は <code>y_axis=' + fmt(yHome)
      + ', rotate=' + fmt(rHome) + '</code> にある。位置定数 yaml の注記は「<code>y_axis = 0</code> では '
      + '<code>rotate &gt;= 10deg</code> なら干渉しない」と言っており、'
      + '<strong>試合中に必ず通る姿勢が、その境界の内側にいる。</strong>';
    box.appendChild(p);

    var dl = document.createElement('dl');
    dl.className = 'kv';
    function add(k, node) {
      var dt = document.createElement('dt'); dt.textContent = k;
      var dd = document.createElement('dd');
      if (typeof node === 'string') dd.textContent = node; else dd.appendChild(node);
      dl.appendChild(dt); dl.appendChild(dd);
    }
    [['HOME (y=' + fmt(yHome) + ', rotate=' + fmt(rHome) + ')', yHome, rHome],
     ['place (y=' + fmt(yHome) + ', rotate=' + fmt(rPlace) + ')', yHome, rPlace],
     ['注記の境界 (y=0, rotate=10)', 0, 10]].forEach(function (row) {
      var h = safeHitTest(row[1], row[2]);
      var span = document.createElement('span');
      span.appendChild(tag(h.hit ? 'ng' : 'ok', h.hit ? '× 干渉' : '○ 安全'));
      span.appendChild(document.createTextNode(' ' + namesText(h.names)));
      add(row[0], span);
    });

    // y=0 で安全になる最小 rotate（朝の 30 分でまず知りたい数値）
    var first = null;
    for (var r = RR.min; r <= RR.max + 1e-9; r += 0.5) {
      if (!safeHitTest(0, r).hit) { first = r; break; }
    }
    add('y=0 で安全になる最小 rotate', mono(first === null ? '見つからない（全角度で干渉）' : fmt(first, 1) + ' deg'));
    var margin = first === null ? null : rHome - first;
    add('HOME の余裕', mono(margin === null ? '—' : (margin >= 0 ? '+' : '') + fmt(margin, 1) + ' deg'
      + (margin < 0 ? '  ← 境界の外（干渉側）' : '')));
    box.appendChild(dl);

    var jump = document.createElement('button');
    jump.type = 'button';
    jump.textContent = 'この姿勢 (y=' + fmt(yHome) + ', rotate=' + fmt(rHome) + ') へ飛ぶ';
    jump.style.marginTop = '6px';
    jump.addEventListener('click', function () { setPose(yHome, rHome); });
    box.appendChild(jump);

    var bad = safeHitTest(yHome, rHome).hit || safeHitTest(yHome, rPlace).hit;
    box.className = 'verdict ' + (bad ? 'ng' : 'ok');
  }
  function posVal2(axis, name, fallback) {
    var v = state.positions[axis] && state.positions[axis][name];
    return typeof v === 'number' && isFinite(v) ? v : fallback;
  }

  /* ---------- 小物 ---------- */
  function td(x) {
    var e = document.createElement('td');
    if (typeof x === 'string') e.textContent = x; else if (x) e.appendChild(x);
    return e;
  }
  function tag(kind, text) {
    var s = document.createElement('span');
    s.className = 'tag ' + kind;
    s.textContent = text;
    return s;
  }
  function mono(text) {
    var s = document.createElement('span');
    s.className = 'mono';
    s.textContent = text;
    return s;
  }

  /* ---------- 2. 凡例 ---------- */
  function renderLegend() {
    try { state.obstacles = Geom.obstacles(state.params, opts()) || []; } catch (e) { state.obstacles = []; }
    var ul = $('legend');
    ul.textContent = '';
    state.obstacles.forEach(function (o) {
      var li = document.createElement('li');
      li.innerHTML = '<code>' + o.name + '</code> ' + (o.label || '')
        + (o.kind === 'work' ? ' [ワーク]' : '');
      ul.appendChild(li);
    });
  }

  /* ---------- 3. 姿勢 ---------- */
  function renderPose() {
    $('y-show').textContent = fmt(state.pose.y) + ' mm';
    $('r-show').textContent = fmt(state.pose.rotate) + ' deg';
    $('w-show').textContent = fmt(state.pose.wallF, 0) + ' deg';
    if ($('y-range').value !== String(state.pose.y)) $('y-range').value = state.pose.y;
    if ($('r-range').value !== String(state.pose.rotate)) $('r-range').value = state.pose.rotate;
    if ($('w-range').value !== String(state.pose.wallF)) $('w-range').value = state.pose.wallF;
    if (document.activeElement !== $('y-num')) $('y-num').value = state.pose.y;
    if (document.activeElement !== $('r-num')) $('r-num').value = state.pose.rotate;
    if (document.activeElement !== $('w-num')) $('w-num').value = state.pose.wallF;

    var h = safeHitTest(state.pose.y, state.pose.rotate);
    var box = $('pose-verdict');
    box.textContent = '';
    box.className = 'verdict ' + (h.hit ? 'ng' : 'ok');
    box.appendChild(tag(h.hit ? 'ng' : 'ok', h.hit ? '× 干渉' : '○ 安全'));
    box.appendChild(document.createTextNode(' '));
    var m = mono('y=' + fmt(state.pose.y) + 'mm  rotate=' + fmt(state.pose.rotate)
      + 'deg  wall_f=' + fmt(state.pose.wallF, 0) + 'deg');
    box.appendChild(m);
    var line = document.createElement('div');
    line.textContent = '相手: ' + namesText(h.names);
    box.appendChild(line);
    if (h.error) {
      var er = document.createElement('div');
      er.className = 'canvas-err';
      er.textContent = 'hitTest 失敗: ' + h.error;
      box.appendChild(er);
    }
  }

  function setPose(y, r, wallF) {
    if (typeof y === 'number' && isFinite(y)) state.pose.y = clamp(y, RY.min, RY.max);
    if (typeof r === 'number' && isFinite(r)) state.pose.rotate = clamp(r, RR.min, RR.max);
    var wallChanged = false;
    if (typeof wallF === 'number' && isFinite(wallF)) {
      var w = clamp(wallF, 80, 270);
      wallChanged = w !== state.pose.wallF;
      state.pose.wallF = w;
    }
    saveStore();
    renderPose();
    drawTop();
    if (wallChanged) { renderCalib(); renderLegend(); renderRects(); scheduleCspace(); }
    else drawCspace();
  }

  function buildChips() {
    var P = Steps.POSITIONS || {};
    function fill(host, axis, apply) {
      host.textContent = '';
      var m = state.positions[axis] || P[axis] || {};
      Object.keys(m).forEach(function (name) {
        if (typeof m[name] !== 'number') return;
        var b = document.createElement('button');
        b.type = 'button';
        b.textContent = name;
        b.title = name + ' = ' + fmt(m[name]);
        b.addEventListener('click', function () { apply(state.positions[axis][name]); });
        host.appendChild(b);
      });
    }
    fill($('y-chips'), 'y_axis', function (v) { setPose(v, undefined); });
    fill($('r-chips'), 'rotate', function (v) { setPose(undefined, v); });
    fill($('w-chips'), 'wall_f', function (v) { setPose(undefined, undefined, v); });
  }

  /* ---------- 4. 矩形領域 ---------- */
  function computeRects() {
    var list;
    try { list = Steps.rectangles(state.ui.plan, state.positions) || []; } catch (e) { list = []; }
    list.forEach(function (r, i) {
      if (r.index == null) r.index = i;
      if (!r.label) r.label = r.moveLabel || r.stepLabel || ('#' + r.index);
      r.verdict = r.unresolved ? null : safeSweep(r, r.wallF);
    });
    state.rects = list;
    if (state.selected) {
      var keep = null;
      for (var i = 0; i < list.length; i++) if (list[i].index === state.selected.index) keep = list[i];
      state.selected = keep;
    }
  }

  function visibleRects() {
    var list = state.rects.slice();
    if (state.ui.bothOnly) list = list.filter(function (r) { return r.movesBothAxes; });
    if (state.ui.hitsFirst) {
      var score = function (r) { return r.unresolved ? 0 : (r.verdict && r.verdict.hit ? 1 : 2); };
      list = list.map(function (r, i) { return [r, i]; })
        .sort(function (a, b) { return score(a[0]) - score(b[0]) || a[1] - b[1]; })
        .map(function (x) { return x[0]; });
    }
    return list;
  }

  function renderRects() {
    computeRects();
    var list = visibleRects();
    var tb = $('rect-table').tBodies[0];
    tb.textContent = '';
    var nHit = 0, nBad = 0;
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      var hit = r.verdict && r.verdict.hit;
      if (r.unresolved) nBad++;
      if (hit) nHit++;
      if (r.unresolved || hit) tr.className = 'bad';
      if (state.selected && state.selected.index === r.index) tr.className += ' sel';

      tr.appendChild(td(mono(String(r.stepIndex != null ? r.stepIndex : r.index))));

      var vcell = document.createElement('td');
      if (r.unresolved) {
        vcell.appendChild(tag('ng', '× 解決不能'));
      } else {
        vcell.appendChild(tag(hit ? 'ng' : 'ok', hit ? '× 干渉' : '○ 安全'));
        if (r.movesBothAxes) { vcell.appendChild(document.createTextNode(' ')); vcell.appendChild(tag('mute', '両軸')); }
        if (r.enabled === false) { vcell.appendChild(document.createTextNode(' ')); vcell.appendChild(tag('warn', '無効')); }
      }
      tr.appendChild(vcell);

      tr.appendChild(td(r.stepLabel || ''));
      tr.appendChild(td(r.moveLabel || ''));
      if (r.unresolved) {
        var ec = td('');
        ec.colSpan = 4;
        ec.appendChild(tag('ng', 'ERROR'));
        ec.appendChild(document.createTextNode(' '));
        var em = mono(r.error || '位置名が解決できません');
        em.style.whiteSpace = 'pre-wrap';
        ec.appendChild(em);
        ec.style.whiteSpace = 'normal';
        tr.appendChild(ec);
      } else {
        tr.appendChild(td(mono(fmt(r.y0) + ', ' + fmt(r.r0))));
        tr.appendChild(td(mono(fmt(r.y1) + ', ' + fmt(r.r1))));
        var w = r.verdict && r.verdict.worst;
        tr.appendChild(td(mono(w ? fmt(w.y) + ', ' + fmt(w.rotate) : '—')));
        tr.appendChild(td(namesText(r.verdict && r.verdict.names)));
      }

      var jc = document.createElement('td');
      if (!r.unresolved) {
        var jb = document.createElement('button');
        jb.type = 'button';
        jb.textContent = '終点へ';
        jb.addEventListener('click', function (ev) {
          ev.stopPropagation();
          setPose(r.y1, r.r1, r.wallF);
        });
        jc.appendChild(jb);
      }
      tr.appendChild(jc);

      tr.addEventListener('click', function () {
        state.selected = state.selected && state.selected.index === r.index ? null : r;
        renderRects();
        drawTop();
        drawCspace();
      });
      tb.appendChild(tr);
    });

    if (!list.length) {
      var tr0 = document.createElement('tr');
      var c0 = td('該当する動作がありません');
      c0.colSpan = 9;
      tr0.appendChild(c0);
      tb.appendChild(tr0);
    }
    $('rect-summary').textContent = list.length + ' 動作 / 干渉 ' + nHit + ' 件 / 解決不能 ' + nBad + ' 件';
  }

  /* ---------- 5. 寸法 ---------- */
  var NOTES = {
    armRadius: { kind: 'warn', text: 'CAD 実測 X=63.8 / Y=4.2 から √(63.8²+4.2²)≈63.9 と読んだ値' },
    rotateZeroDir: { kind: 'ng', text: 'rotate=0 の向き。まだ実機で確認していない仮定' }
  };
  var GROUP_ORDER = ['ハンド', 'ワーク棚', 'コンベア', '支柱・端部', 'wall_f', 'ワーク'];

  var paramTimer = null;
  function onParamEdit() {
    saveStore();
    if (paramTimer) clearTimeout(paramTimer);
    paramTimer = setTimeout(function () {
      renderCalib();
      renderLegend();
      renderRects();
      renderPose();
      drawTop();
    }, 20);
    scheduleCspace();
  }

  function renderParams() {
    var host = $('params');
    host.textContent = '';
    var keys = Object.keys(state.params);
    var groups = {};
    keys.forEach(function (k) {
      var g = (meta[k] && meta[k].group) || 'その他';
      (groups[g] = groups[g] || []).push(k);
    });
    var names = GROUP_ORDER.filter(function (g) { return groups[g]; })
      .concat(Object.keys(groups).filter(function (g) { return GROUP_ORDER.indexOf(g) < 0; }));

    names.forEach(function (g, gi) {
      var det = document.createElement('details');
      det.className = 'group';
      if (gi < 2) det.open = true;
      var sum = document.createElement('summary');
      sum.textContent = g + ' (' + groups[g].length + ')';
      det.appendChild(sum);
      var grid = document.createElement('div');
      grid.className = 'pgrid';
      groups[g].forEach(function (k) { grid.appendChild(paramField(k)); });
      det.appendChild(grid);
      host.appendChild(det);
    });
  }

  function paramField(k) {
    var m = meta[k] || {};
    var wrap = document.createElement('div');
    wrap.className = 'pfield';
    var lab = document.createElement('label');
    lab.htmlFor = 'p-' + k;
    lab.textContent = (m.label || k) + (m.unit ? ' [' + m.unit + ']' : '');
    var note = NOTES[k];
    if (note) {
      lab.appendChild(document.createTextNode(' '));
      var t = tag(note.kind, note.kind === 'ng' ? '未確認の仮定' : '推定値');
      t.title = note.text;
      lab.appendChild(t);
    }
    wrap.appendChild(lab);

    var v = state.params[k];
    var input;
    if (typeof v === 'boolean') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = v;
      input.addEventListener('change', function () { state.params[k] = input.checked; onParamEdit(); });
    } else if (typeof v === 'number') {
      input = document.createElement('input');
      input.type = 'number';
      input.step = m.step || 'any';
      input.value = v;
      input.addEventListener('input', function () {
        var n = parseFloat(input.value);
        if (isFinite(n)) { state.params[k] = n; onParamEdit(); }
      });
    } else {
      input = document.createElement('input');
      input.type = 'text';
      input.value = v === null || v === undefined ? '' : String(v);
      input.addEventListener('input', function () { state.params[k] = input.value; onParamEdit(); });
    }
    input.id = 'p-' + k;
    input.title = k + (note ? '\n' + note.text : '');
    wrap.appendChild(input);
    if (note) {
      var n2 = document.createElement('div');
      n2.className = 'note';
      n2.style.fontSize = '11px';
      n2.textContent = note.text;
      wrap.appendChild(n2);
    }
    return wrap;
  }

  /* ---------- 6. 位置定数 / yaml ---------- */
  function renderPositions() {
    var host = $('positions');
    host.textContent = '';
    ['y_axis', 'rotate'].forEach(function (axis) {
      var m = state.positions[axis];
      if (!m) return;
      var det = document.createElement('details');
      det.className = 'group';
      det.open = true;
      var sum = document.createElement('summary');
      sum.textContent = axis + ' (' + Object.keys(m).length + ')';
      det.appendChild(sum);
      var grid = document.createElement('div');
      grid.className = 'pgrid';
      Object.keys(m).forEach(function (name) {
        if (typeof m[name] !== 'number') return;
        var wrap = document.createElement('div');
        wrap.className = 'pfield';
        var lab = document.createElement('label');
        lab.textContent = name;
        lab.htmlFor = 'pos-' + axis + '-' + name;
        wrap.appendChild(lab);
        var input = document.createElement('input');
        input.type = 'number';
        input.step = '0.5';
        input.id = 'pos-' + axis + '-' + name;
        input.value = m[name];
        input.addEventListener('input', function () {
          var n = parseFloat(input.value);
          if (!isFinite(n)) return;
          state.positions[axis][name] = n;
          saveStore();
          renderRects();
          renderBoundary();
          renderYaml();
          drawTop();
          drawCspace();
        });
        wrap.appendChild(input);
        grid.appendChild(wrap);
      });
      det.appendChild(grid);
      host.appendChild(det);
    });
    renderYaml();
  }

  function yamlNum(v) {
    var s = String(v);
    return /[.eE]/.test(s) ? s : s + '.0';
  }
  function yamlValue(v) {
    if (v && typeof v === 'object') {
      return '{ ' + Object.keys(v).map(function (k) { return k + ': ' + yamlValue(v[k]); }).join(', ') + ' }';
    }
    if (typeof v === 'number') return yamlNum(v);
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return String(v);
  }
  function yamlText() {
    var order = ['rotate', 'y_axis', 'gripper', 'conveyor', 'wall_f'];
    var axes = order.filter(function (a) { return state.positions[a]; })
      .concat(Object.keys(state.positions).filter(function (a) { return order.indexOf(a) < 0; }));
    var out = ['positions:'];
    axes.forEach(function (axis, i) {
      if (i) out.push('');
      out.push('  ' + axis + ':');
      var m = state.positions[axis];
      Object.keys(m).forEach(function (name) {
        out.push('    ' + name + ': ' + yamlValue(m[name]));
      });
    });
    return out.join('\n') + '\n';
  }
  function renderYaml() { $('yaml').value = yamlText(); }

  /* ---------- 配線 ---------- */
  function wire() {
    $('y-range').min = RY.min; $('y-range').max = RY.max; $('y-range').step = 0.5;
    $('r-range').min = RR.min; $('r-range').max = RR.max; $('r-range').step = 0.5;
    $('y-num').min = RY.min; $('y-num').max = RY.max;
    $('r-num').min = RR.min; $('r-num').max = RR.max;

    $('y-range').addEventListener('input', function () { setPose(num(this.value, 0), undefined); });
    $('r-range').addEventListener('input', function () { setPose(undefined, num(this.value, 0)); });
    $('w-range').addEventListener('input', function () { setPose(undefined, undefined, num(this.value, 270)); });
    $('y-num').addEventListener('input', function () { setPose(num(this.value, state.pose.y), undefined); });
    $('r-num').addEventListener('input', function () { setPose(undefined, num(this.value, state.pose.rotate)); });
    $('w-num').addEventListener('input', function () { setPose(undefined, undefined, num(this.value, state.pose.wallF)); });

    $('plan').value = state.ui.plan;
    $('plan').addEventListener('change', function () {
      state.ui.plan = this.value; state.selected = null; saveStore(); renderRects(); drawTop(); drawCspace();
    });
    $('f-both').checked = state.ui.bothOnly;
    $('f-both').addEventListener('change', function () {
      state.ui.bothOnly = this.checked; saveStore(); renderRects(); drawCspace();
    });
    var fh = $('f-hits');
    fh.setAttribute('aria-pressed', String(state.ui.hitsFirst));
    fh.addEventListener('click', function () {
      state.ui.hitsFirst = !state.ui.hitsFirst;
      fh.setAttribute('aria-pressed', String(state.ui.hitsFirst));
      saveStore(); renderRects();
    });
    $('f-clear').addEventListener('click', function () {
      state.selected = null; renderRects(); drawTop(); drawCspace();
    });

    $('cs-res').value = state.ui.csRes;
    $('cs-res').addEventListener('change', function () {
      state.ui.csRes = this.value; saveStore(); scheduleCspace();
    });
    $('cs-labels').checked = state.ui.csLabels;
    $('cs-labels').addEventListener('change', function () {
      state.ui.csLabels = this.checked; saveStore(); drawCspace();
    });

    var cs = $('cs-canvas');
    cs.addEventListener('click', function (ev) {
      var p = null;
      try {
        p = Render.hitTestCspace(cs, { cspace: state.cs, params: state.params, positions: state.positions },
          ev.clientX, ev.clientY);
      } catch (e) { p = null; }
      if (p && isFinite(p.y) && isFinite(p.rotate)) setPose(p.y, p.rotate);
    });
    var raf = 0;
    cs.addEventListener('mousemove', function (ev) {
      if (raf) return;
      var x = ev.clientX, y = ev.clientY;
      raf = requestAnimationFrame(function () {
        raf = 0;
        var p = null;
        try {
          p = Render.hitTestCspace(cs, { cspace: state.cs, params: state.params, positions: state.positions }, x, y);
        } catch (e) { p = null; }
        state.cursor = p && isFinite(p.y) ? p : null;
        drawCspace();
      });
    });
    cs.addEventListener('mouseleave', function () { state.cursor = null; drawCspace(); });

    $('btn-params-reset').addEventListener('click', function () {
      state.params = clone(defaults);
      saveStore(); renderParams(); renderCalib(); renderLegend(); renderRects(); renderPose(); drawTop(); scheduleCspace();
    });
    $('btn-pos-reset').addEventListener('click', function () {
      state.positions = clone(Steps.POSITIONS || {});
      saveStore(); renderPositions(); buildChips(); renderRects(); renderBoundary(); drawTop(); drawCspace();
    });
    $('btn-clear-store').addEventListener('click', function () {
      dropStore();
      $('copy-status').textContent = '保存を消しました（再読み込みで既定値）';
    });
    $('btn-theme').addEventListener('click', function () {
      state.ui.theme = state.ui.theme === 'dark' ? 'light' : state.ui.theme === 'light' ? 'auto' : 'dark';
      applyTheme();
      saveStore();
      $('btn-theme').textContent = '明暗切替 (' + state.ui.theme + ')';
      drawTop(); drawCspace();
    });
    $('btn-theme').textContent = '明暗切替 (' + state.ui.theme + ')';

    $('btn-copy').addEventListener('click', function () {
      var ta = $('yaml');
      var done = function () { $('copy-status').textContent = 'コピーしました'; };
      var fallback = function () {
        ta.removeAttribute('readonly');
        ta.focus();
        ta.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        ta.setAttribute('readonly', '');
        $('copy-status').textContent = ok ? 'コピーしました' : '選択しました。手動でコピーしてください';
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(ta.value).then(done, fallback);
      } else fallback();
    });

    var ro = window.ResizeObserver ? new ResizeObserver(function () { drawTop(); drawCspace(); }) : null;
    if (ro) { ro.observe($('top-canvas')); ro.observe(cs); }
    else window.addEventListener('resize', function () { drawTop(); drawCspace(); });

    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onMq = function () { drawTop(); drawCspace(); };
      if (mq.addEventListener) mq.addEventListener('change', onMq);
      else if (mq.addListener) mq.addListener(onMq);
    }
  }

  /* ---------- 起動 ---------- */
  applyTheme();
  $('boot').hidden = true;
  $('app').hidden = false;
  wire();
  renderLegend();
  renderCalib();
  buildChips();
  renderParams();
  renderPositions();
  renderRects();
  renderPose();
  drawTop();
  scheduleCspace();
})();
