---
title: Take a Table caption whose bottom lies within 40 points above the table
kind: fix
author_model: claude-opus-5-5
---

## Request
"As you suggest" to opening T68 after T67: PMBus Table 31 (its caption lies 47
points above the table, past two note lines, so `table` prints `-`) plus
PR #117 review F1 (`Table` case-insensitive in the page-before rule) and
F3 (docs/COMMANDS.md: running headers and page numbers are not body
text). "OK" to these acceptance criteria.

## Goal
`table` names PMBus Table 31's caption, and any other table whose `Table`
caption ends within 40 points above it but starts further up, without
changing a caption the existing rules already find.

## Acceptance criteria
- AC-1: A table that gets no caption from the 40 pt window or the Figure
  rule takes the nearest body line whose bottom lies within 40 pt above
  the table, when that line is a `Table N` caption (case-insensitive,
  margin line number dropped) whose title does not start with a
  lowercase letter, and no other table lies between it and the table.
  Test: a synthetic page with a caption whose top is 47 pt and bottom 36
  pt above the table, two note lines between, prints the caption. Fails
  on develop.
- AC-2: That fallback does not take a `Figure` line (DSP0274's Figure 24
  shape: prose starting `Figure 24 — ...` above a figure), a prose line
  (`Table 3 lists ...`), a line whose bottom is more than 40 pt above the
  table, or a line with another table between. Synthetic tests.
- AC-3: A table that gets a caption from the existing rules keeps it; the
  order is the 40 pt window, the Figure rule, this fallback, the page
  before. Test: a page where the window rule and the fallback would both
  find a line prints the window rule's.
- AC-4 (PR #117 F1): The page-before rule takes `TABLE 5 - X` and
  `table 5 - x` as it takes `Table 5 - X`. Test fails on develop.
- AC-5 (PR #117 F3): docs/COMMANDS.md says, for the page-before rule, that
  running headers and page numbers do not count as text above the table
  or as the last line of the page before, and describes the new fallback.
- AC-6: TABLES_VERSION becomes 12, so `table` reads a stored
  `tables.json` of version 11 again; tests that pin the number change
  only that number.
- AC-7 (manual, survey): the 20 PDFs of T64 read with the old and the new
  rule; every table whose caption changes is listed with old and new
  caption and checked against the page as that table's own caption.
  PMBUS-II 1.3.1 page 112 becomes `Table 31. Command Summary`; no table
  that had a caption changes, and none changes to `-`.
- AC-8: PMBUS-II's `limits` no longer says Table 31's caption prints as
  `-`; docs/SUPPORT.md regenerated.
- AC-9: The full suite passes, ruff check and format are clean. Label
  `patch`.

## Out of scope
- The main 40 pt window keeps measuring to a line's top (T65 found that
  measuring to the bottom there replaced DSP0274's Figure 24 caption).
- `_captions_a_table` (the furniture guard) keeps the top-measured window.
- NVMe nested tables (Figure 123 in five parts, G108).

## Dependencies
none

## What
- `bmc_toolkit/spec/tables.py`: `Reader.caption` gains a fallback after
  the Figure rule and before the page-before rule,
  `_caption_to_line_bottom`: body lines whose bottom lies within
  CAPTION_GAP_PT above the table, nearest first, the first that
  `_table_caption` accepts with no other table between.
  `_table_caption` / `_TABLE_CAPTION` mirror `figure_caption` /
  `_FIGURE_CAPTION` for `Table` (case-insensitive): an id, an optional
  separator, a title that does not start lowercase and has a letter. The
  page-before rule compares `Table` case-insensitively (F1).
  TABLES_VERSION 12.
- `tests/test_caption_to_line_bottom.py`: AC-1 (PMBus geometry checked
  against CAPTION_GAP_PT; caption over two notes, with a margin number,
  upper case, IPMI's `Table 20-,` form; nothing between), AC-2 (Figure
  prose, three `Table` prose forms, bottom beyond 40 pt, a table between),
  AC-3 (window caption wins, Figure caption wins), AC-4 (`TABLE`, `table`
  on the page before). AC-1 and AC-4 fail on develop; the others guard
  behaviour that holds there.
- `bmc_toolkit/spec/catalog.toml`: PMBUS-II's `limits` removed (it held
  only the Table 31 caption). `docs/SUPPORT.md` regenerated with
  `catalog --table --by-family --golden docs/golden-questions.md`: that
  row's limit becomes `-`.
- `docs/COMMANDS.md`: the new fallback, and for the page-before rule that
  running headers and page numbers are not body text on either page (F3).
- Existing tests changed:
  - TABLES_VERSION 11 to 12, the number only (and `is_11` to `is_12` in
    the names of my own tests): `test_figure_captions.py`,
    `test_page_lines_word_gap.py`, `test_table_word_gap.py`,
    `test_table_section.py`, `test_review_captionbefore.py`,
    `test_review_captionpast.py`, `test_review_captionrepeat.py`,
    `test_review_figcaption.py`, `test_review_pagelinesgap.py`,
    `test_review_wordgap.py`.
  - `test_review_captionpastlimits.py`: the PMBUS-II test pinned the
    limit AC-8 removes; it now asserts the limit and the SUPPORT.md row no
    longer name Table 31, and the SUPPORT.md check takes an empty limit
    as `-`.
- Manual (AC-7), `D:/999_Temp/bmc-t68/tools/survey_ac7.py` on the 20 PDFs
  of T64 (3,295 Logical Tables; fewer than T64's 3,427 since T66 joins
  NVMe continuation pages), old rule = the fallback off and the
  page-before rule case-sensitive: 1 caption changes, PMBUS-II 1.3.1 page
  112 from `-` to `Table 31. Command Summary`, checked on a page image.
  No table that had a caption changes, none changes to `-`, and F1 changes
  no table in these 20 PDFs.

- Round 1 (APPROVE, three low findings, all fixed):
  - F1: `_table_caption` also rejects a title that starts with another
    `Table` mention or a parenthesis (`Table 12, Table 13 and ...`,
    `Table 3 (above) ...`); two more cases in the prose test.
  - F2: the page-before rule wants `Table`, in any case, followed by an
    id (`_TABLE_MENTION` at the start), so a wrapped `table.` or `table
    below:` is not taken. This is also narrower than develop: develop
    took a capitalised `Table of contents` ending a page, which is now
    rejected too (tested). Test with three such lines.
  - F3: `test_review_captionpastlimits.py`'s PMBUS-II test renamed to
    `test_pmbus_ii_no_longer_names_table_31` and the module docstring
    corrected (a finding asked for it; the reviewer test file of T65).
  - AC-7 rerun with the old rule's page-before part copied verbatim from
    develop (`D:/999_Temp/bmc-t68/ac7-r2.json`): still 1 change of 3,295,
    PMBUS-II page 112; full suite 3704 passed.

## Why
G45 reads PMBus Table 31, the command summary, and `table` printed its
caption as `-` (T65 left it as a limit). T65 measured the main window to
a line's bottom and lost DSP0274's Figure 24 caption to a prose line, so
this takes the bottom only for `Table` lines and only where every other
rule found nothing.
