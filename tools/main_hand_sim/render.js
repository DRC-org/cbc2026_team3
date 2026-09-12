/*
 * メインハンド干渉シミュレータの描画レイヤ (window.Render)。
 * 色は opts.theme > canvas の CSS カスタムプロパティ (--sim-fg / --sim-obstacle / --sim-owner-0.. 等) > 既定色 の順で解決する。
 * cspace.cells の並びは rotate 行 × y 列 (cells[iR * nY + iY]) を既定とする (cspace.order === 'y-major' なら転置扱い)。
 */
(function (global) {
  'use strict';

  var DOC = global.document;

  // ---------------------------------------------------------------- 色

  var TOKENS = {
    fg: '--sim-fg',
    muted: '--sim-muted',
    grid: '--sim-grid',
    gridStrong: '--sim-grid-strong',
    plotBg: '--sim-plot-bg',
    frame: '--sim-frame',
    obstacle: '--sim-obstacle',
    obstacleEdge: '--sim-obstacle-edge',
    work: '--sim-work',
    wall: '--sim-wall',
    rail: '--sim-rail',
    carriage: '--sim-carriage',
    arm: '--sim-arm',
    hand: '--sim-hand',
    ghost: '--sim-ghost',
    hit: '--sim-hit',
    safe: '--sim-safe',
    cursor: '--sim-cursor',
    calib: '--sim-calib',
    halo: '--sim-halo'
  };

  // 明暗どちらのテーマでも読める中間トーンの既定色
  var DEFAULTS = {
    fg: '#8d95a3',
    muted: '#79808c',
    grid: 'rgba(127,133,145,0.22)',
    gridStrong: 'rgba(127,133,145,0.5)',
    plotBg: 'rgba(127,133,145,0.10)',
    frame: 'rgba(127,133,145,0.7)',
    obstacle: 'rgba(127,133,145,0.55)',
    obstacleEdge: 'rgba(127,133,145,0.95)',
    work: '#3f9ec4',
    wall: '#d1822c',
    rail: 'rgba(127,133,145,0.8)',
    carriage: '#8d95a3',
    arm: '#6b83e8',
    hand: '#2fab9b',
    ghost: 'rgba(127,133,145,0.55)',
    hit: '#e05252',
    safe: '#3fa66b',
    cursor: '#e8c34a',
    calib: '#bf3fa8',
    // 干渉領域と同系色の枠が沈まないように敷く縁取り
    halo: 'rgba(128,132,140,0.75)'
  };

  var OWNER_DEFAULTS = [
    '#d9534f', '#d1822c', '#b8a12a', '#4b9f5e',
    '#2f8fa8', '#5a6fd0', '#9a5fc0', '#b4626a'
  ];

  var FONT = '10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
  var FONT_BOLD = 'bold 10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
  var FONT_SMALL = '9px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

  // C 空間マップで格子線を引く位置名 (全部引くと潰れるので主要なものだけ)
  var KEY_Y_POSITIONS = ['home', 'clear', 'work_1', 'work_2', 'work_3', 'work_shared'];
  var KEY_R_POSITIONS = ['place', 'after_place', 'work_1_after_2', 'pick'];

  // 実機で分かっている答え合わせ点
  var CALIB_POINTS = [
    { y: 0, rotate: 0, label: '0/0' },
    { y: 40, rotate: 0, label: '40/0' },
    { y: 0, rotate: 10, label: '0/10' }
  ];

  function cssVar(cs, name) {
    if (!cs || !name) return '';
    var v = '';
    try { v = cs.getPropertyValue(name); } catch (e) { v = ''; }
    return v ? String(v).trim() : '';
  }

  function resolveColors(canvas, opts) {
    var theme = (opts && opts.theme) || {};
    var cs = null;
    try {
      if (global.getComputedStyle && canvas) cs = global.getComputedStyle(canvas);
    } catch (e) { cs = null; }
    var out = {};
    Object.keys(DEFAULTS).forEach(function (k) {
      out[k] = theme[k] || cssVar(cs, TOKENS[k]) || DEFAULTS[k];
    });
    var themeOwners = theme.owners || [];
    var owners = [];
    for (var i = 0; i < OWNER_DEFAULTS.length; i++) {
      owners.push(themeOwners[i] || theme['owner' + i] || cssVar(cs, '--sim-owner-' + i) || OWNER_DEFAULTS[i]);
    }
    out.owners = owners;
    // halo の指定が無ければ plot 背景色を流用する (明暗どちらでも網目から線を切り離せる)
    if (!theme.halo && !cssVar(cs, TOKENS.halo)) {
      var pb = theme.plotBg || cssVar(cs, TOKENS.plotBg);
      if (pb) out.halo = pb;
    }
    return out;
  }

  var _probe = null;
  var _rgbCache = {};

  function toRgb(color) {
    if (_rgbCache[color]) return _rgbCache[color];
    var rgb = [140, 145, 155];
    try {
      if (!_probe && DOC) {
        var c = DOC.createElement('canvas');
        c.width = 1; c.height = 1;
        _probe = c.getContext('2d');
      }
      if (_probe) {
        _probe.clearRect(0, 0, 1, 1);
        _probe.fillStyle = '#808080';
        _probe.fillStyle = color;
        _probe.fillRect(0, 0, 1, 1);
        var d = _probe.getImageData(0, 0, 1, 1).data;
        rgb = [d[0], d[1], d[2]];
      }
    } catch (e) { /* getImageData が使えない環境では既定のグレー */ }
    _rgbCache[color] = rgb;
    return rgb;
  }

  // 色覚に依らず区別が付くよう、干渉領域は色 + 網パターンで分ける
  function patternMask(pid, px, py) {
    switch (pid & 3) {
      case 0: return 1;
      case 1: return ((px + py) & 3) < 2 ? 1 : 0.4;
      case 2: return ((px - py + 1024) & 3) < 2 ? 1 : 0.4;
      default: return ((px & 1) === (py & 1)) ? 1 : 0.35;
    }
  }

  var _patCache = {};

  function maskPattern(ctx, color, pid) {
    var key = color + '|' + pid;
    if (_patCache[key]) return _patCache[key];
    var pat = color;
    try {
      var tile = 4;
      var c = DOC.createElement('canvas');
      c.width = tile; c.height = tile;
      var g = c.getContext('2d');
      var img = g.createImageData(tile, tile);
      var rgb = toRgb(color);
      for (var py = 0; py < tile; py++) {
        for (var px = 0; px < tile; px++) {
          var o = (py * tile + px) * 4;
          img.data[o] = rgb[0];
          img.data[o + 1] = rgb[1];
          img.data[o + 2] = rgb[2];
          img.data[o + 3] = Math.round(235 * patternMask(pid, px, py));
        }
      }
      g.putImageData(img, 0, 0);
      pat = ctx.createPattern(c, 'repeat') || color;
    } catch (e) { pat = color; }
    _patCache[key] = pat;
    return pat;
  }

  function ownerStyle(colors, owner) {
    var i = Math.max(0, (owner | 0) - 1);
    return {
      color: colors.owners[i % colors.owners.length],
      pid: i & 3
    };
  }

  // ------------------------------------------------------------ canvas

  function prepare(canvas) {
    var dpr = global.devicePixelRatio || 1;
    var w = 0, h = 0;
    try {
      var r = canvas.getBoundingClientRect();
      w = r.width; h = r.height;
    } catch (e) { /* レイアウト前 */ }
    if (!w) w = canvas.clientWidth || canvas.width || 640;
    if (!h) h = canvas.clientHeight || canvas.height || 400;
    var pw = Math.max(1, Math.round(w * dpr));
    var ph = Math.max(1, Math.round(h * dpr));
    if (canvas.width !== pw) canvas.width = pw;
    if (canvas.height !== ph) canvas.height = ph;
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.lineJoin = 'round';
    ctx.lineCap = 'butt';
    return { ctx: ctx, w: w, h: h, dpr: dpr };
  }

  function cssSize(canvas) {
    var w = 0, h = 0;
    try {
      var r = canvas.getBoundingClientRect();
      w = r.width; h = r.height;
    } catch (e) { /* レイアウト前 */ }
    if (!w) w = canvas.clientWidth || canvas.width || 640;
    if (!h) h = canvas.clientHeight || canvas.height || 400;
    return { w: w, h: h };
  }

  function notice(canvas, message) {
    var p = prepare(canvas);
    var c = resolveColors(canvas, null);
    p.ctx.fillStyle = c.muted;
    p.ctx.font = FONT;
    p.ctx.textAlign = 'center';
    p.ctx.textBaseline = 'middle';
    p.ctx.fillText(message, p.w / 2, p.h / 2);
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function num(v, def) { return (typeof v === 'number' && isFinite(v)) ? v : def; }

  function textW(ctx, t) {
    return ctx.measureText ? ctx.measureText(t).width : String(t).length * 6;
  }

  function dashed(ctx, pattern) {
    if (ctx.setLineDash) ctx.setLineDash(pattern || []);
  }

  function label(ctx, text, x, y, color, align, baseline, font) {
    ctx.fillStyle = color;
    ctx.font = font || FONT;
    ctx.textAlign = align || 'left';
    ctx.textBaseline = baseline || 'alphabetic';
    ctx.fillText(text, x, y);
  }

  // 干渉領域の上に載る文字は縁取りしないと読めない
  function labelOn(ctx, text, x, y, color, align, baseline, font, halo) {
    ctx.font = font || FONT;
    ctx.textAlign = align || 'left';
    ctx.textBaseline = baseline || 'alphabetic';
    if (halo && ctx.strokeText) {
      dashed(ctx, []);
      ctx.strokeStyle = halo;
      ctx.lineWidth = 2;
      ctx.strokeText(text, x, y);
    }
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  // --------------------------------------------------------- 上面図

  function polyPath(ctx, poly, tf) {
    ctx.beginPath();
    for (var i = 0; i < poly.length; i++) {
      var s = tf(poly[i][0], poly[i][1]);
      if (i === 0) ctx.moveTo(s[0], s[1]);
      else ctx.lineTo(s[0], s[1]);
    }
    ctx.closePath();
  }

  function growBoundsPoly(b, poly) {
    for (var i = 0; i < poly.length; i++) {
      var x = poly[i][0], y = poly[i][1];
      if (x < b.x0) b.x0 = x;
      if (x > b.x1) b.x1 = x;
      if (y < b.y0) b.y0 = y;
      if (y > b.y1) b.y1 = y;
    }
  }

  function growBoundsPoint(b, x, y) {
    if (x < b.x0) b.x0 = x;
    if (x > b.x1) b.x1 = x;
    if (y < b.y0) b.y0 = y;
    if (y > b.y1) b.y1 = y;
  }

  function isWall(ob) {
    return ob.kind === 'wall' || /wall/.test(String(ob.name || ''));
  }

  function reachRadius(params, carriageY) {
    var G = global.Geom;
    var r = 0;
    for (var a = 0; a <= 185; a += 5) {
      var parts;
      try { parts = G.handParts(carriageY, a, params) || []; } catch (e) { parts = []; }
      for (var i = 0; i < parts.length; i++) {
        var poly = parts[i].poly || [];
        for (var j = 0; j < poly.length; j++) {
          var dx = poly[j][0];
          var dy = poly[j][1] - carriageY;
          var d = Math.sqrt(dx * dx + dy * dy);
          if (d > r) r = d;
        }
      }
    }
    return r;
  }

  function topView(canvas, opts) {
    opts = opts || {};
    var G = global.Geom;
    if (!canvas) return;
    if (!G || !G.handParts || !G.obstacles) {
      notice(canvas, 'geometry.js 未読み込み');
      return;
    }
    var p = prepare(canvas);
    var ctx = p.ctx;
    var colors = resolveColors(canvas, opts);
    var params = opts.params || G.DEFAULT_PARAMS || {};
    var y = num(opts.y, 0);
    var rotate = num(opts.rotate, 0);
    var wallF = num(opts.wallF, 180);
    var geomOpts = { wallF: wallF };
    var highlight = opts.highlight || [];
    var ghosts = opts.ghosts || [];

    var obstacles = [];
    try { obstacles = G.obstacles(params, geomOpts) || []; } catch (e) { obstacles = []; }
    var parts = [];
    try { parts = G.handParts(y, rotate, params) || []; } catch (e) { parts = []; }

    var yRange = opts.yRange || [0, 650];
    var reach = reachRadius(params, y) || 200;

    // ---- 表示範囲
    var b = { x0: Infinity, x1: -Infinity, y0: Infinity, y1: -Infinity };
    obstacles.forEach(function (ob) { if (ob && ob.poly) growBoundsPoly(b, ob.poly); });
    parts.forEach(function (pt) { if (pt && pt.poly) growBoundsPoly(b, pt.poly); });
    ghosts.forEach(function (g) {
      var gp;
      try { gp = G.handParts(num(g.y, 0), num(g.rotate, 0), params) || []; } catch (e) { gp = []; }
      gp.forEach(function (pt) { if (pt.poly) growBoundsPoly(b, pt.poly); });
    });
    growBoundsPoint(b, 0, yRange[0]);
    growBoundsPoint(b, 0, yRange[1]);
    growBoundsPoint(b, -reach, y - reach);
    growBoundsPoint(b, reach, y + reach);
    growBoundsPoint(b, 0, 0);
    if (!isFinite(b.x0)) { b = { x0: -300, x1: 300, y0: -100, y1: 750 }; }

    var padL = 34, padR = 10, padT = 24, padB = 26;
    var availW = Math.max(20, p.w - padL - padR);
    var availH = Math.max(20, p.h - padT - padB);
    // 上面図は y をレール方向 (画面横) に置く 90deg 回転。鏡像にはしない
    var spanH = Math.max(1, b.y1 - b.y0);
    var spanV = Math.max(1, b.x1 - b.x0);
    var scale = opts.fit === false
      ? num(opts.scale, 0.5)
      : Math.min(availW / spanH, availH / spanV);
    if (!isFinite(scale) || scale <= 0) scale = 0.5;
    var cy = (b.y0 + b.y1) / 2;
    var cx = (b.x0 + b.x1) / 2;
    var ox = padL + availW / 2;
    var oy = padT + availH / 2;

    function tf(wx, wy) {
      return [ox + (wy - cy) * scale, oy + (wx - cx) * scale];
    }

    var viewY0 = cy - (availW / 2) / scale;
    var viewY1 = cy + (availW / 2) / scale;
    var viewX0 = cx - (availH / 2) / scale;
    var viewX1 = cx + (availH / 2) / scale;

    // ---- 格子 (100mm 刻み)
    ctx.save();
    ctx.beginPath();
    ctx.rect(padL, padT, availW, availH);
    ctx.clip();
    ctx.lineWidth = 1;
    dashed(ctx, []);
    // 格子はフィールドの範囲だけに引く (視野いっぱいに広げると場外まで目盛りが散る)
    var step = 100;
    var gY0 = Math.max(Math.floor(b.y0 / step) * step, Math.floor(viewY0 / step) * step);
    var gY1 = Math.min(Math.ceil(b.y1 / step) * step, Math.ceil(viewY1 / step) * step);
    var gX0 = Math.max(Math.floor(b.x0 / step) * step, Math.floor(viewX0 / step) * step);
    var gX1 = Math.min(Math.ceil(b.x1 / step) * step, Math.ceil(viewX1 / step) * step);
    for (var gy = gY0; gy <= gY1 + 1e-6; gy += step) {
      var s0 = tf(gX0, gy), s1 = tf(gX1, gy);
      ctx.strokeStyle = (gy === 0) ? colors.gridStrong : colors.grid;
      ctx.beginPath();
      ctx.moveTo(s0[0], s0[1]);
      ctx.lineTo(s1[0], s1[1]);
      ctx.stroke();
    }
    for (var gx = gX0; gx <= gX1 + 1e-6; gx += step) {
      var t0 = tf(gx, gY0), t1 = tf(gx, gY1);
      ctx.strokeStyle = (gx === 0) ? colors.gridStrong : colors.grid;
      ctx.beginPath();
      ctx.moveTo(t0[0], t0[1]);
      ctx.lineTo(t1[0], t1[1]);
      ctx.stroke();
    }

    // ---- 障害物 / ワーク
    var hitSet = {};
    highlight.forEach(function (n) { hitSet[n] = true; });

    obstacles.forEach(function (ob) {
      if (!ob || !ob.poly || ob.poly.length < 2) return;
      var hot = !!hitSet[ob.name];
      var work = ob.kind === 'work';
      polyPath(ctx, ob.poly, tf);
      if (work) {
        // ワークは当たって当然の相手なので塗らず輪郭だけ (障害物と混同させない)
        ctx.strokeStyle = hot ? colors.hit : colors.work;
        ctx.lineWidth = hot ? 2 : 1.2;
        dashed(ctx, [3, 2]);
        ctx.stroke();
        dashed(ctx, []);
      } else if (isWall(ob)) {
        ctx.fillStyle = hot ? colors.hit : colors.wall;
        ctx.globalAlpha = hot ? 0.75 : 0.55;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = hot ? colors.hit : colors.wall;
        ctx.lineWidth = 1.6;
        ctx.stroke();
      } else {
        ctx.fillStyle = hot ? colors.hit : colors.obstacle;
        ctx.fill();
        ctx.strokeStyle = hot ? colors.hit : colors.obstacleEdge;
        ctx.lineWidth = hot ? 2 : 1;
        ctx.stroke();
      }
      var lbl = ob.label || ob.name;
      if (lbl && isWall(ob)) lbl = lbl + ' ' + wallF.toFixed(0) + 'deg';
      if (lbl && opts.showLabels !== false) {
        var c0 = polyCenter(ob.poly);
        var sc = tf(c0[0], c0[1]);
        label(ctx, lbl, sc[0], sc[1], hot ? colors.hit : colors.muted, 'center', 'middle', FONT_SMALL);
      }
    });

    // ---- y レールと可動円
    var r0s = tf(0, yRange[0]), r1s = tf(0, yRange[1]);
    ctx.strokeStyle = colors.rail;
    ctx.lineWidth = 3;
    dashed(ctx, []);
    ctx.beginPath();
    ctx.moveTo(r0s[0], r0s[1]);
    ctx.lineTo(r1s[0], r1s[1]);
    ctx.stroke();

    var car = tf(0, y);
    ctx.strokeStyle = colors.grid;
    ctx.lineWidth = 1;
    dashed(ctx, [2, 3]);
    ctx.beginPath();
    ctx.arc(car[0], car[1], reach * scale, 0, Math.PI * 2);
    ctx.stroke();
    dashed(ctx, []);

    // ---- ghosts (選択中の矩形の隅など)
    ghosts.forEach(function (g) {
      var gp;
      try { gp = G.handParts(num(g.y, 0), num(g.rotate, 0), params) || []; } catch (e) { gp = []; }
      ctx.globalAlpha = 0.42;
      gp.forEach(function (pt) {
        if (!pt.poly) return;
        polyPath(ctx, pt.poly, tf);
        ctx.fillStyle = colors.ghost;
        ctx.fill();
        ctx.strokeStyle = colors.ghost;
        ctx.lineWidth = 1;
        dashed(ctx, [3, 2]);
        ctx.stroke();
        dashed(ctx, []);
      });
      ctx.globalAlpha = 1;
      if (g.label) {
        var gc = tf(0, num(g.y, 0));
        label(ctx, g.label, gc[0], gc[1] - 4, colors.ghost, 'center', 'bottom', FONT_SMALL);
      }
    });

    // ---- アームとハンド
    var handHot = highlight.length > 0;
    parts.forEach(function (pt) {
      if (!pt || !pt.poly) return;
      var base = (pt.name === 'hand') ? colors.hand : colors.arm;
      var col = handHot ? colors.hit : base;
      polyPath(ctx, pt.poly, tf);
      ctx.globalAlpha = 0.35;
      ctx.fillStyle = col;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = col;
      ctx.lineWidth = (pt.name === 'hand') ? 2 : 1.6;
      ctx.stroke();
    });

    // ---- キャリッジ (回転軸中心) と原点
    ctx.fillStyle = handHot ? colors.hit : colors.carriage;
    ctx.beginPath();
    ctx.arc(car[0], car[1], 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = colors.frame;
    ctx.lineWidth = 1;
    ctx.stroke();

    var org = tf(0, 0);
    ctx.strokeStyle = colors.gridStrong;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(org[0] - 6, org[1]);
    ctx.lineTo(org[0] + 6, org[1]);
    ctx.moveTo(org[0], org[1] - 6);
    ctx.lineTo(org[0], org[1] + 6);
    ctx.stroke();
    label(ctx, '(0,0)', org[0] + 7, org[1] - 3, colors.muted, 'left', 'bottom', FONT_SMALL);
    ctx.restore();

    // ---- 目盛りラベルと枠
    ctx.strokeStyle = colors.frame;
    ctx.lineWidth = 1;
    dashed(ctx, []);
    ctx.strokeRect(padL + 0.5, padT + 0.5, availW - 1, availH - 1);

    for (var ty = gY0; ty <= gY1 + 1e-6; ty += step) {
      var tp = tf(0, ty);
      if (tp[0] < padL - 2 || tp[0] > p.w - padR + 2) continue;
      label(ctx, String(ty), tp[0], p.h - padB + 11, colors.muted, 'center', 'alphabetic', FONT_SMALL);
    }
    for (var tx = gX0; tx <= gX1 + 1e-6; tx += step) {
      var xp = tf(tx, 0);
      // 見出しの行や枠外に掛かる目盛りは出さない
      if (xp[1] < padT + 6 || xp[1] > p.h - padB - 2) continue;
      label(ctx, String(tx), padL - 4, xp[1], colors.muted, 'right', 'middle', FONT_SMALL);
    }

    label(ctx, 'y_axis [mm] →', p.w - padR, p.h - 3, colors.fg, 'right', 'alphabetic', FONT);

    // 狭い canvas では見出しが衝突するので、入る長さまで落とす
    var axisTitle = 'x [mm] ↓ (+棚 / −コンベア)';
    var readout = 'y=' + y.toFixed(1) + 'mm  rotate=' + rotate.toFixed(1) + 'deg  wall_f=' + wallF.toFixed(0) + 'deg';
    ctx.font = FONT;
    if (textW(ctx, axisTitle) + textW(ctx, readout) + 16 > p.w) {
      axisTitle = 'x [mm] ↓';
    }
    if (textW(ctx, axisTitle) + textW(ctx, readout) + 16 > p.w) {
      readout = 'y=' + y.toFixed(0) + ' r=' + rotate.toFixed(0) + ' w=' + wallF.toFixed(0);
    }
    label(ctx, axisTitle, 2, padT - 12, colors.fg, 'left', 'alphabetic', FONT);
    label(ctx, readout, p.w - padR, padT - 12, handHot ? colors.hit : colors.fg, 'right', 'alphabetic', FONT);

    var scaleTxt = '×' + scale.toFixed(2) + 'px/mm';
    label(ctx, scaleTxt, padL, p.h - 3, colors.muted, 'left', 'alphabetic', FONT_SMALL);
    if (highlight.length) {
      ctx.font = FONT_SMALL;
      var offX = textW(ctx, scaleTxt) + 10;
      var names = highlight.length > 3 ? highlight.slice(0, 3).concat(['…']) : highlight;
      label(ctx, '干渉: ' + names.join(', '), padL + offX, p.h - 3, colors.hit, 'left', 'alphabetic', FONT);
    }
  }

  function polyCenter(poly) {
    var sx = 0, sy = 0;
    for (var i = 0; i < poly.length; i++) { sx += poly[i][0]; sy += poly[i][1]; }
    return [sx / poly.length, sy / poly.length];
  }

  // ------------------------------------------------------ C 空間マップ

  function cellIndex(cs, iY, iR) {
    if (cs.order === 'y-major') return iY * cs.nR + iR;
    return iR * cs.nY + iY;
  }

  function idxOf(v, min, max, n) {
    if (n <= 1) return 0;
    return clamp(Math.round((v - min) / (max - min) * (n - 1)), 0, n - 1);
  }

  function cspaceRanges(cs) {
    return {
      yMin: num(cs.yMin, 0),
      yMax: num(cs.yMax, 650),
      rMin: num(cs.rMin, 0),
      rMax: num(cs.rMax, 185)
    };
  }

  function legendEntries(cs, colors) {
    var names = (cs && cs.obstacleNames) || [];
    var out = [];
    for (var i = 0; i < names.length; i++) {
      var st = ownerStyle(colors, i + 1);
      out.push({ text: names[i], color: st.color, pid: st.pid });
    }
    return out;
  }

  function cspaceLayout(w, h, opts) {
    var cs = opts && opts.cspace;
    var entries = cs ? ((cs.obstacleNames || []).length) : 0;
    var cols = Math.max(1, Math.floor(w / 150));
    var rows = Math.ceil((entries + 3) / cols);
    var legendH = rows * 13 + 6;
    var padL = 46, padR = 12, padT = 20;
    var axisH = 30;
    var plotH = Math.max(30, h - padT - axisH - legendH);
    return {
      plot: { x: padL, y: padT, w: Math.max(30, w - padL - padR), h: plotH },
      legend: { x: padL, y: padT + plotH + axisH, w: Math.max(30, w - padL - padR), cols: cols, rowH: 13 },
      padL: padL, padR: padR, padT: padT, axisH: axisH
    };
  }

  function rectBounds(rect) {
    if (!rect) return null;
    var y0, y1, r0, r1, sy, sr, ey, er;
    if (typeof rect.y0 === 'number') {
      y0 = rect.y0; y1 = num(rect.y1, rect.y0);
      r0 = num(rect.r0, 0); r1 = num(rect.r1, r0);
    } else if (rect.from && rect.to) {
      y0 = num(rect.from.y, 0); y1 = num(rect.to.y, y0);
      r0 = num(rect.from.rotate, 0); r1 = num(rect.to.rotate, r0);
    } else if (rect.start && rect.end) {
      y0 = num(rect.start.y, 0); y1 = num(rect.end.y, y0);
      r0 = num(rect.start.rotate, 0); r1 = num(rect.end.rotate, r0);
    } else {
      return null;
    }
    var src = rect.from || rect.start || null;
    var dst = rect.to || rect.end || null;
    sy = src ? num(src.y, y0) : y0;
    sr = src ? num(src.rotate, r0) : r0;
    ey = dst ? num(dst.y, y1) : y1;
    er = dst ? num(dst.rotate, r1) : r1;
    return {
      y0: Math.min(y0, y1), y1: Math.max(y0, y1),
      r0: Math.min(r0, r1), r1: Math.max(r0, r1),
      sy: sy, sr: sr, ey: ey, er: er,
      degenY: Math.abs(y1 - y0) < 1e-9,
      degenR: Math.abs(r1 - r0) < 1e-9,
      label: rect.label || rect.name || rect.stepLabel || rect.moveLabel || rect.title || '',
      tag: (typeof rect.index === 'number') ? String(rect.index) : null,
      // 未解決の手順に安全/干渉の判定を出すと嘘になる
      unresolved: rect.unresolved === true,
      disabled: rect.enabled === false
    };
  }

  // sweepHit を呼ばずに cells を走査する (同じ答えで速い)
  function rectHit(cs, rb) {
    if (!cs || !cs.cells) return { hit: false, owner: 0 };
    var rg = cspaceRanges(cs);
    var a = idxOf(rb.y0, rg.yMin, rg.yMax, cs.nY);
    var b = idxOf(rb.y1, rg.yMin, rg.yMax, cs.nY);
    var c = idxOf(rb.r0, rg.rMin, rg.rMax, cs.nR);
    var d = idxOf(rb.r1, rg.rMin, rg.rMax, cs.nR);
    var owner = 0, hit = false;
    for (var iR = c; iR <= d; iR++) {
      for (var iY = a; iY <= b; iY++) {
        var k = cellIndex(cs, iY, iR);
        if (cs.cells[k]) {
          hit = true;
          if (!owner) owner = cs.owner ? cs.owner[k] : 1;
        }
      }
    }
    return { hit: hit, owner: owner };
  }

  function renderCells(cs, colors) {
    if (!DOC) return null;
    var off = DOC.createElement('canvas');
    off.width = Math.max(1, cs.nY);
    off.height = Math.max(1, cs.nR);
    var g = off.getContext('2d');
    var img = g.createImageData(off.width, off.height);
    var data = img.data;
    var rgbCache = {};
    for (var py = 0; py < off.height; py++) {
      var iR = off.height - 1 - py; // ImageData は上が rotate 最大
      for (var px = 0; px < off.width; px++) {
        var k = cellIndex(cs, px, iR);
        if (!cs.cells[k]) continue;
        var owner = cs.owner ? (cs.owner[k] || 1) : 1;
        var st = rgbCache[owner];
        if (!st) {
          var s = ownerStyle(colors, owner);
          st = rgbCache[owner] = { rgb: toRgb(s.color), pid: s.pid };
        }
        var o = (py * off.width + px) * 4;
        data[o] = st.rgb[0];
        data[o + 1] = st.rgb[1];
        data[o + 2] = st.rgb[2];
        data[o + 3] = Math.round(225 * patternMask(st.pid, px, py));
      }
    }
    g.putImageData(img, 0, 0);
    return off;
  }

  var FILLED_MARKS = { disc: 1, tri: 1, diamond: 1 };

  function markerPath(ctx, kind, x, y, size) {
    ctx.beginPath();
    if (kind === 'circle' || kind === 'disc') {
      ctx.arc(x, y, size, 0, Math.PI * 2);
    } else if (kind === 'square') {
      ctx.rect(x - size, y - size, size * 2, size * 2);
    } else if (kind === 'cross') {
      ctx.moveTo(x - size, y - size); ctx.lineTo(x + size, y + size);
      ctx.moveTo(x + size, y - size); ctx.lineTo(x - size, y + size);
    } else if (kind === 'plus') {
      ctx.moveTo(x - size, y); ctx.lineTo(x + size, y);
      ctx.moveTo(x, y - size); ctx.lineTo(x, y + size);
    } else if (kind === 'tri') {
      ctx.moveTo(x, y - size);
      ctx.lineTo(x + size, y + size);
      ctx.lineTo(x - size, y + size);
      ctx.closePath();
    } else if (kind === 'diamond') {
      ctx.moveTo(x, y - size);
      ctx.lineTo(x + size, y);
      ctx.lineTo(x, y + size);
      ctx.lineTo(x - size, y);
      ctx.closePath();
    }
  }

  function marker(ctx, kind, x, y, size, color, lw, halo) {
    dashed(ctx, []);
    if (halo) {
      markerPath(ctx, kind, x, y, size);
      ctx.strokeStyle = halo;
      ctx.lineWidth = (lw || 1.5) + 2;
      ctx.stroke();
    }
    markerPath(ctx, kind, x, y, size);
    ctx.lineWidth = lw || 1.5;
    if (FILLED_MARKS[kind]) {
      ctx.fillStyle = color;
      ctx.fill();
    } else {
      ctx.strokeStyle = color;
      ctx.stroke();
    }
  }

  function arrow(ctx, x0, y0, x1, y1, color, lw) {
    var dx = x1 - x0, dy = y1 - y0;
    var len = Math.sqrt(dx * dx + dy * dy);
    ctx.strokeStyle = color;
    ctx.lineWidth = lw || 1.5;
    dashed(ctx, []);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
    if (len < 6) return;
    var ux = dx / len, uy = dy / len;
    var a = 6;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x1 - a * ux + a * 0.5 * -uy, y1 - a * uy + a * 0.5 * ux);
    ctx.lineTo(x1 - a * ux - a * 0.5 * -uy, y1 - a * uy - a * 0.5 * ux);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  function cspace(canvas, opts) {
    opts = opts || {};
    if (!canvas) return;
    var cs = opts.cspace;
    if (!cs || !cs.cells || !cs.nY || !cs.nR) {
      notice(canvas, 'C 空間マップ未計算');
      return;
    }
    var p = prepare(canvas);
    var ctx = p.ctx;
    var colors = resolveColors(canvas, opts);
    var L = cspaceLayout(p.w, p.h, opts);
    var plot = L.plot;
    var rg = cspaceRanges(cs);
    var showLabels = opts.showLabels !== false;

    function sx(yv) { return plot.x + (yv - rg.yMin) / (rg.yMax - rg.yMin) * plot.w; }
    function sy(rv) { return plot.y + plot.h - (rv - rg.rMin) / (rg.rMax - rg.rMin) * plot.h; }

    // ---- 背景と干渉領域
    ctx.fillStyle = colors.plotBg;
    ctx.fillRect(plot.x, plot.y, plot.w, plot.h);

    var off = renderCells(cs, colors);
    if (off) {
      var smooth = ctx.imageSmoothingEnabled;
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(off, 0, 0, off.width, off.height, plot.x, plot.y, plot.w, plot.h);
      ctx.imageSmoothingEnabled = smooth;
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(plot.x, plot.y, plot.w, plot.h);
    ctx.clip();

    // ---- 位置定数の格子線 (主要なものだけ)
    var positions = opts.positions || {};
    drawPositionLines(ctx, positions.y_axis, KEY_Y_POSITIONS, rg.yMin, rg.yMax, colors, showLabels,
      function (v) { return sx(v); }, true, plot);
    drawPositionLines(ctx, positions.rotate, KEY_R_POSITIONS, rg.rMin, rg.rMax, colors, showLabels,
      function (v) { return sy(v); }, false, plot);

    // ---- 矩形領域
    var rects = opts.rects || [];
    var selected = (typeof opts.selected === 'number') ? opts.selected : null;
    for (var i = 0; i < rects.length; i++) {
      var rb = rectBounds(rects[i]);
      if (!rb) continue;
      var res = rectHit(cs, rb);
      var isSel = (selected === i);
      drawRect(ctx, rb, res, isSel, colors, sx, sy, showLabels, i, plot);
    }

    // ---- 校正点 (実機と突き合わせる 3 点)
    for (var ci = 0; ci < CALIB_POINTS.length; ci++) {
      var cp = CALIB_POINTS[ci];
      var px = sx(cp.y), py = sy(cp.rotate);
      marker(ctx, 'tri', px, py, 4.5, colors.calib, 1.2);
      marker(ctx, 'circle', px, py, 7, colors.calib, 1.2);
      if (showLabels) {
        label(ctx, cp.label, px + 9, py - 6, colors.calib, 'left', 'middle', FONT_SMALL);
      }
    }

    // ---- 現在姿勢
    if (opts.cursor) {
      var ux = sx(num(opts.cursor.y, 0)), uy = sy(num(opts.cursor.rotate, 0));
      ctx.strokeStyle = colors.cursor;
      ctx.lineWidth = 1;
      dashed(ctx, [4, 3]);
      ctx.beginPath();
      ctx.moveTo(plot.x, uy); ctx.lineTo(plot.x + plot.w, uy);
      ctx.moveTo(ux, plot.y); ctx.lineTo(ux, plot.y + plot.h);
      ctx.stroke();
      dashed(ctx, []);
      marker(ctx, 'plus', ux, uy, 6, colors.cursor, 2);
      marker(ctx, 'circle', ux, uy, 4, colors.cursor, 2);
    }
    ctx.restore();

    // ---- 枠と目盛り
    ctx.strokeStyle = colors.frame;
    ctx.lineWidth = 1;
    dashed(ctx, []);
    ctx.strokeRect(plot.x + 0.5, plot.y + 0.5, plot.w - 1, plot.h - 1);

    var yStep = 50, yMajor = 100;
    for (var yv = Math.ceil(rg.yMin / yStep) * yStep; yv <= rg.yMax + 1e-6; yv += yStep) {
      var xx = sx(yv);
      var major = Math.abs(yv % yMajor) < 1e-6;
      ctx.strokeStyle = colors.frame;
      ctx.beginPath();
      ctx.moveTo(xx, plot.y + plot.h);
      ctx.lineTo(xx, plot.y + plot.h + (major ? 5 : 3));
      ctx.stroke();
      if (major) {
        label(ctx, String(Math.round(yv)), xx, plot.y + plot.h + 15, colors.muted, 'center', 'alphabetic', FONT_SMALL);
      }
    }
    for (var rv = Math.ceil(rg.rMin / 15) * 15; rv <= rg.rMax + 1e-6; rv += 15) {
      var yy = sy(rv);
      var rmaj = Math.abs(rv % 45) < 1e-6;
      ctx.strokeStyle = colors.frame;
      ctx.beginPath();
      ctx.moveTo(plot.x - (rmaj ? 5 : 3), yy);
      ctx.lineTo(plot.x, yy);
      ctx.stroke();
      if (rmaj) {
        label(ctx, String(Math.round(rv)), plot.x - 7, yy, colors.muted, 'right', 'middle', FONT_SMALL);
      }
    }
    // 最大角は目盛りの倍数から十分離れているときだけ出す (185 と 180 が重なる)
    var last45 = Math.floor(rg.rMax / 45) * 45;
    if ((rg.rMax - last45) / (rg.rMax - rg.rMin) * plot.h > 12) {
      label(ctx, String(Math.round(rg.rMax)), plot.x - 7, plot.y + 4, colors.muted, 'right', 'middle', FONT_SMALL);
    }

    label(ctx, 'y_axis [mm] →', plot.x + plot.w, plot.y + plot.h + L.axisH - 2, colors.fg, 'right', 'alphabetic', FONT);
    label(ctx, 'rotate [deg] ↑', 2, plot.y - 8, colors.fg, 'left', 'alphabetic', FONT);
    label(ctx, 'C 空間 (' + cs.nY + '×' + cs.nR + ')', plot.x + plot.w, plot.y - 8, colors.muted, 'right', 'alphabetic', FONT_SMALL);

    drawLegend(ctx, L.legend, cs, colors);
  }

  function drawPositionLines(ctx, table, keys, lo, hi, colors, showLabels, project, vertical, plot) {
    if (!table) return;
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var v = table[k];
      if (v && typeof v === 'object' && typeof v.value === 'number') v = v.value;
      if (typeof v !== 'number' || !isFinite(v)) continue;
      if (v < lo - 1e-6 || v > hi + 1e-6) continue;
      var s = project(v);
      ctx.strokeStyle = colors.gridStrong;
      ctx.lineWidth = 1;
      dashed(ctx, [3, 3]);
      ctx.beginPath();
      if (vertical) {
        ctx.moveTo(s, plot.y);
        ctx.lineTo(s, plot.y + plot.h);
      } else {
        ctx.moveTo(plot.x, s);
        ctx.lineTo(plot.x + plot.w, s);
      }
      ctx.stroke();
      dashed(ctx, []);
      if (!showLabels) continue;
      if (vertical) {
        // 近接する位置名 (home/clear/work_1) が重ならないよう段をずらす
        ctx.save();
        ctx.translate(s + 3, plot.y + 3 + (i % 3) * 13);
        ctx.rotate(Math.PI / 2);
        label(ctx, k, 0, 0, colors.fg, 'left', 'alphabetic', FONT_SMALL);
        ctx.restore();
      } else {
        var ly = clamp(s - 2, plot.y + 10, plot.y + plot.h - 2);
        label(ctx, k, plot.x + plot.w - 3, ly, colors.fg, 'right', 'alphabetic', FONT_SMALL);
      }
    }
  }

  function drawRect(ctx, rb, res, isSel, colors, sx, sy, showLabels, index, plot) {
    var x0 = sx(rb.y0), x1 = sx(rb.y1);
    var yTop = sy(rb.r1), yBot = sy(rb.r0);
    var w = x1 - x0, h = yBot - yTop;
    var col = rb.unresolved ? colors.muted : (res.hit ? colors.hit : colors.safe);
    var halo = colors.halo;
    var lw = isSel ? 3 : 1.6;
    var markHit = !rb.unresolved && res.hit;
    ctx.save();
    if (rb.disabled) ctx.globalAlpha = 0.4;

    ctx.strokeStyle = col;
    ctx.fillStyle = col;
    ctx.lineWidth = lw;

    if (rb.degenY && rb.degenR) {
      // 点 (どちらも動かない) — 面と混同しないよう菱形で描く
      marker(ctx, 'diamond', x0, yBot, isSel ? 6 : 4, col, lw, halo);
    } else if (rb.degenY || rb.degenR) {
      // 線分 (片軸のみ動く)
      ctx.beginPath();
      if (rb.degenY) { ctx.moveTo(x0, yBot); ctx.lineTo(x0, yTop); }
      else { ctx.moveTo(x0, yBot); ctx.lineTo(x1, yBot); }
      dashed(ctx, []);
      ctx.strokeStyle = halo;
      ctx.lineWidth = lw + 4;
      ctx.stroke();
      dashed(ctx, markHit ? [5, 3] : []);
      ctx.strokeStyle = col;
      ctx.lineWidth = lw + 1.5;
      ctx.stroke();
      dashed(ctx, []);
      var mx = (x0 + x1) / 2, my = (yTop + yBot) / 2;
      marker(ctx, rb.unresolved ? 'plus' : (markHit ? 'cross' : 'disc'), mx, my, 3.5, col, 1.5, halo);
    } else {
      // 面 (両軸が動く) — 何枚も重なるので塗るのは選択中の 1 枚だけ
      if (isSel) {
        ctx.globalAlpha *= markHit ? 0.18 : 0.12;
        ctx.fillRect(x0, yTop, w, h);
        ctx.globalAlpha = rb.disabled ? 0.4 : 1;
        dashed(ctx, []);
        ctx.strokeStyle = halo;
        ctx.lineWidth = lw + 2.5;
        ctx.strokeRect(x0, yTop, w, h);
      }
      dashed(ctx, markHit ? [5, 3] : []);
      ctx.strokeStyle = col;
      ctx.lineWidth = isSel ? lw : 1.4;
      ctx.strokeRect(x0, yTop, w, h);
      dashed(ctx, []);
      marker(ctx, rb.unresolved ? 'plus' : (markHit ? 'cross' : 'circle'), x0 + w / 2, yTop + h / 2, markHit ? 5 : 4, col, 1.8, halo);
    }

    if (isSel) {
      arrow(ctx, sx(rb.sy), sy(rb.sr), sx(rb.ey), sy(rb.er), halo, 3.8);
      arrow(ctx, sx(rb.sy), sy(rb.sr), sx(rb.ey), sy(rb.er), col, 2);
      marker(ctx, 'disc', sx(rb.sy), sy(rb.sr), 4, col, 1.5, halo);
      marker(ctx, 'square', sx(rb.ey), sy(rb.er), 5, col, 2.5, halo);
      if (rb.label) {
        labelOn(ctx, rb.label, Math.min(x0, x1) + 4, clamp(Math.min(yTop, yBot) - 5, plot.y + 12, plot.y + plot.h - 3),
          col, 'left', 'alphabetic', FONT_BOLD, halo);
      }
    } else if (showLabels) {
      label(ctx, rb.tag || String(index), Math.min(x0, x1) + 3, clamp(Math.min(yTop, yBot) - 2, plot.y + 10, plot.y + plot.h - 2),
        col, 'left', 'alphabetic', FONT_SMALL);
    }
    ctx.restore();
  }

  function drawLegend(ctx, lg, cs, colors) {
    var entries = legendEntries(cs, colors);
    var extra = [
      { text: '安全な動作 (○)', color: colors.safe, kind: 'safe' },
      { text: '干渉する動作 (×)', color: colors.hit, kind: 'hit' },
      { text: '校正点 (▲)', color: colors.calib, kind: 'calib' }
    ];
    var all = entries.concat(extra);
    var colW = lg.w / lg.cols;
    for (var i = 0; i < all.length; i++) {
      var e = all[i];
      var col = i % lg.cols;
      var row = Math.floor(i / lg.cols);
      var x = lg.x + col * colW;
      var y = lg.y + row * lg.rowH;
      if (e.kind === 'calib') {
        marker(ctx, 'tri', x + 6, y + 5, 4, e.color, 1.2);
      } else if (e.kind === 'safe' || e.kind === 'hit') {
        ctx.strokeStyle = e.color;
        ctx.lineWidth = 1.6;
        dashed(ctx, e.kind === 'hit' ? [4, 2] : []);
        ctx.strokeRect(x + 1.5, y + 1.5, 11, 7);
        dashed(ctx, []);
        marker(ctx, e.kind === 'hit' ? 'cross' : 'circle', x + 7, y + 5, 2.5, e.color, 1.2);
      } else {
        ctx.save();
        ctx.translate(x + 1, y + 1);
        ctx.fillStyle = maskPattern(ctx, e.color, e.pid);
        ctx.fillRect(0, 0, 11, 8);
        ctx.restore();
        ctx.strokeStyle = colors.frame;
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 1.5, y + 1.5, 10, 7);
      }
      label(ctx, e.text, x + 16, y + 8, colors.muted, 'left', 'alphabetic', FONT_SMALL);
    }
  }

  function hitTestCspace(canvas, opts, clientX, clientY) {
    if (!canvas || !opts || !opts.cspace) return null;
    var rect;
    try { rect = canvas.getBoundingClientRect(); } catch (e) { return null; }
    var size = cssSize(canvas);
    var L = cspaceLayout(size.w, size.h, opts);
    var plot = L.plot;
    var px = clientX - rect.left;
    var py = clientY - rect.top;
    if (px < plot.x || px > plot.x + plot.w || py < plot.y || py > plot.y + plot.h) return null;
    var rg = cspaceRanges(opts.cspace);
    var y = rg.yMin + (px - plot.x) / plot.w * (rg.yMax - rg.yMin);
    var r = rg.rMin + (plot.y + plot.h - py) / plot.h * (rg.rMax - rg.rMin);
    return {
      y: clamp(y, rg.yMin, rg.yMax),
      rotate: clamp(r, rg.rMin, rg.rMax)
    };
  }

  global.Render = {
    topView: topView,
    cspace: cspace,
    hitTestCspace: hitTestCspace,
    COLOR_TOKENS: TOKENS,
    OWNER_TOKEN_PREFIX: '--sim-owner-',
    DEFAULT_COLORS: DEFAULTS,
    CSPACE_CELL_ORDER: 'rotate-major (cspace.order === "y-major" ならそちらに従う)',
    CALIB_POINTS: CALIB_POINTS,
    KEY_POSITIONS: { y_axis: KEY_Y_POSITIONS, rotate: KEY_R_POSITIONS }
  };
})(typeof window !== 'undefined' ? window : this);
