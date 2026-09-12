// config/main_hand_positions.yaml と sequences/main_hand.py を写したデータと、
// move_to ごとの (y_axis 始点〜終点) × (rotate 始点〜終点) 矩形領域の展開。
(function (root) {
  'use strict';

  var POSITIONS = {
    y_axis: {
      home: 0,
      clear: 42,
      work_1: 75,
      work_1_after_1: 45,
      work_1_after_2: 225,
      work_2: 270,
      work_2_after_1: 240,
      work_3: 466,
      work_3_after_1: 396,
      work_3_after_2: 366,
      work_shared: 550,
      work_shared_after_1: 400
    },
    rotate: {
      home: 5,
      work_1_after_2: 90,
      work_3_after_1: 165,
      work_3_after_2: 155,
      work_shared_after_1: 185,
      pick: 185,
      pick_shared: 165,
      via_place: 0,
      place: 5,
      after_place: 30
    },
    wall_f: {
      initial: 270,
      closed: 180,
      assist: 216,
      open: 80
    },
    gripper: {
      open: 0,
      closed: 65
    },
    conveyor: {
      stop: 0,
      slow: { red: 0.15, blue: -0.15 },
      run: { red: 0.3, blue: -0.3 }
    }
  };

  var RANGES = {
    y_axis: { min: 0, max: 650 },
    rotate: { min: 0, max: 185 },
    rotateTravel: { min: 0, max: 200 }
  };

  var PRE_CONVEYOR = { rotate: 'place', wall_f: 'open', conveyor: 'stop' };
  var TO_CONVEYOR = { y_axis: 'home', rotate: 'place', wall_f: 'open', conveyor: 'stop' };
  var HOME = {
    y_axis: 'home',
    rotate: 'home',
    gripper: 'open',
    wall_f: 'initial',
    conveyor: 'stop'
  };
  var RELEASE = { gripper: 'open' };

  function pickAt(work) {
    return { y_axis: work, rotate: 'pick', _sync: true };
  }

  var ACTIVE = [
    { step: '初期位置へ移動', enabled: true, moves: [HOME] },
    { step: '2 列目ワークへ移動', enabled: true, moves: [pickAt('work_2')] },
    {
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: true,
      moves: [{ wall_f: 'closed' }, { conveyor: 'run' }]
    },
    { step: '2 列目ワークを把持', enabled: true, moves: [{ gripper: 'closed' }] },
    {
      step: '2 列目ワークをコンベアの位置へ',
      enabled: true,
      moves: [{ wall_f: 'open' }, { y_axis: 'work_2_after_1' }, PRE_CONVEYOR, TO_CONVEYOR]
    },
    {
      step: '2 列目ワークをリリース',
      enabled: true,
      moves: [RELEASE, { rotate: 'after_place', y_axis: 'clear' }]
    },
    { step: '1 列目ワークへ移動', enabled: true, moves: [pickAt('work_1')] },
    {
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: true,
      moves: [{ wall_f: 'closed' }, { conveyor: 'run' }]
    },
    { step: '1 列目ワークを把持', enabled: true, moves: [{ gripper: 'closed' }] },
    {
      step: '1 列目ワークをコンベアの位置へ',
      enabled: true,
      moves: [
        { y_axis: 'work_1_after_1' },
        { y_axis: 'work_1_after_2', rotate: 'work_1_after_2', wall_f: 'open' },
        PRE_CONVEYOR,
        TO_CONVEYOR
      ]
    },
    {
      step: '1 列目ワークをリリース',
      enabled: true,
      moves: [RELEASE, { rotate: 'after_place', y_axis: 'clear' }]
    },
    {
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: true,
      moves: [
        { y_axis: 'work_1_after_2', rotate: 'work_1_after_2' },
        { wall_f: 'closed' },
        { conveyor: 'run' }
      ]
    },
    { step: '初期位置へ復帰', enabled: true, moves: [HOME] }
  ];

  var FULL = [
    { step: '初期位置へ移動', enabled: true, moves: [HOME] },
    { step: '3 列目ワークへ移動', enabled: false, moves: [pickAt('work_3')] },
    { step: '3 列目ワークを把持', enabled: false, moves: [{ gripper: 'closed', wall_f: 'open' }] },
    {
      step: '3 列目ワークをコンベアの位置へ',
      enabled: false,
      moves: [
        { y_axis: 'work_3_after_1', rotate: 'work_3_after_1' },
        { y_axis: 'work_3_after_2', rotate: 'work_3_after_2' },
        { wall_f: 'open' },
        PRE_CONVEYOR,
        TO_CONVEYOR
      ]
    },
    {
      step: '3 列目ワークをリリース',
      enabled: false,
      moves: [RELEASE, { rotate: 'after_place', y_axis: 'clear' }]
    },
    {
      step: '共通ワークへ移動',
      enabled: false,
      moves: [
        { y_axis: 'work_shared', rotate: 'pick', _sync: true },
        { y_axis: 'work_shared', rotate: 'pick_shared', _sync: true }
      ]
    },
    {
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: false,
      moves: [{ wall_f: 'closed' }, { conveyor: 'run' }]
    },
    { step: '共通ワークを把持', enabled: false, moves: [{ gripper: 'closed' }] },
    {
      step: '共通ワークを引き出す',
      enabled: false,
      moves: [{ y_axis: 'work_shared_after_1', rotate: 'work_shared_after_1' }]
    },
    { step: '共通ワークを開放', enabled: false, moves: [{ gripper: 'open' }] },
    { step: '3 列目に置いた共通ワークへ移動', enabled: false, moves: [pickAt('work_3')] },
    { step: '3 列目に置いた共通ワークを把持', enabled: false, moves: [{ gripper: 'closed' }] },
    {
      step: '共通ワークをコンベアの位置へ',
      enabled: false,
      moves: [
        { y_axis: 'work_3_after_1', rotate: 'work_3_after_1' },
        { y_axis: 'work_3_after_2', rotate: 'work_3_after_2' },
        { wall_f: 'open' },
        PRE_CONVEYOR,
        TO_CONVEYOR
      ]
    },
    {
      step: '共通ワークをリリース',
      enabled: false,
      moves: [RELEASE, { rotate: 'after_place', y_axis: 'clear' }]
    },
    { step: '2 列目ワークへ移動', enabled: false, moves: [pickAt('work_2')] },
    {
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: false,
      moves: [{ wall_f: 'closed' }, { conveyor: 'run' }]
    },
    { step: '2 列目ワークを把持', enabled: false, moves: [{ gripper: 'closed' }] },
    {
      step: '2 列目ワークをコンベアの位置へ',
      enabled: false,
      moves: [{ wall_f: 'open' }, { y_axis: 'work_2_after_1' }, PRE_CONVEYOR, TO_CONVEYOR]
    },
    {
      step: '2 列目ワークをリリース',
      enabled: false,
      moves: [RELEASE, { rotate: 'after_place', y_axis: 'clear' }]
    },
    { step: '1 列目ワークへ移動', enabled: false, moves: [pickAt('work_1')] },
    {
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: false,
      moves: [{ wall_f: 'closed' }, { conveyor: 'run' }]
    },
    { step: '1 列目ワークを把持', enabled: false, moves: [{ gripper: 'closed' }] },
    {
      step: '1 列目ワークをコンベアの位置へ',
      enabled: false,
      moves: [
        { y_axis: 'work_1_after_1' },
        { y_axis: 'work_1_after_2', rotate: 'work_1_after_2', wall_f: 'open' },
        PRE_CONVEYOR,
        TO_CONVEYOR
      ]
    },
    {
      step: '1 列目ワークをリリース',
      enabled: false,
      moves: [RELEASE, { rotate: 'after_place', y_axis: 'clear' }]
    },
    {
      // sequences/main_hand.py:218 のまま。rotate.work_1_after_1 は存在しない位置名（直さずに残す）
      step: 'コンベアの壁を閉じてワークを寄せる',
      enabled: false,
      moves: [
        { y_axis: 'work_1_after_1', rotate: 'work_1_after_1' },
        { wall_f: 'closed' },
        { conveyor: 'run' }
      ]
    },
    { step: '初期位置へ復帰', enabled: true, moves: [HOME] }
  ];

  var PLANS = { active: ACTIVE, full: FULL };

  var LABEL_ORDER = ['y_axis', 'rotate', 'wall_f', 'gripper', 'conveyor'];

  function lookup(positions, axis, name) {
    var table = positions && positions[axis];
    if (!table || !Object.prototype.hasOwnProperty.call(table, name)) return null;
    return { value: table[name] };
  }

  function rectangles(planName, positions) {
    var plan = PLANS[planName];
    if (!plan) return [];
    var pos = positions || POSITIONS;
    var state = {
      names: { y_axis: null, rotate: null, wall_f: null, gripper: null, conveyor: null },
      y: (lookup(pos, 'y_axis', 'home') || { value: 0 }).value,
      rotate: (lookup(pos, 'rotate', 'home') || { value: 5 }).value,
      wallF: (lookup(pos, 'wall_f', 'initial') || { value: 270 }).value
    };
    var out = [];
    var index = 0;

    for (var s = 0; s < plan.length; s++) {
      var step = plan[s];
      for (var m = 0; m < step.moves.length; m++) {
        var move = step.moves[m];
        var errors = [];
        var changed = [];
        var next = { y: state.y, rotate: state.rotate, wallF: state.wallF };

        for (var a = 0; a < LABEL_ORDER.length; a++) {
          var axis = LABEL_ORDER[a];
          if (!Object.prototype.hasOwnProperty.call(move, axis)) continue;
          var name = move[axis];
          var found = lookup(pos, axis, name);
          if (!found) {
            errors.push('位置名が存在しない: ' + axis + '.' + name);
            changed.push(axis + '→' + name);
            continue;
          }
          if (state.names[axis] !== name) changed.push(axis + '→' + name);
          state.names[axis] = name;
          if (axis === 'y_axis') next.y = found.value;
          else if (axis === 'rotate') next.rotate = found.value;
          else if (axis === 'wall_f') next.wallF = found.value;
        }

        var from = { y: state.y, rotate: state.rotate };
        var to = { y: next.y, rotate: next.rotate };
        out.push({
          index: index++,
          stepIndex: s,
          stepLabel: step.step,
          moveLabel: changed.length ? changed.join(', ') : '変化なし',
          from: from,
          to: to,
          y0: Math.min(from.y, to.y),
          y1: Math.max(from.y, to.y),
          r0: Math.min(from.rotate, to.rotate),
          r1: Math.max(from.rotate, to.rotate),
          wallF: next.wallF,
          movesBothAxes: from.y !== to.y && from.rotate !== to.rotate,
          sync: move._sync === true,
          enabled: step.enabled !== false,
          unresolved: errors.length > 0,
          error: errors.length ? errors.join(' / ') : null
        });

        state.y = next.y;
        state.rotate = next.rotate;
        state.wallF = next.wallF;
      }
    }
    return out;
  }

  var Steps = {
    POSITIONS: POSITIONS,
    RANGES: RANGES,
    PLANS: PLANS,
    rectangles: rectangles
  };

  root.Steps = Steps;
  if (typeof module !== 'undefined' && module.exports) module.exports = Steps;
})(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this);
