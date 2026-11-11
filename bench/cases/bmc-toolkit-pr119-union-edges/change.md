---
title: Join an NVMe figure's pages past nested tables and changing inner columns
kind: fix
author_model: claude-opus-5-5
---

## Request
"OK" to T69 split into a survey (T69.1) and a fix (T69.2), and "OK" to
the survey's recommendation: variant B (nested tables ignored, column
edges may be a subset, and with the caption repeated above the next part
only the outer frame must match), rows put on the union of the parts'
column edges, and PR #118 round 3 F1/F2 taken in.

## Goal
A multi-page NVMe figure whose pages differ only by tables nested in its
cells or by inner column edges reads as one Logical Table (Figure 123 of
NVME-NVM, which G108 reads, pages 85-93), with its rows on one column
set, and no table that joins today prints differently.

## Acceptance criteria
- AC-1: A table lying inside another table's box on the same page
  (nested in a cell) does not count when deciding whether a table is the
  last on its page or the first on the next: an outer table with a nested
  table in its last rows, carried on the next page under its repeated
  caption, reads as one Logical Table, and the nested table stays a
  Logical Table of its own. Test fails on develop.
- AC-2: Two parts whose left and right edges match within 6 pt and whose
  column edges are a subset one of the other (every edge of the part
  with fewer within 6 pt of an edge of the other) join, with or without
  a caption above the second. Test fails on develop.
- AC-3: When every line above the second part is a caption that repeats
  one on the page before or the two before (the existing repeat rule),
  only the left and right edges must match within 6 pt; inner edges may
  differ. Test fails on develop.
- AC-4: No join when the left or the right edge differs by more than
  6 pt; when inner edges are not a subset and no repeated caption stands
  above; when another caption or any other line stands above (as today);
  or when a table not nested in it follows the first part on its page.
  Synthetic tests.
- AC-5: A joined table's rows are put on the union of its parts' column
  edges (edges within 6 pt are one edge, the first part's position
  kept): a cell lands under the edge it starts at and empty cells fill
  the edges it spans, so every row has the same number of cells and a
  header repeated on a part with fewer columns is dropped as a repeat.
  The Logical Table's `columns` is that union. A table whose parts all
  have the same column edges reads exactly as before (rows and columns).
  Tests: a five-column part and a four-column part (one inner edge
  missing) give rows of five cells and one header.
