import { TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { Icon } from "@/components/ui/Icon";

interface MalformedNoticeProps {
  /** 「〜を読み取れませんでした」の主語。何が読めなかったかは箇所ごとに違う */
  subject: string;
  /** 読めないことの影響。渡すと見出し + 本文の 2 段になる */
  detail?: ReactNode;
}

/** MALFORMED を黙って空にしないための警告。文面は主語だけが変わる */
export function MalformedNotice({ subject, detail }: MalformedNoticeProps) {
  if (detail === undefined) {
    return (
      <p className="flex items-center gap-1.5 text-warning">
        <Icon as={TriangleAlert} />
        {subject}を読み取れませんでした (配信の形が読めていません)
      </p>
    );
  }

  return (
    <div className="text-warning">
      <p className="flex items-center gap-1.5 font-medium">
        <Icon as={TriangleAlert} />
        {subject}を読み取れませんでした
      </p>
      <p className="mt-1">{detail}</p>
    </div>
  );
}
