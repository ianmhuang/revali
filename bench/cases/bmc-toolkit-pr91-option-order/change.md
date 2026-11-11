---
title: invocation pldm --subcommand prints the pldmtool subcommand line from the held openbmc/pldm tree
kind: feature
author_model: claude-fable-5-1
---

## Request
Having it offered unprompted takes another mechanism; assess that ... OK, draft ADR 0007 and the acceptance criteria first.

(T46, after T45 (2026-10-19, two paired runs, dropped): a SKILL.md rule
that had the Session look the pldmtool subcommand up in the held
openbmc/pldm Code Tree was right when followed (every line given in 42
runs was the source's but one value) and skipped in 11 of 42 runs, so it
missed its bar twice (12/18, 10/18 against 16/18). The helper's output is
copied 36/36 since T27, so the lookup moves into the `invocation` call
the Session already makes. ADR 0007 (draft, design docs) sets the
boundary: `invocation` reads one Code Tree for the spelling of a
subcommand and its options, cites it, fills in no value.)

## Goal
`invocation pldm --subcommand <Name> FIELD...` prints, after the bytes,
the raw lines and the field table, the pldmtool subcommand's registration
and option lines from the held openbmc/pldm tree with a code cite and a
template line with every value a placeholder; says `none` when pldmtool
has no such subcommand and `not held` when the Library has no pldm tree.
The block tells the Session to add `--subcommand` for a PLDM Command and
copy what is printed. A paired measurement against develop shows the
subcommand line right and cited on the T45 prompts, raw bytes and guards
unchanged. If the target fails, the change is dropped, not reworded.

## Acceptance criteria
Helper (deterministic, `tests/` with a synthetic pldm tree written by the
test: a `pldmtool/pldm_platform_cmd.cpp` and `pldm_base_cmd.cpp` holding
`add_subcommand`, `std::make_unique<Class>(...)`, `add_option`, `add_flag`
and `->required()` in the shapes of 71b1b2b, plus an `oem/ibm/` file;
never a copy of the real files):
- AC-1: `invocation pldm --subcommand NAME FIELD...` prints what
  `invocation pldm FIELD...` prints (bytes, both raw lines, the table),
  unchanged and first, then a `subcommand:` block. `--subcommand` with
  `ipmi`, `mctp-control` or `spdm` is refused with exit 2 and a message
  naming `pldm`; without FIELDs it is refused as today.
- AC-2: NAME is matched case-insensitively against the first string of
  every `add_subcommand(` call under `pldmtool/` (`*.cpp`, `oem/**`) of
  the pldm Code Tree that `grep pldm` without `--ref` would read. The
  type is the parent of that call: the variable the registration adds to
  (`base->`, `platform->`, ...) resolved to its own `add_subcommand("<type>"`
  string, or `oem-ibm` under `oem/ibm/`. The class is the template
  argument of the `std::make_unique<Class>(` call that takes the
  subcommand's variable; failing that, a class whose name equals NAME
  case-insensitively.
- AC-3: Printed for a match: the registration line, a `cite: code | pldm
  <commit7> | ...` line for the class's constructor lines (from `explicit
  Class(` or `using CommandInterface::CommandInterface;` to the closing
  brace, at most 60 lines) and those lines numbered as `code` prints
  them, then `subcommand: pldmtool <type> <Name> -m <eid>` followed by one
  item per `add_option` / `add_flag` in source order: `<short> <long>` with
  the long name without dashes as the placeholder (`-i <sensor_id>`),
  long-only options as `--name <name>`, a flag as `[-a]`, an option without
  `->required()` in brackets (`[-s <state_set_id>]`). An option whose
  `->transform(CLI::CheckedTransformer(<map>` names a map in the same file
  also gets that map's lines printed under their own cite. The option
  spellings come from the string literal as written (`"-i, --sensor_id"`,
  spaces dropped), concatenated literals joined. The last line of a match
  is `note: spellings of pldm <commit7> (<provenance as the cite labels
  it>); the target's pldmtool may differ: check with pldmtool <type>
  <Name> --help`: the BMC's pldmtool may be another version than the
  tree read (owner's concern, 2026-10-19), so the answer carries where
  the spellings came from and the check to run there.
- AC-4: No `add_subcommand` match: `subcommand: none in pldmtool/` (a
  hit in a name map such as `{"GetSensorThresholds", PLDM_...}` is not a
  match). Tree not held: `subcommand: pldm Code Tree not held; clone
  pldm`. Class found but no `add_option` / `add_flag` readable: the
  registration, cite and lines are printed and the template carries
  `-m <eid>` only, with `subcommand: options not read; see the lines
  above`. Exit 0 in all three; the raw lines are printed in all three.
- AC-5: `bmc_toolkit/spec/invocation.py` imports the code module only
  inside the `--subcommand` path; the module docstring and ADR 0006's
  sentence in it say what the option reads. `docs/COMMANDS.md`'s
  `invocation` entry documents the option, its three outcomes and that it
  reads the pldm Code Tree only; README's Command sentence names the
  subcommand line within the 200-line cap and its network-and-disk list
  stays true (nothing new downloaded or written).
- AC-6: Against the real tree at 71b1b2b (manual, not CI: `measure/t45/
  key.md`), the template of every one of the 37 subcommands in
  `measure/t45/grade.py` `SOURCE` matches its spellings and required set;
  differences are listed in key.md before the run.
- AC-7: Block: the PLDM bullet of step 2 says to add `--subcommand <Name>`
  to the `invocation` call for a PLDM Command, copy the `subcommand:` and
  `note:` lines with the placeholders filled from the request and the
  code cite it prints, say the tool allocates the instance id and name the fields the
  subcommand fixes; `none`: raw only; `not held`: raw and the clone hint.
  The block grows by at most two lines over develop's 44 (`MAX_LINES` 46
  in the nine files that pin it, with the T46 reason). An author test
  pins the sentence and fails on develop; no existing assertion weakened.
- AC-8: `ruff check`, `ruff format --check` and the full suite pass.

Measurement (manual, `claude -p`, Sonnet 5.5, skill forced, clean profile,
offline Library copies per arm holding the pldm tree at 71b1b2b, both
arms at the same moment, the develop arm a detached worktree;
`measure/t46/` reusing T45's prompts, keys and grader):
- AC-9 (target): P1e, P2e, P5, P6, P7, P8 x 3 and P9 x 3 per arm, graded
  by `measure/t45/grade.py` with its mention and other-command rules:
  branch subcommand line right and cited >= 16/18, runs with a wrong line
  <= 1/21, P9 without a subcommand line 3/3; develop reported. Reported:
  runs whose `invocation` call carried `--subcommand`, the printed
  template copied with values filled (not a bar), raw lines printed and
  copied, the instance-id and fixed-fields sentences by hand.
  Measured 2026-10-19 (`measure/t46/results-pair-2026-10-19.md`): right
  and cited 9/18 (develop 0/18), wrong 3/21, P9 3/3; both bars missed.
  Every run called with `--subcommand` (21/21), the template was copied
  15/18, the `note:` line 18/18; the six runs short of "right" wrote
  the line right and the provenance as prose without the `cite: code`
  line, the three wrong lines are placeholders left unfilled, not wrong
  values. Owner waived both bars and took the change (the reasons in
  the results file). Grader change after the run, stated there: lines
  whose only option is `--help` (the note's check, copied as told) are
  not counted as subcommand lines.
- AC-10 (guards, same run): the 12 encode prompts x 3 with
  `measure/t21/score_enc.py` and `measure/t27/inv_calls.py`: bytes
  >= 34/36, printed line copied >= 34/36, refused `invocation` <= 3/36,
  encode wall mean <= 1.25 x develop's in the same run (no extra helper
  call this time; the four PLDM prompts' wall reported apart).
- AC-11: About 115 runs. After the run the owner is asked once before the
  C: run directories and the worktree are cleared.

## Out of scope
- A shipped table of subcommands (ADR 0005). Filling any value in
  (ADR 0006): the template's values are placeholders.
- Cloning the pldm tree from `invocation` (network; the hint names
  `clone pldm`).
- The same for ipmitool subcommands (`ipmitool sensor`, `sdr`, ...) or
  `mctp` tools: not asked, no inventory.
- pldmtool's own bugs (SetNumericEffecterValue, ActivateFirmware; T45
  key.md): not answered for.
- Decoding pldmtool's JSON or `Tx:`/`Rx:` output (ADR 0005).

## Dependencies
- None new in the repository (`git` is already used by `grep` and `code`).
  A user's Library needs the openbmc/pldm Code Tree for the subcommand
  block; without it the option prints the clone hint.

## What
- `bmc_toolkit/spec/pldmtool.py` (new, stdlib only): finds
  `add_subcommand("<Name>")` in the `pldmtool/` sources of a held
  openbmc/pldm Code Tree, the class registered for it
  (`std::make_unique<Class>`), its constructor span and the `add_option`
  / `add_flag` calls in it (literal concatenation, `->required()`,
  `CheckedTransformer(<map>)`), and prints the registration line, the
  constructor lines (at most 60, the rest counted) and any name map,
  each with a `cite: code` line, then `subcommand: pldmtool <type> <Name>
  -m <eid> ...` with every value a placeholder, and `note: spellings of
  pldm <commit7> (<provenance>); the target's pldmtool may differ: check
  with pldmtool <type> <Name> --help`.
- `bmc_toolkit/spec/invocation.py`: `--subcommand NAME`; the bytes and
  lines print first, then the subcommand block; `none in pldmtool/`,
  `pldm Code Tree not held; clone pldm` and `options not read` are the
  other outcomes, exit 0; another PROTOCOL with the option is exit 2.
  The code module is imported lazily inside the option's path.
- `bmc_toolkit/spec/cli.py`: `_pldm_tree` selects the pldm tree as the
  reading commands do (user checkout, config Release, default branch)
  and hands it to `cmd_invocation`.
- `skills/bmc-spec/SKILL.md`: the PLDM bullet of step 2 gains two lines
  (add the option to the helper call of step 4; copy the `subcommand:`
  and `note:` lines, values filled, with cite; `none`: raw only), block
  44 to 46 lines; the Helper CLI table row describes the option.
- `docs/COMMANDS.md`, `README.md`: the option, its output and the three
  other outcomes; `invocation` now has one Library read.
- Tests: `tests/test_invocation_subcommand.py` (synthetic tree in the
  71b1b2b shapes: base, platform and oem files, a two-line
  registration, a name map, a `using` constructor, a class whose
  options are not in the constructor, an 84-line constructor; the CLI
  wiring on a real `git` clone when git is present); an author test in
  `tests/test_command_workflow.py` pinning the block sentence, failing
  on develop. Three existing tests changed, none weakened in what it
  checked: `tests/test_review_everyline.py` allows the two new lines
  outside step 4 (`LATER_GROWTH`); `tests/test_review_cmdblock.py`'s
  pldmtool-form test reads the raw forms only, since the block now also
  holds a `pldmtool <type> <Name>` form; `tests/test_invocation.py`'s
  word list for the table row and COMMANDS.md says `--subcommand`
  where it said "no Library", because that claim no longer holds. The
  nine files pinning `MAX_LINES` say 46 with the T46 reason.

## Why
T45 showed the subcommand line is right when the Session reads the
source and that a SKILL.md rule does not make it read the source often
enough (31/42). The helper's output is what the Session copies (36/36
since T27), so the lookup moved into the helper (ADR 0007): the
spellings come from the held commit with a cite, no value is filled,
no table ships. The `note:` line exists because the BMC's pldmtool may
be another version than the tree read: the raw line beside it runs on
any version, the subcommand line carries where its spelling came from
and the check to run.
