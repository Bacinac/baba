import { describe, expect, it } from "vitest";
import { byLabel } from "./order";

describe("byLabel", () => {
  it("orders by the Croatian alphabet, digraphs included", () => {
    const names = ["Zagreb", "Čakovec", "Ćiro", "Cres", "Njivice", "Nin", "Ljubljana", "Lovran", "Džep", "Dubrovnik", "Šibenik", "Split"];
    expect(byLabel(names, (n) => n)).toEqual([
      "Cres", "Čakovec", "Ćiro", "Dubrovnik", "Džep", "Lovran", "Ljubljana", "Nin", "Njivice", "Split", "Šibenik", "Zagreb",
    ]);
  });

  it("sorts by the label it is given and leaves the input alone", () => {
    const items = [{ id: 2, name: "Vrt" }, { id: 1, name: "Dvorište" }];
    expect(byLabel(items, (i) => i.name).map((i) => i.id)).toEqual([1, 2]);
    expect(items.map((i) => i.id)).toEqual([2, 1]);
  });
});
