import { describe, expect, it } from "vitest";
import { paginateRows } from "./tablePagination";
import type { DataRow } from "./types";

describe("paginateRows", () => {
  it("splits rows into 20-row pages", () => {
    const rows = makeRows(45);

    const firstPage = paginateRows(rows, 1);
    const secondPage = paginateRows(rows, 2);
    const thirdPage = paginateRows(rows, 3);

    expect(firstPage.totalPages).toBe(3);
    expect(firstPage.pageRows).toHaveLength(20);
    expect(firstPage.pageRows[0]).toEqual({ id: 1 });
    expect(firstPage.pageRows[19]).toEqual({ id: 20 });
    expect(secondPage.pageRows).toHaveLength(20);
    expect(secondPage.pageRows[0]).toEqual({ id: 21 });
    expect(thirdPage.pageRows).toHaveLength(5);
    expect(thirdPage.pageRows[0]).toEqual({ id: 41 });
    expect(thirdPage.pageRows[4]).toEqual({ id: 45 });
  });

  it("clamps page numbers to the available range", () => {
    const rows = makeRows(45);

    expect(paginateRows(rows, -1).page).toBe(1);
    expect(paginateRows(rows, 99).page).toBe(3);
  });

  it("uses one empty page when there are no rows", () => {
    const page = paginateRows([], 2);

    expect(page.page).toBe(1);
    expect(page.totalPages).toBe(1);
    expect(page.pageRows).toEqual([]);
  });
});

function makeRows(count: number): DataRow[] {
  return Array.from({ length: count }, (_, index) => ({ id: index + 1 }));
}
