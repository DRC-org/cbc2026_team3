import { memo } from "react";

import { HomingPanel } from "@/components/homing/HomingPanel";
import { SwitchMeasurePanel } from "@/components/homing/SwitchMeasurePanel";
import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import { Panel } from "@/components/ui/Panel";
import { ScrollArea } from "@/components/ui/ScrollArea";

export const ActuatorCheck = memo(function ActuatorCheck() {
  return (
    <Panel
      legend="アクチュエータ動作確認"
      className="min-h-0 flex-1"
      bodyClassName="p-0"
      actions={<MotorCheckSummary />}
    >
      <ScrollArea className="px-2 py-1.5">
        <div className="flex shrink-0 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <MotorCheckButton />
          </div>
          <MotorCheckPanel />
          {/* 起動口は動作確認だけ (零点確定はその中で走る)。機体単独の零点合わせは操縦者画面 */}
          <HomingPanel />
          <SwitchMeasurePanel />
        </div>
      </ScrollArea>
    </Panel>
  );
});
