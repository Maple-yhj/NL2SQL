import type { DataRow } from "./types";

export const TABLE_PAGE_SIZE = 20;

export interface PaginatedRows {
  page: number;
  pageSize: number;
  totalRows: number;
  totalPages: number;
  pageRows: DataRow[];
}

export function paginateRows(
  rows: DataRow[],
  requestedPage: number,
  pageSize = TABLE_PAGE_SIZE,
): PaginatedRows {
  const totalRows = rows.length;
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  const page = clamp(Math.trunc(requestedPage) || 1, 1, totalPages);
  const start = (page - 1) * pageSize;

  return {
    page,
    pageSize,
    totalRows,
    totalPages,
    pageRows: rows.slice(start, start + pageSize),
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
