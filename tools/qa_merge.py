"""QA step C: apply adversarial downgrades, dedup, merge raw scope files -> the
curated global corpus, and print the grade x mechanism distribution.

Reproducible pipeline: reads the tracked per-source provenance in corpus/raw/*.jsonl
and (re)generates corpus/global.jsonl deterministically. Re-run after editing any
raw scope file or the DOWNGRADE / FIELD_FIXES remediation tables below.
    python tools/qa_merge.py
"""
import json, glob, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cie.schema import Record
from collections import Counter

DIR = os.path.join(ROOT, "corpus", "raw")          # inputs: tracked raw scope files (provenance)
OUT = os.path.join(ROOT, "corpus", "global.jsonl")  # output: curated global corpus

# Adversarial-review downgrades A->B: (file, lineno) -> reason
DOWNGRADE = {
    ("brazil-natural.jsonl", 10): "generalized CoE composite, not this lot's own score",
    ("espresso-classic.jsonl", 11): "source is prose, no citation/link",
    ("espresso-classic.jsonl", 12): "generic competition blend, no citation",
    ("ethiopia-natural.jsonl", 7): "roastcoffee.ai blog, not a real CoE scoresheet",
    ("hoffmann-v60.jsonl", 1): "empty bean, method-character flavor numbers invented",
    ("hoffmann-v60.jsonl", 2): "empty bean, method-character flavor numbers invented",
    ("hoffmann-v60.jsonl", 3): "empty bean, method-character flavor numbers invented",
    ("hoffmann-v60.jsonl", 6): "empty bean, method-character flavor numbers invented",
    ("hoffmann-v60.jsonl", 7): "empty bean, method-character flavor numbers invented",
    ("tetsu-46.jsonl", 2): "bean-less method template variation",
    ("tetsu-46.jsonl", 3): "bean-less method template variation",
    ("tetsu-46.jsonl", 4): "bean-less method template variation",
    ("tetsu-46.jsonl", 5): "bean-less method template variation",
    ("tetsu-46.jsonl", 6): "bean-less method template variation",
    ("tetsu-46.jsonl", 7): "bean-less method template variation",
    ("immersion-frenchpress-clever.jsonl", 1): "SCA-cupping 'descriptor profile' stereotype, no measured lot",
    ("immersion-frenchpress-clever.jsonl", 2): "SCA-cupping 'descriptor profile' stereotype, no measured lot",
    ("immersion-frenchpress-clever.jsonl", 3): "SCA-cupping 'descriptor profile' stereotype, no measured lot",
    ("immersion-frenchpress-clever.jsonl", 4): "Hoffmann-FP + origin stereotype, no measured lot",
    ("immersion-frenchpress-clever.jsonl", 5): "Hoffmann-FP + origin stereotype, no measured lot",
    ("immersion-frenchpress-clever.jsonl", 12): "SCA-cupping 'descriptor profile' stereotype, no measured lot",
    # --- Round 2 (24-scope expansion) adversarial downgrades ---
    ("aeropress.jsonl", 1): "empty bean, method-character flavor invented (subagent self-flagged)",
    ("aeropress.jsonl", 2): "WAC champion but flavor is thin prose (sweet/juicy), vague 3-country blend; not a cupped value-anchor",
    ("anaerobic-experimental.jsonl", 2): "CoE 'added experimental category' announcement article, not this lot's scoresheet; generic Brazil Geisha",
    ("el-salvador-pacamara.jsonl", 4): "generic 'El Salvador' origin, COE honey-group composite, no single-lot score",
    ("el-salvador-pacamara.jsonl", 5): "COE 2022 Pacamara group description, generic flavor, no single-lot scoresheet",
    ("nordic-light-espresso.jsonl", 1): "filter green cup-score 88 applied to a pressure/espresso record (crosses mechanism); espresso flavor is roaster prose",
    ("panama-geisha.jsonl", 4): "Proud Mary V60 roaster brew guide mislabeled competition_recipe; no standardized score, roaster flavor prose",
    ("rwanda-burundi-washed.jsonl", 6): "COE score real but flavor is generic washed-Bourbon stereotype (identical to L7), not this lot's cupped notes",
    ("rwanda-burundi-washed.jsonl", 7): "COE score real but flavor is generic washed-Bourbon stereotype (identical to L6), not this lot's cupped notes",
    # --- P1 (pressure-A + percolation-A expansion) adversarial 2-lens verify downgrades ---
    # Refuted by the verify panel: claimed A but params (or flavor) not actually in the cited sources.
    ("wbc-signature-espresso.jsonl", 2): "Berg Wu 2016: TDS 9.25% + 19g/43g not published in cited sources; shot params invented",
    ("wbc-signature-espresso.jsonl", 4): "Anthony Douglas 2022: 19g/40g borrowed from a signature-drink template (different Ethiopian bean), not the cited Sidra espresso",
    ("wbrc-championship-pourover.jsonl", 1): "Domatiotis 2014: params absent from cited sources AND flavor fabricated (contradicts documented lychee profile)",
    ("wbrc-championship-pourover.jsonl", 3): "Du Jianing 2019: flavor confirmed but recipe params not in cited sources",
    ("wbrc-championship-pourover.jsonl", 4): "Matt Winton 2021: recipe explicitly undisclosed in sources; params invented (flavor confirmed)",
}

