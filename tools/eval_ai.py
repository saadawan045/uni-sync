"""Score the event-detection AI on synthetic messages (tools/eval_cases.json).

    PYTHONPATH=. .venv/bin/python tools/eval_ai.py [--model qwen2.5:7b] [--only id1,id2] [-v]

Per case the result is what you'd have experienced:
  ok        right calendar change (or correctly nothing)
  missed    an important event was not added / changed
  wrong     a change was made, but with the wrong date/time/kind
  unwanted  an event was added for something that isn't one
  unsure    a "please check" email instead of acting
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from unisync import config, llm
from unisync.extract import normalize, propose


def decide(cfg, proposed):
    """Same cleanup + guards as Applier.apply → (applied, unsure)."""
    applied, unsure = [], []
    for it, quote, sent in proposed:
        it = normalize(cfg, it, quote, sent)
        if it is None or not (it.get("title") or "").strip() or not it.get("date"):
            continue
        (applied if float(it.get("confidence") or 0) >= cfg.min_confidence else unsure).append(it)
    return applied, unsure


def matches(exp: dict, got: dict) -> bool:
    if got.get("action") != exp["action"] or got.get("date", "")[:10] != exp["date"]:
        return False
    if got.get("kind") not in exp["kind"]:
        return False
    for k in ("start", "new_date", "new_start"):
        if k in exp and (got.get(k) or "")[:len(exp[k])] != exp[k]:
            return False
    return True


def score(case, applied, unsure) -> str:
    exp = case["expect"]
    if not exp:
        return "unwanted" if applied else ("unsure" if unsure else "ok")
    if case.get("optional") and not applied:
        return "ok"
    if all(any(matches(e, g) for g in applied) for e in exp):
        return "ok"
    if applied:
        return "wrong"
    return "unsure" if unsure else "missed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--only")
    ap.add_argument("--no-verify", action="store_true", help="skip the double-check step")
    ap.add_argument("-v", action="store_true", help="print what the AI returned")
    a = ap.parse_args()
    cfg = config.load()
    if a.model:
        cfg = dataclasses.replace(cfg, model=a.model)
    if a.no_verify:
        cfg = dataclasses.replace(cfg, verify=False)
    raw: list = []
    real_extract = llm.extract
    llm.extract = lambda *x, **k: raw.extend(r := real_extract(*x, **k)) or r  # keep raw output for printing
    data = json.loads((Path(__file__).parent / "eval_cases.json").read_text())
    now = datetime.fromisoformat(data["now"]).replace(tzinfo=cfg.tz)
    cases = [c for c in data["cases"] if not a.only or c["id"] in a.only.split(",")]
    tally, slow = {}, []
    t_all = time.time()
    for c in cases:
        n = len(c["msgs"])
        msgs = [{"n": i + 1, "sender": s, "text": t, "time": now - timedelta(minutes=2 * (n - i)), "group": c.get("group", False)}
                for i, (s, t) in enumerate(c["msgs"])]
        t0 = time.time()
        raw.clear()
        proposed = propose(cfg, "test", c["where"], msgs, now=now)
        items = list(raw)
        dt = time.time() - t0
        slow.append(dt)
        applied, unsure = decide(cfg, proposed)
        r = score(c, applied, unsure)
        tally[r] = tally.get(r, 0) + 1
        mark = "✓" if r == "ok" else "✗"
        print(f"{mark} {r:8} {c['id']:20} {dt:4.0f}s", flush=True)
        if a.v or r != "ok":
            if len(proposed) < len(items):
                print(f"       (double-check dropped {len(items) - len(proposed)})")
            for it in items:
                keys = ("action", "kind", "title", "day_words", "date", "start", "new_date", "new_start", "confidence")
                print("      ", {k: it.get(k) for k in keys if it.get(k) not in (None, "")})
    total = len(cases)
    print(f"\n{cfg.model}: {tally.get('ok', 0)}/{total} ok | " +
          ", ".join(f"{k} {v}" for k, v in sorted(tally.items()) if k != "ok") +
          f" | avg {sum(slow)/len(slow):.0f}s, max {max(slow):.0f}s, total {time.time()-t_all:.0f}s")


if __name__ == "__main__":
    main()
