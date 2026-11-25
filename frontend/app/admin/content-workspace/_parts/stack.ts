// app/admin/content-workspace/_parts/stack.ts — AA-1148: the one-column grid of the content workspace editor.
//
// A bare `display: grid` has one implicit `auto` column, sized at least to the min-content width of its children.
// A row of an input (flex: 1, min-width: 0) and an Add button does not shrink below the input's intrinsic width
// (~300 px), so at 390 px the column grew past the page frame and the frame scrolled sideways (the "Visual (deployed
// host)" run of 25/11, content-workspace-new at 390). `minmax(0, 1fr)` lets the column shrink to the frame; at any
// width where the content fits, the layout is the same as before.
import type { CSSProperties } from "react";

export const stack = (gap: number): CSSProperties => ({ display: "grid", gap, gridTemplateColumns: "minmax(0, 1fr)" });