# ─────────────────────── Round 3 (bean-expansion) remediation layer ───────────────────────
# Applied AFTER round-1/2 DOWNGRADE. Sources: adversarial 2-lens verify panel + per-file honesty audit.
# Rationale lives in docs / the round-3 report; each line was cross-checked against the actual source.
R3_FILES = {
    "colombia-natural.jsonl", "costa-rica-washed.jsonl", "honduras-washed.jsonl",
    "peru-washed.jsonl", "mexico-washed.jsonl", "nicaragua-washed.jsonl",
    "tanzania-peaberry.jsonl", "uganda-washed-natural.jsonl",
    "india-monsooned-malabar.jsonl", "china-yunnan.jsonl",
    "hawaii-kona.jsonl", "jamaica-blue-mountain.jsonl",
}

AXES = ("acidity", "sweetness", "bitterness", "body", "aftertaste", "balance", "clarity")

# Coffee Review tier policy. CR = single-expert standardized blind 100pt cupping (NOT an SCA panel).
#   "A": literal rubric reading (closed-loop standardized protocol + genuinely cupped/described flavor)
#        + rounds-1/2 precedent; confidence capped at 0.80 (< competition-jury 0.85-0.90).
#   "B": stricter scarcity reading — demote every Coffee Review record to B (conf<=0.70), reserving
#        grade A for competition juries (COE/BoP) + genuine SCA panels + named-method-with-params.
# Flip this single value and re-run to switch the whole corpus' CR treatment.
CR_TIER = "B"

# Explicit per-line field remediations (non-Coffee-Review or special cases). CR records are handled
# generically by cr_normalize() below; only put a CR line here when it needs MORE than normalization.
FIELD_FIXES = {
    # Coffee Review ESPRESSO (pressure) mislabeled SCA_cupping grade A -> B. Pressure track keeps 0 A
    # (least-modeled, non-monotonic physics); CR-espresso is not a cupping value-anchor.
    ("china-yunnan.jsonl", 3): {"grade": "B", "protocol": "coffee_review_espresso", "confidence": 0.6},
    ("india-monsooned-malabar.jsonl", 4): {"grade": "B", "protocol": "coffee_review_espresso", "confidence": 0.6},
    # Verify panel A->B: Coffee Review review whose flavor drifted to a varietal stereotype (specific lot).
    ("honduras-washed.jsonl", 1): {"grade": "B", "protocol": "CoffeeReview_100pt", "confidence": 0.65},
    # Verify panel A->B: real COE score but flavor is variety-generic, not the lot's published cupping notes.
    ("costa-rica-washed.jsonl", 3): {"grade": "B", "confidence": 0.65},
    ("costa-rica-washed.jsonl", 4): {"grade": "B", "confidence": 0.65},
    # Verify panel A->B: "Best of Yunnan" cup score is green-seller-reported, not an independent scoresheet.
    ("china-yunnan.jsonl", 4): {"grade": "B", "confidence": 0.65},
    ("china-yunnan.jsonl", 5): {"grade": "B", "confidence": 0.65},
    # COE/score-only with NO published per-lot notes -> per-axis flavor was invented. Null flavor, drop to C
    # (magnitude-only; §五.2 + §四 C-grade: 風味數值保守、寧可 null). Score context survives in embedding_text.
    ("peru-washed.jsonl", 10): {"grade": "C", "confidence": 0.2, "acidity_type": "none", "null_axes": True, "clear_notes": True},
    ("mexico-washed.jsonl", 3): {"grade": "C", "confidence": 0.25, "acidity_type": "none", "null_axes": True},
    ("mexico-washed.jsonl", 4): {"grade": "C", "confidence": 0.25, "acidity_type": "none", "null_axes": True},
    # COE A kept (competition jury + plausibly published notes) but protocol year contradicts source URL
    # (claimed 2025, URL is 2022); fix year + cap conf to jury level.
    ("nicaragua-washed.jsonl", 1): {"protocol": "competition_score:COE_Nicaragua_2022_rank2_90.5", "confidence": 0.85},
    # Fabricated V60 temp/time/grind on a roaster product page (no recipe stated) -> null (§五.1).
    ("costa-rica-washed.jsonl", 8): {"null_params": ["water_temp_c", "contact_time_s", "grind_um"]},
    # Anaerobic naturals mislabeled plain "natural" -> split so ferment funk doesn't pollute the clean-natural prior.
    ("uganda-washed-natural.jsonl", 3): {"process": "anaerobic"},
    ("uganda-washed-natural.jsonl", 10): {"process": "anaerobic"},
}


