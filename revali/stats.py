"""`revali stats`: what history.jsonl says about how the pipeline is doing."""
from collections import defaultdict
from typing import List

from revali.state import read_history

TERMINAL = ("ready_to_merge", "merged", "needs_human")


def pipelines(rows: List[dict]) -> List[dict]:
    """Collapse history rows into one record per pipeline (repo, branch, PR).

    Every stop writes a row (needs_action, ready_to_merge, merged...), and the
    counters and cost in each row are cumulative, so the last row of a pipeline
    is its current state. `fallback` is kept if any row set it."""
    latest = {}
    order = []
    for r in rows:
        key = (r.get("repo") or "(unknown repo)", r.get("branch") or "", int(r.get("pr") or 0))
        if key not in latest:
            order.append(key)
            latest[key] = dict(r)
        else:
            merged = dict(r)
            merged["fallback"] = bool(latest[key].get("fallback")) or bool(r.get("fallback"))
            latest[key] = merged
    return [latest[k] for k in order]


def summarise(rows: List[dict]) -> str:
    if not rows:
        return "no runs recorded yet"
    runs = pipelines(rows)
    by_repo = defaultdict(list)
    for r in runs:
        by_repo[r.get("repo") or "(unknown repo)"].append(r)
    out = ["history rows: %d, pipelines: %d" % (len(rows), len(runs)), ""]
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
        cost = sum(float(r.get("cost_usd", 0) or 0) for r in items)
        rate = ("%d/%d" % (len(first_try), len(passed))) if passed else "-"
        mean_rounds = ("%.1f" % (sum(rounds) / len(rounds))) if rounds else "-"
        out.append("| %s | %d | %d | %s | %d | %d | %d | %s | $%.2f |" % (
            repo, len(items), len(terminal), rate, len(merged), len(human), len(fallback), mean_rounds, cost))
    models = defaultdict(int)
    for r in runs:
        for m in r.get("models", []) or []:
            models[m] += 1
    if models:
        out += ["", "models seen: " + ", ".join("%s (%d)" % (m, n) for m, n in sorted(models.items()))]
    verdicts = defaultdict(int)
    for r in runs:
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
