import { Ruler } from "lucide-react";
import { useState } from "react";

import { SwitchMeasureBadge, SwitchMeasureResult } from "@/components/homing/SwitchMeasureResult";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { MalformedNotice } from "@/components/ui/MalformedNotice";
import { Modal } from "@/components/ui/Modal";
import { ClearanceWarning } from "@/components/ui/SafetyNotice";
import { useRobotStatus } from "@/context/RobotContext";
import { useSwitchMeasure } from "@/hooks/useSwitchMeasure";
import type { SwitchMeasureOptions } from "@/hooks/useSwitchMeasure";
import { cx } from "@/lib/cx";
import { MALFORMED } from "@/lib/protocol";
import type { SwitchDirection } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";
import { SWITCH_DIRECTIONS, directionLabel, switchMeasureStatus } from "@/lib/switchMeasureStatus";

const OPTION_FIELDS: { key: keyof SwitchMeasureOptions; label: string }[] = [
  { key: "step", label: "刻み" },
  { key: "coarse_step", label: "粗刻み" },
  { key: "limit", label: "上限" },
];

type Drafts = Record<keyof SwitchMeasureOptions, string>;

const EMPTY_DRAFTS: Drafts = { step: "", coarse_step: "", limit: "" };

interface ReadOptions {
  options: SwitchMeasureOptions;
  invalid: (keyof SwitchMeasureOptions)[];
}

function readOptions(drafts: Drafts): ReadOptions {
  const options: SwitchMeasureOptions = {};
  const invalid: (keyof SwitchMeasureOptions)[] = [];
  for (const { key } of OPTION_FIELDS) {
    const text = drafts[key].trim();
    if (text === "") continue;
    const value = Number(text);
    if (Number.isFinite(value) && value > 0) options[key] = value;
    else invalid.push(key);
  }
  return { options, invalid };
}

export function SwitchMeasurePanel() {
  const { connected } = useRobotStatus();
  const { state, start } = useSwitchMeasure();
  const [chosenRobot, setChosenRobot] = useState<string | null>(null);
  const [chosenAxis, setChosenAxis] = useState<string | null>(null);
  const [direction, setDirection] = useState<SwitchDirection | null>(null);
  const [drafts, setDrafts] = useState<Drafts>(EMPTY_DRAFTS);
  const [confirming, setConfirming] = useState(false);

  const { outcome, reasonLabel } = switchMeasureStatus(state, connected);

  const header = (
    <div className="flex items-center gap-2">
      <span className="text-base-content/70">作動点測定</span>
      <SwitchMeasureBadge outcome={outcome} />
      {state.robot !== null && state.axis !== null && state.direction !== null ? (
        <span className="flex min-w-0 items-center gap-1 truncate">
          <span>{robotLabel(state.robot)}</span>
          <span className="font-mono">{state.axis}</span>
          <span>{directionLabel(state.direction)}</span>
        </span>
      ) : null}
    </div>
  );

  if (state.targets === MALFORMED) {
    return (
      <div className="flex flex-col gap-1">
        {header}
        <MalformedNotice subject="作動点測定の対象" />
      </div>
    );
  }

  const targets = Object.entries(state.targets).filter(([, axes]) => axes.length > 0);
  if (targets.length === 0) {
    return (
      <div className="flex flex-col gap-1">
        {header}
        <p className="text-base-content/70">作動点を測定できる軸がありません。</p>
      </div>
    );
  }

  const [robot, axes] = targets.find(([name]) => name === chosenRobot) ?? targets[0];
  const axis = chosenAxis !== null && axes.includes(chosenAxis) ? chosenAxis : axes[0];
  const { options, invalid } = readOptions(drafts);
  const disabled = reasonLabel !== null || direction === null || invalid.length > 0;

  const optionSummary = OPTION_FIELDS.map(({ key, label }) => {
    const value = options[key];
    return `${label}: ${value === undefined ? "homing の既定" : value}`;
  }).join(" / ");

  return (
    <div className="flex flex-col gap-1.5">
      {header}

      <div className="flex flex-wrap items-center gap-2">
        <select
          className="select border-base-300 bg-base-100 select-sm"
          aria-label="測定するロボット"
          value={robot}
          disabled={state.running}
          onChange={(e) => {
            setChosenRobot(e.target.value);
            setChosenAxis(null);
          }}
        >
          {targets.map(([name]) => (
            <option key={name} value={name}>
              {robotLabel(name)}
            </option>
          ))}
        </select>
        <select
          className="select border-base-300 bg-base-100 font-mono select-sm"
          aria-label="測定する軸"
          value={axis}
          disabled={state.running}
          onChange={(e) => setChosenAxis(e.target.value)}
        >
          {axes.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        <div className="join">
          {SWITCH_DIRECTIONS.map((candidate) => (
            <Button
              key={candidate}
              className={cx(
                "join-item",
                direction === candidate && "border-info bg-info text-info-content",
              )}
              disabled={state.running}
              onClick={() => setDirection(candidate)}
              aria-pressed={direction === candidate}
            >
              {directionLabel(candidate)}
            </Button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {OPTION_FIELDS.map(({ key, label }) => (
          <label key={key} className="flex items-center gap-1 text-base-content/70">
            {label}
            <input
              type="number"
              inputMode="decimal"
              min={0}
              className={cx(
                "input w-28 border-base-300 bg-base-100 text-right font-mono tabular-nums input-sm",
                invalid.includes(key) && "border-warning",
              )}
              aria-label={label}
              placeholder="homing の既定"
              value={drafts[key]}
              disabled={state.running}
              onChange={(e) => setDrafts({ ...drafts, [key]: e.target.value })}
            />
          </label>
        ))}
        <Button
          disabled={disabled}
          onClick={() => setConfirming(true)}
          aria-label="作動点測定を開始"
        >
          {state.running ? (
            <span className="loading loading-xs loading-spinner" />
          ) : (
            <Icon as={Ruler} />
          )}
          作動点測定
        </Button>
      </div>

      {reasonLabel !== null ? (
        <span className="text-base-content/70">{reasonLabel}</span>
      ) : direction === null ? (
        <span className="text-base-content/70">向きを選んでください</span>
      ) : invalid.length > 0 ? (
        <span className="text-warning">刻み・粗刻み・上限は正の数で入力してください</span>
      ) : null}

      <SwitchMeasureResult state={state} />

      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        tone="danger"
        title="作動点測定"
        footer={
          <>
            <Button onClick={() => setConfirming(false)}>キャンセル</Button>
            <Button
              tone="info"
              onClick={() => {
                if (direction !== null) start(robot, axis, direction, options);
                setConfirming(false);
              }}
            >
              開始
            </Button>
          </>
        }
      >
        <p>
          <span className="font-medium text-info">{robotLabel(robot)}だけ</span>
          を動かします。リミットスイッチが入る位置と離れる位置を測ります。
        </p>
        <p className="mt-2">
          対象の軸: <span className="font-mono">{axis}</span>
        </p>
        <p>
          向き: <span className="font-mono">{axis}</span> を{" "}
          <span className="font-medium">{direction === null ? "" : directionLabel(direction)}</span>
          へ動かします
        </p>
        <p className="text-base-content/70">{optionSummary}</p>
        <ClearanceWarning />
      </Modal>
    </div>
  );
}