def apply_fix(d, fix):
    """Apply an explicit FIELD_FIXES op-set to a raw record dict (before validation)."""
    if "grade" in fix:
        d["grade"] = fix["grade"]
    if "confidence" in fix:
        d["confidence"] = fix["confidence"]
    if "confidence_cap" in fix:
        d["confidence"] = min(float(d.get("confidence", 0.5)), fix["confidence_cap"])
    p = d.setdefault("params", {})
    if "brew_mechanism" in fix:
        p["brew_mechanism"] = fix["brew_mechanism"]
    if "method" in fix:
        p["method"] = fix["method"]
    for k in fix.get("null_params", []):
        if k in p:
            p[k] = None
    if "protocol" in fix:
        d["protocol"] = fix["protocol"]
    if "process" in fix:
        d.setdefault("bean", {})["process"] = fix["process"]
    fl = d.setdefault("flavor", {})
    if fix.get("null_axes"):
        for a in AXES:
            fl[a] = None
    if fix.get("clear_notes"):
        fl["flavor_notes"] = []
    if "acidity_type" in fix:
        fl["acidity_type"] = fix["acidity_type"]


def _competition(d):
    """Genuine competition jury (COE / Best of Panama / Brewers/Barista Cup / auction).
       Such a lot stays grade A even under CR_TIER=='B' — it is NOT single-grader cupping.
       (Bare 'coe' substring is avoided: too many false hits like 'coexist'.)"""
    blob = ((d.get("source") or "") + " " + (d.get("protocol") or "")).lower()
    return any(t in blob for t in (
        "cup of excellence", "cup-of-excellence", "competition", "auction",
        "best of panama", "best-of-panama", "world brewers", "world barista",
        "brewers cup", "barista championship"))


def cr_normalize(d):
    """Honest normalization for CUPPING-SCORE records, back-applied to ALL scope files (P0.2).
       A 'cupping score' = a Coffee Review review (single expert) OR a roaster/panel SCA_cupping
       claim. Coffee Review is NOT an SCA panel, so a CR record's protocol -> CoffeeReview_100pt.
         - detect:  CR  = coffeereview.com in source OR 'coffee_review'/'coffeereview' in protocol
                    SCA  = 'sca_cupping' in protocol (and not already CR)
                    genuine competition juries labeled SCA_cupping are excluded (kept grade A).
         - protocol: CR -> CoffeeReview_100pt; roaster-SCA keeps its SCA_cupping label.
         - tier: single-grader cupping (CR or roaster-SCA) is capped. CR_TIER=='B' demotes an
                 A to B (conf<=0.70); else conf<=0.80.
         - mechanism (鐵則1): cupping is immersion -> any percolation flips to immersion+cupping,
                 nulling now-meaningless percolation/pressure params (grind/contact_time/pressure).
       NOT touched: CR article/category pages at grade C (community lore citing a CR page, never a
       cupping score — they stay percolation-C, magnitude-only) and roaster_cupping V60 brews
       (real percolation with stated params; not a Coffee Review / SCA_cupping record).
       Returns True if it touched the record.
    """
    s = (d.get("source") or "").lower()
    p = (d.get("protocol") or "").lower()
    is_cr = ("coffeereview.com" in s) or ("coffee_review" in p) or ("coffeereview" in p)
    is_sca = ("sca_cupping" in p) and not is_cr
    if not (is_cr or is_sca):
        return False
    if d.get("grade") not in ("A", "B"):
        return False
    if is_sca and _competition(d):
        return False  # real competition jury (e.g. a COE lot tagged SCA_cupping) — leave grade/protocol
    touched = False
    # protocol: Coffee Review (single expert) -> CoffeeReview_100pt; roaster-SCA keeps SCA_cupping
    if is_cr and d.get("protocol") != "CoffeeReview_100pt":
        d["protocol"] = "CoffeeReview_100pt"
        touched = True
    # tier / confidence: single-grader cupping
    cur_conf = float(d.get("confidence", 0.5))
    if CR_TIER == "B" and d.get("grade") == "A":
        d["grade"] = "B"
        d["confidence"] = min(cur_conf, 0.70)
        touched = True
    else:
        nc = min(cur_conf, 0.80)
        if nc != cur_conf:
            touched = True
        d["confidence"] = nc
    # mechanism (鐵則1): a cupping is immersion physics, never percolation
    pm = d.setdefault("params", {})
    if pm.get("brew_mechanism") == "percolation":
        pm["brew_mechanism"] = "immersion"
        pm["method"] = "cupping"
        for k in ("grind_um", "contact_time_s", "pressure_bar"):
            if k in pm:
                pm[k] = None
        touched = True
    return touched

