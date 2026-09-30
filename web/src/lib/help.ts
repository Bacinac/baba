// BABA's articles (help/index.json and a body per language), for the frame's
// "?", the /help pages and every Hint.
import { Help, type HelpEntry } from "$lib/kit";
import index from "./help/index.json";

export const help = new Help(
  index as HelpEntry[],
  import.meta.glob("./help/*.md", { query: "?raw", import: "default", eager: true }),
);
