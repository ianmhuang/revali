"""`revali stats`: what history.jsonl says about how the pipeline is doing."""
from collections import defaultdict
from typing import List

from revali.state import read_history

TERMINAL = ("ready_to_merge", "merged", "needs_human")


def summarise(rows: List[dict]) -> str:
    if not rows:
        return "no runs recorded yet"
    by_repo = defaultdict(list)
    for r in rows:
        by_repo[r.get("repo") or "(unknown repo)"].append(r)
    out = ["runs recorded: %d" % len(rows), ""]
    header = "| repo | runs | reached verdict | first-try pass | merged | needs human | fallback | mean rounds | cost |"
    out += [header, "|---|---|---|---|---|---|---|---|---|"]
    for repo, items in sorted(by_repo.items()):
        terminal = [r for r in items if r.get("stage") in TERMINAL]
        passed = [r for r in terminal if r.get("stage") in ("ready_to_merge", "merged")]
        first_try = [r for r in passed if int(r.get("fixes", 0)) == 0]
        merged = [r for r in items if r.get("stage") == "merged"]
        human = [r for r in items if r.get("stage") == "needs_human"]
        fallback = [r for r in items if r.get("fallback")]
        rounds = [int(r.get("rounds", 0)) for r in terminal if int(r.get("rounds", 0)) > 0]
        cost = sum(float(r.get("cost_usd", 0) or 0) for r in items if r.get("stage") in TERMINAL)
        rate = ("%d/%d" % (len(first_try), len(passed))) if passed else "-"
        mean_rounds = ("%.1f" % (sum(rounds) / len(rounds))) if rounds else "-"
        out.append("| %s | %d | %d | %s | %d | %d | %d | %s | $%.2f |" % (
            repo, len(items), len(terminal), rate, len(merged), len(human), len(fallback), mean_rounds, cost))
    models = defaultdict(int)
    for r in rows:
        for m in r.get("models", []) or []:
            models[m] += 1
    if models:
        out += ["", "models seen: " + ", ".join("%s (%d)" % (m, n) for m, n in sorted(models.items()))]
    verdicts = defaultdict(int)
    for r in rows:
        verdicts[r.get("last_verdict") or "-"] += 1
    out += ["last verdicts: " + ", ".join("%s %d" % (k, v) for k, v in sorted(verdicts.items()))]
    return "\n".join(out)


def cmd_stats(args) -> int:
    from revali.config import ConfigError, history_path, load_user_config
    try:
        path = history_path(load_user_config())
    except ConfigError:
        path = history_path(None)
    print("history: %s" % path)
    print(summarise(read_history(path)))
    return 0
