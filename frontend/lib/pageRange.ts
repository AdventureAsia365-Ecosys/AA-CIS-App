// lib/pageRange.ts — which page of a server-paged list is on screen (AA-1006 follow-up, S263 F4 / F3).
//
// A react-query list that keeps the previous page visible while the next one loads (`placeholderData: (prev) =>
// prev`) shows the OLD rows until the new page arrives. Row numbers and the "21–40 of 892" range must then follow
// the rows on screen, not the page the user just asked for: Master Content numbered page 1's rows 21–40 during
// "Refreshing…". The response's own `pagination` (page / page_size, or offset / limit) says which page its rows are;
// the requested values are only the fallback before the first answer.

/** A server answer's paging: page-numbered (`page`, 1-based, + `page_size`) or offset-based (`offset` + `limit`). */
export interface ServerPaging {
  page?: number | null;
  page_size?: number | null;
  offset?: number | null;
  limit?: number | null;
  total?: number | null;
}

export interface ShownRange {
  /** 1-based page of the rows on screen. */
  page: number;
  pageSize: number;
  /** Rows before the first one on screen (the row number of row i is offset + i + 1). */
  offset: number;
  /** First and last row number on screen (0 / 0 when there are no rows). */
  from: number;
  to: number;
  total: number;
}

function positiveInt(v: number | null | undefined): number | null {
  return typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.floor(v) : null;
}

/**
 * The range of the rows on screen. `paging` is the answer those rows came with (undefined before the first one);
 * `requested` is the page the list asked for; `rowCount` the number of rows on screen.
 */
export function shownRange(
  paging: ServerPaging | null | undefined,
  requested: { page: number; pageSize: number },
  rowCount: number,
): ShownRange {
  const pageSize = positiveInt(paging?.page_size) ?? positiveInt(paging?.limit) ?? Math.max(1, requested.pageSize);
  const answeredPage = positiveInt(paging?.page);
  let offset: number;
  if (answeredPage != null) offset = (answeredPage - 1) * pageSize;
  else if (typeof paging?.offset === "number" && paging.offset >= 0) offset = Math.floor(paging.offset);
  else offset = (Math.max(1, requested.page) - 1) * pageSize;
  const page = Math.floor(offset / pageSize) + 1;
  const rows = Math.max(0, rowCount);
  const seen = rows > 0 ? offset + rows : 0;
  const total = Math.max(typeof paging?.total === "number" ? paging.total : seen, seen);
  return {
    page,
    pageSize,
    offset,
    from: rows > 0 ? offset + 1 : 0,
    to: rows > 0 ? offset + rows : 0,
    total,
  };
}