- AC-6 (PR #118 round 3 F1): The page-before rule also takes a caption
  whose id is a single letter followed by a separator (`Table A - Annex
  codes`, `Table B: Codes`); `Table: Supported commands` (no id) stays
  rejected and docs/COMMANDS.md says so. Test fails on develop.
- AC-7 (PR #118 round 3 F2): The page-before rule and the line-bottom
  fallback read a table id from one shared pattern, so the fallback takes
  `Table II - Limits`; a Roman numeral counts as an id only when
  followed by whitespace, a separator or the end of the line, so the
  fallback takes `Table 9 - Table I/O map`. Tests fail on develop.
- AC-8: TABLES_VERSION becomes 13; tests that pin the number change only
  that number.
- AC-9 (manual, survey): (i) NVME-NVM, NVME-MI and NVME-BASE: at least
  the 149 split pairs T69.1's variant B joined are joined, no join puts
  together parts under different captions, NVME-NVM Figure 123 is one
  Logical Table on pages 85-93 with its header once, and its `table`
  output read against the page images (NSZE 07:00, FLBAS 26, LBAF0
  131:128 as G108 states). (ii) The 20 PDFs of T64, old and new: every
  Logical Table whose parts, rows or columns change is listed; outside
  the joins of (i), each change is checked on the page; a table that
  joined before keeps its rows.
- AC-10: NVME-NVM's `limits` no longer says Figure 123 reads as five
  Logical Tables (the Figure 157/159 sentence stays); docs/SUPPORT.md
  regenerated; docs/COMMANDS.md describes nested tables, subset edges,
  the frame rule under a repeated caption, and the union of columns.
  Tests that pin the Figure 123 sentence change with it.
- AC-11: The full suite passes, ruff check and format are clean. Label
  `patch`.

## Out of scope
- The 15 split pairs variant B leaves (a line above the part that is not
  the repeated caption; an outer frame or column count that really
  differs, Figure 338 at page 374 among them): they stay split.
- A nested table that itself runs across a page break.
- `Table 5 SHALL ...` read as a caption by the fallback (PR #118
  response-2).

## Dependencies
none

## What
- `bmc_toolkit/spec/tables.py`:
  - `Reader.outer_tables(page)`: the page's tables less those inside
    another table's box (`_nested_in`, 1 pt slack, smaller area).
    `continues` and `_walk` use it for "last on its page" / "first on the
    next" (AC-1).
  - `continues`: the columns pass when they match as before, or are a
    subset (`_columns_subset`: same left and right edges, every edge of
    the part with fewer on an edge of the other, AC-2), or when lines
    stand above the second part and only the frame matches
    (`_frame_match`); those lines must still all be repeated captions
    (the existing check, now over the same `above` list), so the frame
    rule only applies under a repeated caption (AC-3).
  - `_assemble`: `union_columns` (the first part's edges, then each later
    edge more than COLUMN_TOL_PT from all taken) and `spread_row` (each
    cell under the union edge nearest its left edge, empty cells
    elsewhere; a part whose edges equal the union keeps its rows as they
    are) put every part's rows on one column set before the header
    check; the Logical Table's `columns` is the union (AC-5).
  - `_ID`: one id pattern for `_TABLE_CAPTION` and `_TABLE_ID`: a number
    as before, or an upper-case Roman numeral or single letter followed
    by whitespace, a separator or the end (`(?-i:...)`, so `table a, b`
    is not an id) (AC-6, AC-7).
  - TABLES_VERSION 13.
- `tests/test_join_nested_parts.py`: AC-1 (nested table in the last row,
  joined; the nested one stays its own), AC-2 (subset edges with and
  without a caption), AC-3 (inner edge 10 pt off under the caption),
  AC-4 (shifted without a caption; right or left edge 10 pt in; another
  caption; a table after the first part that is not nested), AC-5 (union
  edges, one header, the wider part first, a 4 pt shift reads as
  before). AC-1, AC-2, AC-3 and AC-5 fail on develop.
- `tests/test_caption_to_line_bottom.py`: AC-6 (`Table A -`, `Table B:`
  taken on the page before; `Table:` and `Table a, b and c` not), AC-7
  (`Table II - Limits`, `Table 9 - Table I/O map`, `Table IV: Codes`
  taken by the fallback). The taken cases fail on develop.
- `bmc_toolkit/spec/catalog.toml`: NVME-NVM's limits keep only the Figure
  157/159 sentence. `docs/SUPPORT.md` regenerated (that row).
- `docs/COMMANDS.md`: the join rule (nested tables, subset edges, the
  frame under a repeated caption, rows on all the parts' edges) and the
  page-before id (`Table A -`; not `Table: ...`).
- Existing tests changed:
  - TABLES_VERSION 12 to 13, the number only (and `is_12` to `is_13` in
    the names of my own tests): `test_figure_captions.py`,
    `test_page_lines_word_gap.py`, `test_table_word_gap.py`,
    `test_table_section.py`, `test_review_captionbefore.py`,
    `test_review_captionbottom.py`, `test_review_captionpast.py`,
    `test_review_captionrepeat.py`, `test_review_figcaption.py`,
    `test_review_pagelinesgap.py`, `test_review_wordgap.py`.
  - Four tests built two parts that now join under AC-2 or AC-3; their
    point (parts that do not belong together stay apart) is kept by
    giving the parts geometry that still does not join, the assertions
    unchanged:
    `test_caption_not_furniture.py::test_a_repeated_caption_over_tables_that_do_not_join_names_both`
    and `test_review_captionrepeat.py::test_ac1_a_caption_repeated_over_parts_with_other_columns_names_both`
    (second part's right edge 20 pt out instead of only other inner
    edges; `OTHER_COLS` in the reviewer file);
    `test_caption_not_furniture.py::test_a_repeat_of_a_caption_two_pages_back_still_joins`
    (page 1's frame 30 pt wider, so page 1 stays apart as asserted);
    `test_review_tablemerge.py::test_different_column_count_stops_the_merge`
    (page 2's edges 232 instead of 252, not a subset of page 1's).
  - The Figure 123 limit pins (AC-10): `test_catalog_t61.py` now asserts
    the limits do not name Figure 123; `test_review_catalogt61.py`
    checks the Figure 157/159 sentence instead; `test_review_captionrepeat.py`
    AC-7 asserts the limits no longer name Figure 123.
- Manual (AC-9), develop 3754a35 in a detached worktree against this
  branch, `D:/999_Temp/bmc-t69/tools/dump_all.py` on the 20 PDFs of T64
  and `diff_dumps.py` (`D:/999_Temp/bmc-t69/diff.json`):
  - No Logical Table with the same parts changes rows, columns or
    caption (0 of 3,295).
  - NVMe: 95 new joins (NVME-NVM 18, NVME-MI 17, NVME-BASE 60) over 159
    seams; of T69.1's 164 split pairs 149 now sit inside one table; no
    join puts together parts under different captions (the pieces
    without one are pages the table fills from the top). NVME-NVM
    Figure 123 is one table, pages 85-93, 86 rows on 6 columns, header
    once; NSZE 07:00, NCAP 15:08, NUSE 23:16, NLBAF 25, FLBAS 26, LBAF0
    131:128, LBAF1 135:132 as G108 states, pages 85 and 93 checked on
    images.
  - Outside NVMe, 6 joins, each checked on an image of the page break and
    each a continuation: DSP0240 Table 4 (pages 26-27; the repeated
    header now dropped, one row fewer), DSP0248 1.3.1 Tables 13 and 15
    (60-61, 62-63), DSP0248 1.4.0 page 103-104 (Response data), DSP0251
    Table 36 (83-84; header dropped), OCP-NIC primary connector pinout
    (100-101).
  - Full suite 3753 passed (`D:/999_Temp/bmc-t69/suite.log`); ruff check
    and format clean.

## Why
G108 reads NVME-NVM Figure 123 (Identify Namespace), which `table`
printed as five tables; NVME-BASE's Identify Controller (Figure 338) was
about thirty. NVMe draws tables inside cells and changes a figure's inner
columns from page to page; T69.1 measured 164 such breaks in the three
NVMe documents (`D:/999_Temp/bmc-t69/report.md`).
