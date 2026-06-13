"""QA step A: validate every raw scope jsonl line + inventory, sweep invariants,
print the pre-dedup grade x mechanism distribution. Run before/after editing corpus/raw/.
    python tools/qa_validate.py
"""
import json, glob, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cie.schema import Record

DIR = os.path.join(ROOT, "corpus", "raw")
files = sorted(f for f in glob.glob(os.path.join(DIR, "*.jsonl"))
               if not os.path.basename(f).startswith("_"))

bad = []
recs = []  # (file, lineno, raw_dict, Record)
for f in files:
    with open(f, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                bad.append((f, i, "EMPTY_LINE", ""))
                continue
            try:
                d = json.loads(s)
            except Exception as e:
                bad.append((f, i, "JSON_ERR", repr(e)[:160]))
                continue
            try:
                r = Record.model_validate(d)
            except Exception as e:
                bad.append((f, i, "SCHEMA_ERR", repr(e)[:200]))
                continue
            recs.append((os.path.basename(f), i, d, r))

print("=" * 70)
print("FILES:", len(files))
for f in files:
    n = sum(1 for fn, _, _, _ in recs if fn == os.path.basename(f))
    print(f"  {os.path.basename(f):40s} {n:3d} valid")
print("TOTAL valid records:", len(recs))
print("BAD lines:", len(bad))
for b in bad:
    print("  BAD", os.path.basename(b[0]), "line", b[1], b[2], b[3])

# invariants sweep
print("=" * 70)
print("INVARIANT VIOLATIONS:")
viol = 0
for fn, i, d, r in recs:
    msgs = []
    if r.user_id != "global":
        msgs.append(f"user_id={r.user_id}")
    if r.grade.value == "prediction":
        msgs.append("grade=prediction")
    if r.grade.value == "A" and not r.protocol:
        msgs.append("A-without-protocol")
    # water->flavor causality can't be auto-detected; check water all-null sanity not required
    if "id" in d:
        msgs.append("has-id")
    if "timestamp" in d:
        msgs.append("has-timestamp")
    if msgs:
        viol += 1
        print(f"  {fn} L{i}: {', '.join(msgs)}")
print("  (none)" if viol == 0 else f"  total {viol}")

# grade x mechanism distribution
print("=" * 70)
print("GRADE x MECHANISM (pre-dedup):")
from collections import Counter
gm = Counter((r.grade.value, r.params.brew_mechanism.value) for _, _, _, r in recs)
mechs = ["immersion", "percolation", "pressure"]
print(f"  {'grade':6s} " + " ".join(f"{m:12s}" for m in mechs) + "  total")
for g in ["A", "B", "C"]:
    row = [gm.get((g, m), 0) for m in mechs]
    print(f"  {g:6s} " + " ".join(f"{v:12d}" for v in row) + f"  {sum(row):5d}")
tot = [sum(gm.get((g, m), 0) for g in ['A','B','C']) for m in mechs]
print(f"  {'TOT':6s} " + " ".join(f"{v:12d}" for v in tot) + f"  {sum(tot):5d}")
