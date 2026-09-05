"""revali - headless review / validate / merge pipeline for feature branches."""

NAME = "revali"
VERSION = "0.2.0"
CONFIG_VERSION = 1
STATE_VERSION = 3
PROMPT_VERSION = "5"  # bumped when prompts/, schemas/ or checklists/ change

# Exit codes shared by every subcommand.
EXIT_OK = 0  # done / ready to merge
EXIT_ERROR = 1  # pipeline error, not a verdict
EXIT_ACTION = 2  # the author session must act (fix, rebase, answer)
EXIT_HUMAN = 3  # a human must decide

V1_KINDS = ("feature", "fix", "docs")
ALL_KINDS = ("feature", "fix", "docs", "refactor", "hotfix", "small")
V1_PLATFORMS = ("linux",)
