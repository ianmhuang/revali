---
title: A table's Citation names the section the table starts in
kind: fix
author_model: claude-fable-5-1
---

## Request
T24 (STATUS.md): the section column prints the wrong section. On 2026-10-04,
`/grill-with-docs T24`, questions 1-13 all answered "as suggested"; then "As you suggest, go."

## Goal
The section field of a `cite:` line names where the cited text is. Two
commands print a section that is not: `table` looks for the caption's words
on the page and takes the section of the first line holding them, so a table
without a caption gets the section in force at the top of the page (DSP0240
1.2.1 page 34: the response tables of SetTID and GetTID print `9.1 PLDM
Terminus`, the request tables `9.1.1` and `9.1.2`), and a caption quoted in
the body text above its table can give the section of the quote; `page DOC
--section Q` prints Q alone on every page of its range, also on a page that
runs into the next sections. Two of the four citation slips left after PR
#63 copied the first. This change makes both commands print the section the
text is in, and brings three descriptions of the lines field in line with
what the tool prints.

## Acceptance criteria
- AC-1: `table` prints, for each Logical Table, the Outline entry in force where the table starts on its first page: the last entry whose heading is above the table on that page, else the entry in force at the top of that page. A table without a caption that follows a section heading on its page prints that section, not the one at the top of the page.
- AC-2: A table whose caption text also appears earlier on the page, in body text under another section, prints the section its own position is in.
- AC-3: A table that continues over several pages prints the section in force where its first part starts; a heading on a later page of the table does not change it.
- AC-4: When no heading on the page can be placed above or below the table (the heading is not found in the page's text), `table` prints the entry in force at the top of the page and does not fail; with an empty Outline the section field is `-`, as before.
- AC-5: A `tables.json` written before this change is not trusted for the section: the next `table` call for a page reads that page again without `--force` and prints and stores the new section. The file keeps its keys, so a plugin of the previous version still reads the Library (it reads the pages again).
- AC-6: `page DOC --section Q` prints, for every page of its range, the same `cite:` line `page DOC N` prints for that page (every section the page spans); the pages chosen and the text printed under each `cite:` line do not change.
- AC-7: The output of `page DOC N [--to M]`, `find`, `section` and `render` does not change, and `table` output changes in the section field only.
- AC-8: `docs/COMMANDS.md` and SKILL.md describe both: the section of a `table` Citation as the one the table starts in, and `page --section` as printing the sections each page spans (SKILL.md's Citation definition no longer says `page --section` prints one section).
- AC-9: The lines field of a rendered page is described as `lines rendered page`, and that of a table as `lines table K`, in the `render` row of SKILL.md's command table, in `docs/COMMANDS.md` and in the module docstring of `bmc_toolkit/spec/search.py`.
- AC-10: SKILL.md changes only in the Citation definition and the `page`, `render` and `table` rows of the command table: the frontmatter, the block between the `command-workflow` markers and the `## Citation rules` section are byte-identical to develop.
- AC-11: (checked by the author, not a test) Every Logical Table already read in the offline Library copy used for the measurements (about 110 tables in 9 documents) is listed with its section before and after; each table whose section changes is read against its page and the new section is right, and the tables without a caption whose section does not change are read too. The same listing for `page --section` on the sections the measured runs asked for. Results in `measure/t24/results-<date>.md` of the design repository.
- AC-12: (measured by the author, not a test) Guard, not a target: 20 prompts x 3 on Sonnet 5.5, run as in PR #63 and scored with `measure/t24/cite_check.py`, against the baseline `measure/t23/runs-changed` (4 of 60 runs with a slip): at most 5 of 60 runs with a slip, no run writing `table K` for `lines table K`, encode content at least 35/36, no golden run citing a wrong place. The three P1e runs are reported on their own, without a bar.
  Measured 2026-10-04 (`measure/t24/results-2026-10-04.md`): 3 of 60 runs with a slip (met), no run writing `table K` (met), no golden run citing a wrong place (met), encode content 33/36 (not met; the bar is 35). P1e: 3 of 3 runs cite `9.1.2 GetTID command (0x02)` for both tables, where 2 of 3 baseline runs copied `9.1 PLDM Terminus`. The encode-content bar was waived by the owner on 2026-10-04 (「好，豁免」): the three failing runs read tool output that is the same on develop (none read a table whose section changes, none used `page --section`); one lost its answer to the runner, which keeps the final message only, one is the known SPDM GET_CAPABILITIES serialisation error, one wrote the queried PLDM type into the header; and the same prompts under the same encode rules scored 31, 34 and 36 of 36 in earlier arms, so a bar of the best arm minus one was set too tight.
- AC-13: (measured by the author, not a test) If AC-11 or AC-12 fails the branch is not merged and nothing is tuned to pass; the result goes back to the owner.
  Done on 2026-10-04: AC-11 passed; AC-12 failed on one bar, nothing was changed after the measurement, and the owner waived that bar.
- AC-14: (reported, no bar) After AC-12 passes, the 20 prompts once each on Opus 5.5 with the owner's profile, next to the Opus run of PR #63.
  Done on 2026-10-04 after the waiver (`measure/t24/results-2026-10-04.md`, section "Opus 5.5 spot check"; 20 runs, all exit 0): 2 of 20 runs with a slip (PR #63's Opus run: 1 of 20 with the same checker), 73 cite lines, no run writing `table K`. Both slips write one line number inside the printed range (G11-r1 `lines 238` for `lines 234-242`, S2e-r1 `lines 273` for `lines 263-273`). Encode content 11/12 (S2e-r1 leaves the ExtFlags bytes as placeholders, as in PR #63's run). 6 of 8 golden runs cite the golden row's pages; G4-r1 and G5-r1 cite other pages that hold the answer, G5-r1 with the section this change prints for the Get SEL Entry table.

## Out of scope
- A caption for the second table under one caption, or joining a request and a response table into one Logical Table.
- Printing only the text of the section for `page --section`.
- Table and figure bookmarks counted as sections (DSP0248).
- The wording of the Citation rules (findings F2 and F3 of PR #63), the Notes recorder, Notes already stored.
- A version bump (label `patch`; released with the next `Release` PR).

## Dependencies
none

## What
- `bmc_toolkit/spec/search.py`: `Version.section_below(page, above)` names
  the Outline entry in force below a set of lines of the page: the entry at
  the page top, unless an entry starting on the page has its heading among
  those lines (the last one wins; a heading the Extract does not hold counts
  as the page top, as `owning_section` reads it). The heading match is the
  one `_heading_index` used, moved to `_heading_at`, and also tried with a
  printed line number removed from the front of a line (the table reader's
  lines still carry them). `find_line` had one caller and is gone. The
  module docstring gives the lines field with its `lines` prefix.
- `bmc_toolkit/spec/tables.py`: a Logical Table's section is asked for with
  the body lines above the table on its first page, not with its caption.
  `TABLES_VERSION` 4 to 5, so stores written before are read again; the
  keys of `tables.json` do not change.
- `bmc_toolkit/spec/cli.py`: `table` passes `section_below`; `page
  --section` prints `cite(n)` for each page, as `page N` does.
- `skills/bmc-spec/SKILL.md`: the Citation definition says `table` names the
  section the table starts in (and no longer that `page --section` names
  one section); the `render` row says `lines rendered page`. Two lines.
- `docs/COMMANDS.md`: the same two descriptions, and the lines forms with
  their prefix.
- `tests/test_table_section.py`: nine tests for AC-1 to AC-6, AC-8, AC-9.
- Existing tests changed, each because it pinned what this change replaces:
  - `assert data["tables_version"] == 4` (14 lines) and
    `assert T.TABLES_VERSION == 4` (4 lines) now say 5, in
    test_curve_rules, test_review_cellboxes, test_review_curvecli,
    test_review_curvefill, test_review_cutrow, test_review_tablecli and
    test_tables. Nothing else in those tests changes.
  - Four tests asserted that `page --section` names the queried section
    alone, the behaviour AC-6 replaces (decided by the owner, T24 Q6):
    test_review_citespan (renamed
    `test_page_section_names_the_sections_each_page_spans`; it now checks
    each line against `page N`), two in test_review_sectionrange and one in
    test_search_cli. Each now expects the sections the page spans; the
    pages they expect are unchanged.

Checked by the author (measure/t24 in the design repo):
- AC-11: 109 Logical Tables already read in the offline Library copy. 20
  change section, all of them tables without a caption (IPMI 11,
  IPMI-UPDATE 4, DSP0236 1, DSP0240 4); each was read against its page and
  the new section is the heading above the table. The 12 tables without a
  caption that keep their section were read too and are right (one,
  DSP0248 page 84, keeps a table bookmark as its section, which is out of
  scope). 77 captioned tables do not change. `page --section` was asked 7
  distinct ways in 140 measured runs; every page now prints the line
  `page N` prints.

## Why
Two of the four citation slips left after PR #63 copied a section `table`
printed for the wrong table, and a model that read a page through `page
--section` was told the whole page was the queried section when most of it
was not. Both are the tool printing a section the text is not in; the
answer can only copy what is printed.
