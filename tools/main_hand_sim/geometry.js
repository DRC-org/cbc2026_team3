// メインハンド干渉シミュレータの幾何計算コア。DOM に触らない純ロジック。
// 座標系: 上面図 mm / deg。y = y_axis の論理値そのまま、x は正がワーク棚側・負がコンベア側。
// 回転中心は常に (0, y)、腕の向きは theta_world = rotateZeroDir + rotateSign * rotate。
(function (root) {
  'use strict';

  var DEG = Math.PI / 180;

  var DEFAULT_PARAMS = {
    armRadius: 64,
    armWidth: 40,
    handWidth: 90,
    handDepth: 70,
    rotateZeroDir: 180,
    // rotate 0→185 の掃引を +y 側（フィールド側）に取る向き。+1 だとレール端の外側を掃く
    rotateSign: -1,

    shelfX0: 120,
    shelfX1: 420,
    shelfY0: -50,
    shelfY1: 700,

    conveyorX0: -320,
    conveyorX1: -120,
    conveyorY0: -50,
    conveyorY1: 700,

    // 校正の 3 件を通すために決めた値。実機の実測ではない
    postOriginX0: -250,
    postOriginX1: -90,
    postOriginY0: -200,
    postOriginY1: -34,

    postFarX0: -250,
    postFarX1: 250,
    postFarY0: 660,
    postFarY1: 820,

    wallFPivotX: -120,
    wallFPivotY: 60,
    wallFLength: 180,
    wallFThickness: 15,
    wallFZeroDir: 0,
    wallFSign: 1,

    workX: 150,
    workRadius: 30
  };

  var PARAM_META = {
    armRadius: { label: 'アーム長（回転中心→EE 水平距離）', unit: 'mm', group: 'ハンド' },
    armWidth: { label: 'アームの幅', unit: 'mm', group: 'ハンド' },
    handWidth: { label: 'ハンドの幅（腕に垂直）', unit: 'mm', group: 'ハンド' },
    handDepth: { label: 'ハンドの奥行き（腕に平行）', unit: 'mm', group: 'ハンド' },
    rotateZeroDir: { label: 'rotate=0 のときの腕の向き', unit: 'deg', group: 'ハンド' },
    rotateSign: { label: 'rotate の回転向き（+1 反時計 / -1 時計）', unit: '', group: 'ハンド' },

    shelfX0: { label: 'ワーク棚 x 手前', unit: 'mm', group: 'ワーク棚' },
    shelfX1: { label: 'ワーク棚 x 奥', unit: 'mm', group: 'ワーク棚' },
    shelfY0: { label: 'ワーク棚 y 原点側', unit: 'mm', group: 'ワーク棚' },
    shelfY1: { label: 'ワーク棚 y 遠端側', unit: 'mm', group: 'ワーク棚' },

    conveyorX0: { label: 'コンベア x 奥', unit: 'mm', group: 'コンベア' },
    conveyorX1: { label: 'コンベア x 手前', unit: 'mm', group: 'コンベア' },
    conveyorY0: { label: 'コンベア y 原点側', unit: 'mm', group: 'コンベア' },
    conveyorY1: { label: 'コンベア y 遠端側', unit: 'mm', group: 'コンベア' },

    postOriginX0: { label: '原点端の障害物 x 奥', unit: 'mm', group: '支柱・端部' },
    postOriginX1: { label: '原点端の障害物 x 手前', unit: 'mm', group: '支柱・端部' },
    postOriginY0: { label: '原点端の障害物 y 外側', unit: 'mm', group: '支柱・端部' },
    postOriginY1: { label: '原点端の障害物 y 内側（上端）', unit: 'mm', group: '支柱・端部' },
    postFarX0: { label: '遠端の障害物 x 奥', unit: 'mm', group: '支柱・端部' },
    postFarX1: { label: '遠端の障害物 x 手前', unit: 'mm', group: '支柱・端部' },
    postFarY0: { label: '遠端の障害物 y 内側（下端）', unit: 'mm', group: '支柱・端部' },
    postFarY1: { label: '遠端の障害物 y 外側', unit: 'mm', group: '支柱・端部' },

    wallFPivotX: { label: 'wall_f 回転軸 x', unit: 'mm', group: 'wall_f' },
    wallFPivotY: { label: 'wall_f 回転軸 y', unit: 'mm', group: 'wall_f' },
    wallFLength: { label: 'wall_f 板の長さ', unit: 'mm', group: 'wall_f' },
    wallFThickness: { label: 'wall_f 板の厚み', unit: 'mm', group: 'wall_f' },
    wallFZeroDir: { label: 'wall_f=0 のときの板の向き', unit: 'deg', group: 'wall_f' },
    wallFSign: { label: 'wall_f の回転向き（+1 反時計 / -1 時計）', unit: '', group: 'wall_f' },

    workX: { label: 'ワーク中心の x', unit: 'mm', group: 'ワーク' },
    workRadius: { label: 'ワークの半径', unit: 'mm', group: 'ワーク' }
  };

  // ワークが並ぶ y（config/main_hand_positions.yaml の y_axis work_1/2/3/shared）
  var WORK_COLUMNS = [
    { name: 'work_1', label: '1 列目ワーク', y: 75 },
    { name: 'work_2', label: '2 列目ワーク', y: 270 },
    { name: 'work_3', label: '3 列目ワーク', y: 466 },
    { name: 'work_shared', label: '共通ワーク', y: 550 }
  ];

  // HOME の wall_f（initial）。opts.wallF が来なければこれを使う
  var DEFAULT_WALL_F = 270;

  var Y_MIN = 0;
  var Y_MAX = 650;
  var R_MIN = 0;
  var R_MAX = 185;

  function num(v, fallback) {
    return typeof v === 'number' && isFinite(v) ? v : fallback;
  }

  function withDefaults(p) {
    var out = {};
    var k;
    for (k in DEFAULT_PARAMS) {
      if (Object.prototype.hasOwnProperty.call(DEFAULT_PARAMS, k)) {
        out[k] = p && typeof p[k] === 'number' && isFinite(p[k]) ? p[k] : DEFAULT_PARAMS[k];
      }
    }
    return out;
  }

  function rectPoly(x0, y0, x1, y1) {
    var xa = Math.min(x0, x1);
    var xb = Math.max(x0, x1);
    var ya = Math.min(y0, y1);
    var yb = Math.max(y0, y1);
    return [[xa, ya], [xb, ya], [xb, yb], [xa, yb]];
  }

  function aabb(poly) {
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    for (var i = 0; i < poly.length; i++) {
      var x = poly[i][0];
      var y = poly[i][1];
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    return [minX, minY, maxX, maxY];
  }

  function boxOverlap(a, b) {
    return !(a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1]);
  }

  function cross(ox, oy, ax, ay, bx, by) {
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox);
  }

  function onSegment(px, py, qx, qy, rx, ry) {
    return (
      Math.min(px, qx) - 1e-9 <= rx &&
      rx <= Math.max(px, qx) + 1e-9 &&
      Math.min(py, qy) - 1e-9 <= ry &&
      ry <= Math.max(py, qy) + 1e-9
    );
  }

  function segIntersects(a1, a2, b1, b2) {
    var d1 = cross(b1[0], b1[1], b2[0], b2[1], a1[0], a1[1]);
    var d2 = cross(b1[0], b1[1], b2[0], b2[1], a2[0], a2[1]);
    var d3 = cross(a1[0], a1[1], a2[0], a2[1], b1[0], b1[1]);
    var d4 = cross(a1[0], a1[1], a2[0], a2[1], b2[0], b2[1]);
    if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) {
      return true;
    }
    if (Math.abs(d1) < 1e-9 && onSegment(b1[0], b1[1], b2[0], b2[1], a1[0], a1[1])) return true;
    if (Math.abs(d2) < 1e-9 && onSegment(b1[0], b1[1], b2[0], b2[1], a2[0], a2[1])) return true;
    if (Math.abs(d3) < 1e-9 && onSegment(a1[0], a1[1], a2[0], a2[1], b1[0], b1[1])) return true;
    if (Math.abs(d4) < 1e-9 && onSegment(a1[0], a1[1], a2[0], a2[1], b2[0], b2[1])) return true;
    return false;
  }

  function pointInPoly(pt, poly) {
    var inside = false;
    var x = pt[0];
    var y = pt[1];
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0];
      var yi = poly[i][1];
      var xj = poly[j][0];
      var yj = poly[j][1];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // 凸でない多角形も扱う: 辺の交差 + 片方の頂点の内包
  function polyIntersects(a, b) {
    if (!a || !b || a.length < 3 || b.length < 3) return false;
    if (!boxOverlap(aabb(a), aabb(b))) return false;
    for (var i = 0; i < a.length; i++) {
      var a1 = a[i];
      var a2 = a[(i + 1) % a.length];
      for (var j = 0; j < b.length; j++) {
        if (segIntersects(a1, a2, b[j], b[(j + 1) % b.length])) return true;
      }
    }
    return pointInPoly(a[0], b) || pointInPoly(b[0], a);
  }

  function handParts(y, rotate, params) {
    var p = withDefaults(params);
    var theta = (p.rotateZeroDir + p.rotateSign * rotate) * DEG;
    var ux = Math.cos(theta);
    var uy = Math.sin(theta);
    var vx = -uy;
    var vy = ux;
    var px = 0;
    var py = y;
    var aw = p.armWidth / 2;
    var ex = px + p.armRadius * ux;
    var ey = py + p.armRadius * uy;
    var hd = p.handDepth / 2;
    var hw = p.handWidth / 2;

    var arm = [
      [px + aw * vx, py + aw * vy],
      [ex + aw * vx, ey + aw * vy],
      [ex - aw * vx, ey - aw * vy],
      [px - aw * vx, py - aw * vy]
    ];
    var hand = [
      [ex + hd * ux + hw * vx, ey + hd * uy + hw * vy],
      [ex + hd * ux - hw * vx, ey + hd * uy - hw * vy],
      [ex - hd * ux - hw * vx, ey - hd * uy - hw * vy],
      [ex - hd * ux + hw * vx, ey - hd * uy + hw * vy]
    ];
    return [
      { name: 'arm', label: 'アーム', poly: arm },
      { name: 'hand', label: 'ハンド', poly: hand }
    ];
  }

  function circlePoly(cx, cy, r, n) {
    var poly = [];
    var seg = n || 16;
    for (var i = 0; i < seg; i++) {
      var t = (i / seg) * Math.PI * 2;
      poly.push([cx + r * Math.cos(t), cy + r * Math.sin(t)]);
    }
    return poly;
  }

  function wallFAngle(params, opts) {
    if (opts && typeof opts.wallF === 'number' && isFinite(opts.wallF)) return opts.wallF;
    if (params && typeof params.wallFDefault === 'number') return params.wallFDefault;
    return DEFAULT_WALL_F;
  }

  function obstacles(params, opts) {
    var p = withDefaults(params);
    var list = [
      {
        name: 'shelf',
        label: 'ワーク棚',
        kind: 'obstacle',
        poly: rectPoly(p.shelfX0, p.shelfY0, p.shelfX1, p.shelfY1)
      },
      {
        name: 'conveyor',
        label: 'コンベア',
        kind: 'obstacle',
        poly: rectPoly(p.conveyorX0, p.conveyorY0, p.conveyorX1, p.conveyorY1)
      },
      {
        name: 'postOrigin',
        label: '原点端の障害物',
        kind: 'obstacle',
        poly: rectPoly(p.postOriginX0, p.postOriginY0, p.postOriginX1, p.postOriginY1)
      },
      {
        name: 'postFar',
        label: '遠端の障害物',
        kind: 'obstacle',
        poly: rectPoly(p.postFarX0, p.postFarY0, p.postFarX1, p.postFarY1)
      }
    ];

    var wf = (p.wallFZeroDir + p.wallFSign * wallFAngle(params, opts)) * DEG;
    var wux = Math.cos(wf);
    var wuy = Math.sin(wf);
    var wt = p.wallFThickness / 2;
    var bx = p.wallFPivotX;
    var by = p.wallFPivotY;
    var tx = bx + p.wallFLength * wux;
    var ty = by + p.wallFLength * wuy;
    list.push({
      name: 'wallF',
      label: 'コンベアの壁 (wall_f)',
      kind: 'obstacle',
      poly: [
        [bx - wt * wuy, by + wt * wux],
        [tx - wt * wuy, ty + wt * wux],
        [tx + wt * wuy, ty - wt * wux],
        [bx + wt * wuy, by - wt * wux]
      ]
    });

    for (var i = 0; i < WORK_COLUMNS.length; i++) {
      var c = WORK_COLUMNS[i];
      list.push({
        name: c.name,
        label: c.label,
        kind: 'work',
        poly: circlePoly(p.workX, c.y, p.workRadius, 16)
      });
    }
    return list;
  }

  function prepared(params, opts) {
    var obs = obstacles(params, opts);
    var out = [];
    for (var i = 0; i < obs.length; i++) {
      out.push({ o: obs[i], box: aabb(obs[i].poly) });
    }
    return out;
  }

  function hitAgainst(y, rotate, p, obs) {
    var parts = handParts(y, rotate, p);
    var names = [];
    var pairs = [];
    for (var i = 0; i < parts.length; i++) {
      var box = aabb(parts[i].poly);
      for (var j = 0; j < obs.length; j++) {
        if (obs[j].o.kind === 'work') continue;
        if (!boxOverlap(box, obs[j].box)) continue;
        if (!polyIntersects(parts[i].poly, obs[j].o.poly)) continue;
        var n = obs[j].o.name;
        if (names.indexOf(n) < 0) names.push(n);
        pairs.push([box, obs[j].box]);
      }
    }
    return { hit: names.length > 0, names: names, pairs: pairs };
  }

  function hitTest(y, rotate, params, opts) {
    var p = withDefaults(params);
    var r = hitAgainst(y, rotate, p, prepared(params, opts));
    return { hit: r.hit, names: r.names };
  }

  // 深さの目安: 当たった部位と障害物の AABB の重なりの短辺を足す（面積計算を避けた近似）
  function depthScore(pairs) {
    var s = 0;
    for (var i = 0; i < pairs.length; i++) {
      var a = pairs[i][0];
      var b = pairs[i][1];
      var w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
      var h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
      if (w > 0 && h > 0) s += Math.min(w, h);
    }
    return s;
  }

  function scanRect(y0, y1, r0, r1, n, p, obs, acc) {
    var ny = y1 > y0 ? n : 1;
    var nr = r1 > r0 ? n : 1;
    for (var iy = 0; iy < ny; iy++) {
      var y = ny === 1 ? y0 : y0 + ((y1 - y0) * iy) / (ny - 1);
      for (var ir = 0; ir < nr; ir++) {
        var r = nr === 1 ? r0 : r0 + ((r1 - r0) * ir) / (nr - 1);
        var h = hitAgainst(y, r, p, obs);
        if (!h.hit) continue;
        for (var k = 0; k < h.names.length; k++) {
          if (acc.names.indexOf(h.names[k]) < 0) acc.names.push(h.names[k]);
        }
        var score = depthScore(h.pairs);
        if (!acc.worst || score > acc.worstScore) {
          acc.worst = { y: y, rotate: r };
          acc.worstScore = score;
        }
      }
    }
  }

  // (y_axis 始点〜終点) × (rotate 始点〜終点) の矩形領域全体を見る（軸は同期しない）
  function sweepHit(rect, params, opts) {
    var p = withDefaults(params);
    var obs = prepared(params, opts);
    var y0 = Math.min(rect.y0, rect.y1);
    var y1 = Math.max(rect.y0, rect.y1);
    var r0 = Math.min(rect.r0, rect.r1);
    var r1 = Math.max(rect.r0, rect.r1);
    var acc = { names: [], worst: null, worstScore: -1 };

    scanRect(y0, y1, r0, r1, 21, p, obs, acc);

    if (acc.worst) {
      var dy = (y1 - y0) / 20;
      var dr = (r1 - r0) / 20;
      var fy0 = Math.max(y0, acc.worst.y - dy);
      var fy1 = Math.min(y1, acc.worst.y + dy);
      var fr0 = Math.max(r0, acc.worst.rotate - dr);
      var fr1 = Math.min(r1, acc.worst.rotate + dr);
      scanRect(fy0, fy1, fr0, fr1, 9, p, obs, acc);
    }
    return { hit: acc.names.length > 0, names: acc.names, worst: acc.worst };
  }

  // cells / owner は y 優先の並び: index = iy * nR + ir
  function cspace(params, opts, grid) {
    var p = withDefaults(params);
    var nY = grid && grid.nY ? grid.nY : 200;
    var nR = grid && grid.nR ? grid.nR : 120;
    var obs = prepared(params, opts);
    var obstacleNames = [];
    for (var i = 0; i < obs.length; i++) {
      if (obs[i].o.kind !== 'work') obstacleNames.push(obs[i].o.name);
    }
    var cells = new Uint8Array(nY * nR);
    var owner = new Uint8Array(nY * nR);
    for (var iy = 0; iy < nY; iy++) {
      var y = nY === 1 ? Y_MIN : Y_MIN + ((Y_MAX - Y_MIN) * iy) / (nY - 1);
      for (var ir = 0; ir < nR; ir++) {
        var r = nR === 1 ? R_MIN : R_MIN + ((R_MAX - R_MIN) * ir) / (nR - 1);
        var h = hitAgainst(y, r, p, obs);
        if (!h.hit) continue;
        var idx = iy * nR + ir;
        cells[idx] = 1;
        owner[idx] = obstacleNames.indexOf(h.names[0]) + 1;
      }
    }
    return {
      nY: nY,
      nR: nR,
      yMin: Y_MIN,
      yMax: Y_MAX,
      rMin: R_MIN,
      rMax: R_MAX,
      order: 'y-major',
      cells: cells,
      owner: owner,
      obstacleNames: obstacleNames
    };
  }

  function cspaceIndex(iy, ir, g) {
    return iy * g.nR + ir;
  }

  var CALIBRATION_POINTS = [
    { label: 'y=0 / rotate=0 は干渉する（実機記録）', point: { y: 0, rotate: 0 }, expectHit: true },
    { label: 'rotate=0 なら y_axis >= 40mm で干渉しない', point: { y: 40, rotate: 0 }, expectHit: false },
    { label: 'y_axis=0 なら rotate >= 10deg で干渉しない', point: { y: 0, rotate: 10 }, expectHit: false }
  ];

  function calibration(params, opts) {
    var out = [];
    for (var i = 0; i < CALIBRATION_POINTS.length; i++) {
      var c = CALIBRATION_POINTS[i];
      var r = hitTest(c.point.y, c.point.rotate, params, opts);
      out.push({
        label: c.label,
        point: { y: c.point.y, rotate: c.point.rotate },
        expectHit: c.expectHit,
        actualHit: r.hit,
        ok: r.hit === c.expectHit,
        names: r.names
      });
    }
    return out;
  }

  var Geom = {
    DEFAULT_PARAMS: DEFAULT_PARAMS,
    PARAM_META: PARAM_META,
    DEFAULT_WALL_F: DEFAULT_WALL_F,
    WORK_COLUMNS: WORK_COLUMNS,
    Y_MIN: Y_MIN,
    Y_MAX: Y_MAX,
    R_MIN: R_MIN,
    R_MAX: R_MAX,
    handParts: handParts,
    obstacles: obstacles,
    hitTest: hitTest,
    sweepHit: sweepHit,
    cspace: cspace,
    cspaceIndex: cspaceIndex,
    calibration: calibration,
    polyIntersects: polyIntersects,
    pointInPoly: pointInPoly,
    rectPoly: rectPoly,
    withDefaults: withDefaults
  };

  root.Geom = Geom;
  if (typeof module !== 'undefined' && module.exports) module.exports = Geom;
})(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this);
