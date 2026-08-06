# The learned tier and the session dataset

**Branch `ml-dataset-and-models`.** What it adds, what it measures, and what it
does not claim. Read this before reviewing the diff — several of the decisions
below look conservative until you know what they are protecting against.

Companion to `07_QUAD_SESSION_LOG.md` §19–§21, which is the blow-by-blow. This
file is the reviewable summary.

---

## Why

Two gaps, one product and one structural.

**Nothing persisted.** `power_history` was a 60-entry deque — five minutes — and
every other piece of state died with the process. The system could not learn from
its own operation, and there was no record of what it decided or why.

**Every rule was a post-mortem.** R1–R6 report money already spent. Advice that
arrives after the money is gone is a receipt, not a recommendation.

---

## The architectural claim

Adding a model must not weaken the safety story. So each kind of intelligence has
a **bounded mandate**, and none may override the one above it:

| Tier | Decides | May not |
|---|---|---|
| Deterministic rules + R7 | what is **legal / safe** | be overridden by anything |
| Learned model | what is **likely / worth showing** | change a number, or unlock a refused action |
| LLM (Hexagon NPU) | **wording** | compute, or invent a figure |

That table is the point of the branch. Everything below is an instance of it.

---

## What was added

### 1. Session recorder — `hub/recorder.py`

One JSONL row per evaluation tick, plus a row for every finding, approval,
guardrail refusal, guardrail **veto**, hardware confirmation and dashboard thumb.
Wired into `server.py` at six points.

- Swallows its own exceptions and disables itself after five consecutive write
  failures. **A dataset is worth less than a working demo.**
- Rows carry the `*_src` stamps and `metered` flags plus a computed provenance
  summary, so a model trained on them can never be described as having learned
  from a real household when it did not.
- `code/data/sessions/` is **gitignored**. The project claims occupancy data
  never leaves the house; that has to be true of the repo too.

**Labels come from the human:** approval = positive, guardrail refusal = hard
negative, thumb = explicit, untouched card = *weak* negative at weight 0.25 and
marked as such — "ignored" might only mean nobody was looking.

A **veto is not a refusal.** A refusal is a human asking and being told no — a
real preference signal. A veto is advice nobody was ever shown, and scoring it as
a negative would teach a model that people dislike recommendations they never
saw. Separate row types, and `build_dataset.py` reads vetoes as audit evidence
only.

### 2. Learned occupancy — `hub/occupancy_model.py`

There is no occupancy sensor; occupancy was a toggle. This infers it from
temperature, humidity and lux, trained on the UCI Occupancy Detection benchmark
(dataset 357, Candanedo & Feldheim 2016).

**Runs in shadow mode by default** (`OCCUPANCY_MODEL=1`): it predicts, the
dashboard shows its call beside the reported value, and no rule reads it — so a
wrong prediction cannot change a recommendation or an actuation. Mode 2 fills a
gap only when no other source supplied occupancy; it never overwrites a reported
value.

### 3. Declined decisions — `rules.evaluate_all()`

`r7_comfort_guardrail()` used to `continue` past a vetoed finding, throwing away
the most interesting thing the system does. Vetoed findings are now tagged rather
than dropped, fully computed and fully costed, and the dashboard renders them
greyed beside the live ones:

```
Considered 2 · recommending 1 · declined 1 on safety grounds

⃠ DECLINED   comfort_guardrail
  A/C cooling an empty home  ($0.029 withheld)
  living is 29.5 C, above the 27 C comfort limit
  ↳ comfort protected here — the saving is taken on lights instead
```

That last line is the system resolving a **conflict between two loads**: both are
wasting money, and it declines the one that would cost comfort while still taking
the one that would not. `evaluate()` keeps its exact old contract, so no existing
caller changed and nothing downstream can accidentally offer a suppressed
finding.

### 4. R8 `peak_window_imminent` — anticipation

The mirror of R6 moved 30 minutes earlier. R6 says the dryer is running inside
the expensive window; R8 says it is *about to be*, while you can still stop it.
**Detected waste becomes avoided waste.**

Nothing is predicted — the tariff calendar is published and fixed, so the claim
is "the rate changes at 16:00 and this load is running". Its figure *is* a
projection (remaining cycle time is unknowable), bounded at one hour and labelled
in the formula:

```
3000 W x 3600 s projected = 3.0000 kWh; rate delta $0.58 - $0.32 = $0.26/kWh;
3.0000 kWh x $0.26 = $0.780 AVOIDABLE if shifted (projected, not yet incurred)
```

`kind` = `detected` | `anticipated` flows through both narration paths; the LLM
prompt switches to future tense for anticipated findings; `realized_totals()`
splits out `avoided_usd` so a projection is never counted as a measurement.

**Anticipated cards expire.** *"You can still shift it"* is useful at 15:48 and
false at 17:30, so the card disappears once its rule stops firing and R6 takes
over.

`POST /api/clock` was added so this is demoable without waiting for 3:30 PM —
same reasoning as `/api/sensor`: the no-broker path has to exercise the whole
engine.

### 5. Synthetic corpus + relevance model

Labels are the scarce resource: ticks accrue for free, but an approval only
exists when a human clicks, so an unattended overnight run yields thousands of
rows and no training signal.

