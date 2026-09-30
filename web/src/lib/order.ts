import { compareHr } from "$lib/kit";

/** A list a person picks from reads in their alphabet, by the words they see
 *  rather than the keys behind them. */
export function byLabel<T>(items: readonly T[], label: (item: T) => string): T[] {
  return [...items].sort((a, b) => compareHr(label(a), label(b)));
}
