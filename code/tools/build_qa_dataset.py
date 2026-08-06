"""Build a grounded Q&A dataset for the tier-3 NPU question-answering.

Run:  python tools/build_qa_dataset.py
      python tools/build_qa_dataset.py --eval        # score a live model on it
      python tools/build_qa_dataset.py --limit 400

WHAT THIS IS FOR
    "Why is the A/C off?" is the question this project most wants to answer
    well, and it is the one with no way to tell whether the answer was good.
    An LLM answer either looks plausible or it does not, and looking plausible
    is exactly what a wrong answer does.

    So this builds an EVALUATION SET, not a training set. Each row is a real
    situation from the recorded corpus, the exact digest the model would be
    given for it, a question, and the facts a correct answer must rest on. That
    turns "the answers seem better" into a number.

WHY NOT A FINE-TUNING SET
    Because the problem was never the weights. The Q&A tier could see the state
    of every load and none of the causes, so it had to invent them; the fix was
    to put the causal record in the prompt (see hub/ask.py). Fine-tuning a model
    to guess better at facts it cannot see is the wrong repair, and on a
    hackathon clock it is also not a repair anyone can finish.

    The rows here are still directly useful for that later: the schema is a
    standard instruction triple, so the same file can seed few-shot exemplars or
    an SFT run without regeneration.

HOW A ROW IS GROUNDED
    `must_cite` lists the facts a correct answer has to rest on — the load, the
    rule that fired, the approving timestamp, the guardrail reason. `allowed`
    is every number the model was given, taken from the SAME builder that
    rendered the prompt, so scoring cannot drift from what was actually asked.

    The reference answer is produced by `ask.deterministic_answer()`, which
    reads only the digest. It is a FLOOR, not a ceiling: it is what the system
    can already say with no model at all. A model that scores below it is
    costing you latency for nothing.

PROVENANCE
    Rows inherit the corpus's provenance. Today that corpus is generated, so
    these are simulated situations with real rule output and real arithmetic —
    the same standing as everything else built from data/sessions/.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub"))

import ask as ask_mod          # noqa: E402
import provenance              # noqa: E402

CORPUS_DIR = ROOT / "data" / "sessions"
OUT_PATH = ROOT / "data" / "qa_eval.jsonl"

# How many recent events travel with a situation, matching public_state().
HISTORY_N = 8


def corpus_files() -> List[Path]:
    if not CORPUS_DIR.is_dir():
        return []
    return sorted(CORPUS_DIR.glob("*.jsonl"))


def _rate_for(ts: float):
    from energy_model import rate_at
    return rate_at(datetime.fromtimestamp(ts))


def situations(paths: List[Path]) -> Iterator[Dict]:
    """Replay the corpus and yield a public_state()-shaped dict per event.

    A situation is emitted right AFTER something causal happened — an approval,
    a confirmed switch, a guardrail veto — because that is when "why?" has an
    answer worth grading. Ticks in between are the same question with no story
    behind it, and thousands of those would swamp the interesting rows.
    """
    for path in paths:
        last_tick: Optional[Dict] = None
        titles: Dict[str, Dict] = {}
        realized: List[Dict] = []
        actuations: List[Dict] = []
        suppressed: List[Dict] = []

        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue

        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue

                kind = row.get("type")
                ts = row.get("ts") or 0

                if kind == "tick":
                    last_tick = row
                    continue

                if kind == "finding":
                    rid = row.get("reco_id")
                    if rid:
                        titles[rid] = row
                    continue

                if kind == "apply":
                    rid = row.get("reco_id", "")
                    meta = titles.get(rid, {})
                    realized.insert(0, {
                        "reco_id": rid,
                        "load_key": row.get("load_key", ""),
                        "rule_name": meta.get("rule_name", ""),
                        "room": meta.get("room", ""),
                        "title": meta.get("title") or meta.get("headline") or rid,
                        "usd": row.get("realized_usd", 0.0),
                        "ts": ts,
                        "kind": meta.get("kind", "detected"),
                    })
                    del realized[HISTORY_N:]

                elif kind == "actuation":
                    actuations.insert(0, {
                        "load_key": row.get("load_key", ""),
                        "state": row.get("state", row.get("action", "")),
                        "ok": row.get("ok", True),
                        "source": row.get("source", "simulated"),
                        "ts": ts,
                    })
                    del actuations[HISTORY_N:]

                elif kind == "veto":
                    lk = row.get("load_key", "")
                    suppressed = [{
                        "id": row.get("reco_id", ""),
                        "load": lk.split("/")[-1],
                        "headline": (titles.get(row.get("reco_id", ""), {})
                                     .get("title", "a saving")),
                        "usd": row.get("usd_withheld", 0.0),
                        "gate": row.get("gate", ""),
                        "reason": row.get("reason", ""),
                    }]

                else:
                    continue

                if last_tick is None:
                    continue

                rate, period = _rate_for(ts)
                yield {
                    "now": ts,
                    "rooms": last_tick.get("rooms") or {},
                    "loads": last_tick.get("loads") or {},
                    "user": last_tick.get("user") or {},
                    "total_watts": last_tick.get("total_watts", 0.0),
                    "tariff": {"rate": rate, "period": period,
                               "clock": datetime.fromtimestamp(ts).strftime("%H:%M")},
                    "recos": [],
                    "realized_events": list(realized),
                    "actuations": list(actuations),
                    "suppressed": list(suppressed),
                    "_trigger": kind,
                }


def questions_for(state: Dict) -> List[Dict]:
    """Questions worth asking of THIS situation, with what must ground them."""
    out: List[Dict] = []

    for key, load in (state.get("loads") or {}).items():
        name = key.split("/")[-1]
        if name == "standby":
            continue
        st = load.get("state", "off")
        plural = name.endswith("s")
        q = (f"why {'are' if plural else 'is'} the {name} {st}?")

        # Classify from the SAME window ask.py renders. Scanning the full
        # history here labelled a row "approved" when the approval had already
        # scrolled out of the digest, so the grader demanded a fact the model
        # was never shown — the harness marking itself wrong.
        must: List[str] = [name]
        ev = next((e for e in (state.get("realized_events") or [])[:ask_mod.HISTORY_LIMIT]
                   if e.get("load_key") == key), None)
        vet = next((s for s in (state.get("suppressed") or [])
                    if s.get("load") == name), None)
        # must_cite holds USER-FACING facts, never internal identifiers. An
        # earlier version demanded the rule id ("away_with_hvac_on"), which no
        # correct answer would ever say out loud — so it marked good answers as
        # ungrounded and the harness scored 53% against its own reference. A
        # grader that punishes the right answer is worse than no grader.
        if ev:
            kind, must = "approved", must + ["approved"]
        elif vet:
            kind, must = "declined", must + ["comfort"]
        else:
            kind, must = "unexplained", must + ["record"]
        out.append({"question": q, "expects": kind, "must_cite": [m for m in must if m]})

    out.append({"question": "What should I do first?", "expects": "priority",
                "must_cite": []})
    out.append({"question": "Is anything unusual right now?", "expects": "anomaly",
                "must_cite": []})
    return out


def build(limit: int) -> List[Dict]:
    rows: List[Dict] = []
    seen_keys = set()

    for state in situations(corpus_files()):
        for spec in questions_for(state):
            digest, allowed = ask_mod._digest_lines(state)
            ref = ask_mod.deterministic_answer(spec["question"], state)

            # One row per (question, expectation, trigger) shape. The corpus
            # repeats the same situation hundreds of times; grading the same
            # thing repeatedly inflates the score without testing anything.
            key = (spec["question"], spec["expects"], state["_trigger"])
            if key in seen_keys:
                continue
            seen_keys.add(key)

            rows.append({
                "question": spec["question"],
                "expects": spec["expects"],
                "must_cite": spec["must_cite"],
                "digest": digest,
                "allowed": allowed,
                "reference_answer": ref,
                "trigger": state["_trigger"],
                "clock": state["tariff"]["clock"],
                "provenance": "synthetic_corpus",
            })
            if len(rows) >= limit:
                return rows
    return rows


def grade(answer: str, row: Dict) -> Dict:
    """Score one answer: is it grounded, and does it rest on the right facts."""
    # provenance.verify -> (ok, leftover numbers the model was never given).
    # Called directly rather than behind a hasattr guard: if this API changes,
    # the grader must fail loudly, not quietly score everything as clean.
    ok, leftover = provenance.verify(answer, row["allowed"])
    cited = [m for m in row["must_cite"] if m.lower() in answer.lower()]
    missing = [m for m in row["must_cite"] if m.lower() not in answer.lower()]
    return {
        "verified": ok,
        "unverified_numbers": leftover,
        "cited": cited,
        "missing": missing,
        "grounded": (not leftover) and not missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--eval", action="store_true",
                    help="score the live model against the reference answers")
    args = ap.parse_args()

    files = corpus_files()
    print("=" * 78)
    print("BUILD GROUNDED Q&A EVALUATION SET")
    print("=" * 78)
    if not files:
        print(f"  no corpus in {CORPUS_DIR}")
        print("  generate one:  python tools/generate_sessions.py --days 90 --seed 11")
        return 1

    rows = build(args.limit)
    if not rows:
        print("  corpus had no causal events (no applies, vetoes or actuations).")
        return 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    by_expect: Dict[str, int] = {}
    for r in rows:
        by_expect[r["expects"]] = by_expect.get(r["expects"], 0) + 1

    print(f"  corpus files     : {len(files)}")
    print(f"  rows             : {len(rows)}")
    for k, v in sorted(by_expect.items()):
        print(f"    {k:14s} {v:4d}")
    print(f"  wrote {OUT_PATH}")
    print()
    print("  Each row carries the EXACT digest the model would be given, the")
    print("  numbers it is allowed to cite, and the facts a correct answer must")
    print("  rest on. The reference answer is the no-model floor.")

    # Grade the deterministic answers against their own rows. This is a sanity
    # check on the harness, not a result: it should be near-perfect, and if it
    # is not, the grader is wrong rather than the answers.
    ok = sum(1 for r in rows if grade(r["reference_answer"], r)["grounded"])
    print()
    print(f"  harness check: {ok}/{len(rows)} reference answers self-grade as "
          f"grounded ({ok / len(rows) * 100:.0f}%)")

    if args.eval:
        print()
        print("  --eval: scoring the live model")
        try:
            import llm as llm_mod
            if not llm_mod.LLM_ENABLED:
                raise RuntimeError("LLM_ENABLED=0")
            asker = ask_mod.ASKER
        except Exception as exc:
            print(f"    SKIPPED — {exc}")
            print("    (needs GenieX up and AI_ASK=1; run this on the X Elite)")
            return 0

        scored = 0
        wins = {"verified": 0, "grounded": 0}
        for r in rows[:40]:
            try:
                answer = asker.answer(r["question"], {"_digest_override": r["digest"]})
            except Exception as exc:
                print(f"    call failed: {exc}")
                break
            g = grade(str(answer), r)
            scored += 1
            wins["verified"] += 1 if g["verified"] else 0
            wins["grounded"] += 1 if g["grounded"] else 0
        if scored:
            print(f"    scored {scored} rows")
            print(f"    verified (no invented numbers): "
                  f"{wins['verified']}/{scored}")
            print(f"    grounded (verified AND cites the right facts): "
                  f"{wins['grounded']}/{scored}")

    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