`tools/generate_sessions.py` **invents inputs, never outputs.** Occupancy,
presence, appliance use, the daylight curve and the temperature cycle are
fabricated; every situation is then fed through the real `rules.evaluate_all()`
and the real `template_narrate()`, so each row's rule, cost, formula and evidence
are exactly what the live system would have produced.

The simulated occupant has **preferences the rules do not encode** — acts on
HVAC, largely ignores lighting while home, never acts 23:00–07:00, ignores
phantom standby, responds to size, noisy about one decision in eight. Labels
generated by the rules would teach a model to re-derive R1–R8: it would score
well and know nothing.

---

## Results

**Occupancy** (UCI 357, two official held-out splits):

| Variant | Features | datatest | datatest2 |
|---|---|---|---|
| `full` | temp + humidity + lux | 0.9786 | 0.9884 |
| `no_light` | temp + humidity | 0.8608 | 0.8452 |

**Relevance** (90 simulated days, 446 rows, 138 positives, chronological split):

| | acc | precision | recall | F1 | AUC |
|---|---|---|---|---|---|
| majority | 0.679 | 0.000 | 0.000 | 0.000 | 0.500 |
| **by-cost** | 0.709 | 1.000 | 0.093 | 0.170 | **0.946** |
| learned model | **0.813** | 0.636 | 0.977 | **0.771** | 0.945 |

It recovered every hidden preference (`presence_away +1.03`, `occupancy −1.03`,
`usd +1.23`, `phantom_standby −0.54`, `hvac_with_window_open +0.38`,
`peak_window_imminent +0.53`).

**But the result is a draw on ranking, and that is the finding.** The dollar
figure the rules already compute is an excellent ordering; the model does not
beat it. What it adds is a usable *operating point* — cost alone cannot be
thresholded without losing 91% of the positives. `train_relevance.py` prints that
comparison every run, flattering or not, and refuses to train below 30 positives.

> A model that cannot beat the arithmetic already in the repo should not be
> described as an improvement to it.

---

## Honest limitations

- **The occupancy benchmark is out of domain here.** UCI is an office in Belgium
  in February: 19–23.2 °C, 16.8–39.1 % RH. This demo drives 16–32 °C and
  0–100 % RH, so many demo states are outside anything the model has seen.
  `predict()` returns `in_domain: False` and the dashboard says *extrapolating*.
  **Do not quote 97.9 % while standing in a 29.5 °C simulated room.**
- **Lux dominates the `full` variant** (standardised weights: lux +3.59, humidity
  +0.76, temp −0.25). R3 already thresholds on lux, so inferred occupancy and R3
  are **not independent evidence**. The `no_light` variant exists for exactly
  that, and the weights print at training time rather than being buried.
- **The relevance model learned a simulated occupant**, not a real person. Both
  artifacts record what they were trained on, the sample count, and their
  caveats.
- **R8's figure is a projection**, not observed waste, and is labelled
  `anticipated` everywhere it appears.

## No new dependencies

Both models are plain-Python logistic regression trained by full-batch gradient
descent. **No numpy, no sklearn, no ML runtime.** Inference is a dot product over
two or three floats, so it adds nothing to install on a Windows-on-ARM machine —
this project has already lost time to wheels that do not exist for `win_arm64` —
and the same artifact would run unchanged on the UNO Q's A53. The weights are
small enough to put on a slide.

---

## How to run it

```bash
# occupancy model: fetch UCI 357, train both variants, export the artifact
python tools/train_occupancy.py
python hub/occupancy_model.py          # replays the held-out split through inference

# corpus -> dataset -> relevance model
python tools/generate_sessions.py --days 90 --seed 11
python tools/build_dataset.py
python tools/train_relevance.py

# demo R8 without waiting for 3:30 PM
curl -X POST localhost:8000/api/clock -H "Content-Type: application/json" \
  -d '{"time":"15:48"}'                # or {"offset_s": 3600} / {"reset": true}
```

`smoke_test.py` is now **44/44** (was 32/32): twelve new checks covering the
suppressed list, that a vetoed finding keeps its cost and reason, that
`evaluate()` is unchanged, R8's lead window and rate-delta arithmetic, R6 taking
over inside the window, the recorder, and the feedback endpoint.

---

## Not verified

- **No dashboard change in this branch has ever been rendered in a browser.**
  Declined cards, anticipated cards, the 👍/👎 buttons, the dataset tile and the
  shadow line are all correct at the data layer and none has been looked at.
  **Check this before demoing.**
- `code/simulator/index.html` is deliberately **untouched** — it was being edited
  in parallel — so it does not yet render declined decisions, anticipated cards
  or the thumbs.

## Still open

- **The benchmark table is stale.** Its rules-engine row was measured with seven
  rules; there are now eight. `benchmark.py`'s label says eight. Re-run
  `python hub/benchmark.py --markdown` on the demo machine and paste the new
  table rather than relabelling a number nobody re-measured.
- **Metered watts still do not reach the cost arithmetic.** `rules._attach()`
  costs every finding from the static `LOADS` table, so a bulb metered at 1.7 W
  is billed as a 240 W incandescent set — while `code/README.md` claims "the
  savings arithmetic runs on measured power". Pre-existing, not introduced here,
  and the one claim in the repo a judge could puncture.
- Everything in `07_QUAD_SESSION_LOG.md` §18.6.
