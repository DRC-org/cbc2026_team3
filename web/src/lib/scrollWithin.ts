/**
 * 最寄りの `.scroll` 容器の中だけを動かして要素を見せる。
 *
 * `scrollIntoView` は overflow:hidden の外枠 (main) まで巻き込んで動かすので、ステップ一覧が
 * 現在行を追うたびに画面全体が上へずれ、モード切替や START が見えなくなった (2026-09-12 実機)。
 */
export function scrollWithinContainer(
  el: HTMLElement | null,
  block: "nearest" | "center" | "start" = "nearest",
): void {
  if (!el) return;
  const container = el.parentElement?.closest<HTMLElement>(".scroll") ?? null;
  if (!container) return;
  const cr = container.getBoundingClientRect();
  const er = el.getBoundingClientRect();
  let delta = 0;
  if (block === "center") delta = er.top + er.height / 2 - (cr.top + cr.height / 2);
  else if (block === "start") delta = er.top - cr.top;
  else if (er.top < cr.top) delta = er.top - cr.top;
  else if (er.bottom > cr.bottom) delta = er.bottom - cr.bottom;
  if (delta === 0) return;
  if (typeof container.scrollBy === "function")
    container.scrollBy({ top: delta, behavior: "smooth" });
  else container.scrollTop += delta;
}
