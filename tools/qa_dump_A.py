"""QA step B: dump every A-grade record (bean / mechanism / flavor / source) for
adversarial review of the corpus/raw/ A-anchors.
    python tools/qa_dump_A.py
"""
import json, glob, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cie.schema import Record

DIR = os.path.join(ROOT, "corpus", "raw")
files = sorted(f for f in glob.glob(os.path.join(DIR, "*.jsonl"))
               if not os.path.basename(f).startswith("_"))
recs = []
for f in files:
    with open(f, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                continue
            d = json.loads(s)
            recs.append((os.path.basename(f), i, d, Record.model_validate(d)))

for fn, i, d, r in recs:
    if r.grade.value != "A":
        continue
    f = r.flavor
    fl = f"ac{f.acidity}/{f.acidity_type.value} sw{f.sweetness} bi{f.bitterness} bo{f.body} af{f.aftertaste} ba{f.balance} cl{f.clarity}"
    print(f"[{fn} L{i}] conf={r.confidence} protocol={r.protocol!r}")
    print(f"    bean: {r.bean.origin!r} {r.bean.variety!r} {r.bean.process.value} agtron={r.bean.roast_agtron}")
    print(f"    mech={r.params.brew_mechanism.value} method={r.params.method!r}")
    print(f"    flavor: {fl}")
    print(f"    notes: {f.flavor_notes}")
    print(f"    source: {r.source!r}")
    print()