files = sorted(f for f in glob.glob(os.path.join(DIR, "*.jsonl"))
               if not os.path.basename(f).startswith("_"))

GRANK = {"A": 3, "B": 2, "C": 1, "prediction": 0}
items = []  # (dict, Record, file, line)
n_dg = n_fix = n_cr = 0
for f in files:
    base = os.path.basename(f)
    with open(f, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                continue
            d = json.loads(s)
            if (base, i) in DOWNGRADE:
                d["grade"] = "B"
                if float(d.get("confidence", 0.5)) > 0.7:
                    d["confidence"] = 0.65
                n_dg += 1
            elif (base, i) in FIELD_FIXES:
                apply_fix(d, FIELD_FIXES[(base, i)])
                n_fix += 1
            else:
                # P0.2: cr_normalize back-applied corpus-wide (was gated to R3_FILES).
                if cr_normalize(d):
                    n_cr += 1
            r = Record.model_validate(d)
            items.append((d, r, base, i))

print(f"Applied {n_dg} A->B downgrades (expected {len(DOWNGRADE)}), "
      f"{n_fix} explicit field-fixes (expected {len(FIELD_FIXES)}), "
      f"{n_cr} Coffee-Review normalizations [CR_TIER={CR_TIER}].")

# Dedup on normalized (origin, process, roast_band, method, brew_ratio, grind_um); keep higher grade then conf
def key(r):
    # Spec key (origin, process, roast_band, method, brew_ratio, grind_um) + a distinctness
    # guard: flavor-note signature. Two records collapse only when params AND flavor match,
    # so distinct lots sharing a coarse key (e.g. two scored Guatemala Huehuetenango cupping
    # lots with null brew params) are NOT merged — collapsing verified A anchors would break
    # the A-scarcity intent. True redundant twins (same params + same notes) still merge.
    return (
        (r.bean.origin or "").strip().lower(),
        r.bean.process.value,
        r.bean.roast_band(),
        (r.params.method or "").strip().lower(),
        r.params.brew_ratio,
        r.params.grind_um,
        tuple(sorted(n.strip().lower() for n in r.flavor.flavor_notes)),
    )

best = {}
dups = []
for d, r, base, i in items:
    k = key(r)
    cand = (GRANK[r.grade.value], r.confidence)
    if k not in best:
        best[k] = (cand, d, r, base, i)
    else:
        if cand > best[k][0]:
            dups.append((base, i, "replaced-by-higher"))
            best[k] = (cand, d, r, base, i)
        else:
            dups.append((base, i, f"dup-of {best[k][3]}:{best[k][4]}"))

survivors = [v for v in best.values()]
print(f"Pre-dedup: {len(items)}  | dups removed: {len(dups)}  | survivors: {len(survivors)}")
for b, i, why in dups:
    print(f"  DUP {b} L{i}: {why}")

# Write merged
with open(OUT, "w", encoding="utf-8") as out:
    for _, d, r, base, i in survivors:
        out.write(json.dumps(d, ensure_ascii=False) + "\n")
print("WROTE", OUT, "with", len(survivors), "records")

# Distribution table
print("=" * 70)
print("GRADE x MECHANISM (merged, post-dedup):")
gm = Counter((v[2].grade.value, v[2].params.brew_mechanism.value) for v in survivors)
mechs = ["immersion", "percolation", "pressure"]
print(f"  {'grade':6s} " + " ".join(f"{m:12s}" for m in mechs) + "  total")
for g in ["A", "B", "C"]:
    row = [gm.get((g, m), 0) for m in mechs]
    print(f"  {g:6s} " + " ".join(f"{v:12d}" for v in row) + f"  {sum(row):5d}")
tot = [sum(gm.get((g, m), 0) for g in ['A', 'B', 'C']) for m in mechs]
print(f"  {'TOT':6s} " + " ".join(f"{v:12d}" for v in tot) + f"  {sum(tot):5d}")
print("=" * 70)
print("A-grade survivors:")
for v in survivors:
    r = v[2]
    if r.grade.value == "A":
        print(f"  {v[3]} L{v[4]}: {r.bean.origin!r} {r.params.method!r} [{r.protocol}] {r.source[:60]}")
