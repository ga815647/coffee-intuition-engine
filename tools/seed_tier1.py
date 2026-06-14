"""Tier-1 常見豆「分層詳盡覆蓋」缺口填補(§4)。

對常見產地 × 處理法 × 機制的**空格**(corpus 內 0 筆)補上**單元級(cell-level)
社群風味傾向**記錄,讓「這支豆喝起來怎樣」不再一律硬湊物理先驗(降 §16.4 硬湊率)。

誠實分級(§4.3 / 鐵則):
  - 這些是**一般公認的產地×處理法風味原型**(general / community open-loop tendency),
    非某一支經量測的批次,故一律 **grade=C**(社群傾向、只壓量級、不定義方向、永不當 holdout)。
  - **不抄任何烘豆商的專有杯測詞**(§4.4):flavor_notes 用通用描述語,source 誠實標為
    社群原型推導、**非偽造特定 URL**。A 級永遠保留給 owner 閉環真值。
  - `variety=""`:單元級(整個 origin×process 格),非特定品種;對給品種的查詢仍同豆(寬鬆放行)。

只補**缺單元錨點**的格:某 (origin_token, process, mechanism) 在 corpus 已有 `variety=""`
單元錨點就跳過。看「無 variety="" 錨點」而非「格全空」,是因為一格內若全是**特定品種**的批次,
彼此被同豆閘判為非同豆(§3.2)、查詢/留出豆在該格仍硬湊物理先驗——`variety=""` 錨點才真正鋪平
該格(同時填全空格 **與** 品種破碎格)。C 級低權重(0.1),既有 A/B 真值主導不被稀釋;冪等:
重跑對同 (origin,process,mechanism) 錨點不重覆寫。

用法:
    python -m tools.seed_tier1 --dry-run      # 預覽缺口與將補的格(不寫檔)
    python -m tools.seed_tier1 --apply        # 追加到 corpus/global.jsonl(預設)
    python -m tools.seed_tier1 --apply --out corpus/global.jsonl
之後照常 `python -m cie.bootstrap && python -m cie.rebuild`(離線需先載 .env;見 CLAUDE.md)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus" / "global.jsonl"

AXES = ("acidity", "sweetness", "bitterness", "body", "aftertaste", "balance", "clarity")
_STOP = {"blend", "single", "origin", "coffee", "beans", "bean"}


def origin_token(o: str) -> str:
    for t in (o or "").lower().replace(",", " ").split():
        if t not in _STOP:
            return t
    return ""


# ── 產地原型(washed 參考軸 0–10 + 通用描述 + 酸質)──────────────────────────────
# 來源:一般公認產地特徵(教科書 / 社群共識),非特定批次量測;故 grade=C。
ARCHE: Dict[str, dict] = {
    "Ethiopia":    dict(ax=(8.0, 7.0, 3.0, 4.5, 6.5, 7.0, 8.0), at="citric",
                        notes=["floral", "citrus", "bergamot", "tea-like"]),
    "Kenya":       dict(ax=(8.5, 7.0, 3.5, 6.0, 7.0, 7.0, 7.5), at="citric",
                        notes=["blackcurrant", "tomato", "grapefruit", "juicy"]),
    "Colombia":    dict(ax=(7.0, 7.0, 3.5, 6.0, 6.5, 7.5, 7.0), at="malic",
                        notes=["caramel", "red apple", "citrus"]),
    "Brazil":      dict(ax=(4.5, 7.0, 4.5, 7.0, 6.0, 7.0, 5.5), at="mixed",
                        notes=["nutty", "chocolate", "peanut"]),
    "Costa Rica":  dict(ax=(7.5, 7.0, 3.5, 5.5, 6.5, 7.5, 7.5), at="citric",
                        notes=["citrus", "honey", "clean"]),
    "Guatemala":   dict(ax=(7.0, 7.0, 4.0, 6.5, 6.5, 7.5, 6.5), at="malic",
                        notes=["chocolate", "orange", "spice"]),
    "Panama":      dict(ax=(7.5, 7.5, 3.0, 4.5, 7.0, 7.5, 8.0), at="citric",
                        notes=["jasmine", "bergamot", "tropical", "tea-like"]),
    "El Salvador": dict(ax=(6.5, 7.5, 3.5, 6.0, 6.5, 7.5, 7.0), at="malic",
                        notes=["caramel", "stone fruit", "creamy"]),
    "Honduras":    dict(ax=(6.5, 7.0, 4.0, 6.0, 6.0, 7.0, 6.5), at="malic",
                        notes=["caramel", "mild fruit", "chocolate"]),
    "Nicaragua":   dict(ax=(6.5, 7.0, 4.0, 6.0, 6.0, 7.0, 6.5), at="malic",
                        notes=["nutty", "caramel", "mild fruit"]),
    "Mexico":      dict(ax=(6.0, 6.5, 4.0, 5.5, 6.0, 7.0, 6.5), at="mixed",
                        notes=["nutty", "chocolate", "mild citrus"]),
    "Peru":        dict(ax=(6.0, 6.5, 4.0, 5.5, 6.0, 7.0, 6.5), at="mixed",
                        notes=["nutty", "chocolate", "mild fruit"]),
    "Rwanda":      dict(ax=(7.5, 7.0, 3.5, 5.5, 6.5, 7.5, 7.5), at="malic",
                        notes=["red fruit", "floral", "citrus", "clean"]),
    "Burundi":     dict(ax=(7.5, 7.0, 3.5, 5.5, 6.5, 7.5, 7.0), at="malic",
                        notes=["red fruit", "juicy", "citrus"]),
    "Tanzania":    dict(ax=(7.5, 7.0, 3.5, 6.0, 6.5, 7.0, 7.0), at="citric",
                        notes=["blackcurrant", "citrus", "bright"]),
    "Yemen":       dict(ax=(6.5, 7.0, 4.5, 7.0, 7.0, 7.0, 5.5), at="mixed",
                        notes=["winey", "spice", "dried fruit", "chocolate"]),
    "Indonesia":   dict(ax=(4.0, 6.0, 5.5, 8.0, 6.5, 6.5, 4.5), at="mixed",
                        notes=["earthy", "herbal", "cedar", "dark chocolate"]),
}

# ── 處理法 delta(加到 washed 參考後 clamp)+ 額外描述 + 酸質覆寫 ──────────────────
PROC_DELTA: Dict[str, dict] = {
    "washed":    dict(d=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), at=None,
                      notes=[], desc="clean washed"),
    "natural":   dict(d=(-1.0, 1.0, 0.0, 1.0, 0.5, -0.3, -1.5), at="mixed",
                      notes=["berry", "fruity", "winey", "jammy"], desc="fruity natural"),
    "honey":     dict(d=(-0.3, 1.0, 0.0, 0.7, 0.2, 0.2, -0.5), at="mixed",
                      notes=["honey", "caramel", "stone fruit"], desc="sweet honey"),
    "anaerobic": dict(d=(-0.5, 1.2, 0.0, 0.8, 0.5, -0.5, -1.5), at="mixed",
                      notes=["fermented", "boozy", "tropical", "funky"], desc="intense anaerobic"),
}

# ── 各產地實際會出現的處理法(避免造不存在的組合;尊重現實 §4.4)──────────────────
ORIGIN_PROCS: Dict[str, List[str]] = {
    "Ethiopia": ["washed", "natural", "honey", "anaerobic"],
    "Kenya": ["washed", "natural"],
    "Colombia": ["washed", "natural", "honey", "anaerobic"],
    "Brazil": ["washed", "natural", "honey"],
    "Costa Rica": ["washed", "natural", "honey", "anaerobic"],
    "Guatemala": ["washed", "natural", "honey"],
    "Panama": ["washed", "natural", "honey"],
    "El Salvador": ["washed", "natural", "honey"],
    "Honduras": ["washed", "natural", "honey"],
    "Nicaragua": ["washed", "natural", "honey"],
    "Mexico": ["washed", "natural", "honey"],
    "Peru": ["washed", "natural", "honey"],
    "Rwanda": ["washed", "natural"],
    "Burundi": ["washed", "natural"],
    "Tanzania": ["washed", "natural"],
    "Yemen": ["natural"],          # 葉門幾乎全日曬;washed 罕見故不造
    "Indonesia": ["natural", "washed"],
}

# 預設機制:濾泡兩軌(滴濾/浸泡);加壓只給少數義式經典格,避免大量低訊號 espresso 單品格。
DEFAULT_MECHS = ["percolation", "immersion"]
PRESSURE_CELLS = {  # (origin_token, process) 才補 pressure
    ("kenya", "natural"), ("ethiopia", "natural"), ("ethiopia", "washed"),
    ("brazil", "natural"), ("colombia", "washed"),
}

# 各機制典型參數(取 corpus 中位數帶;僅標籤,推理走物理軸)。
MECH_PARAMS: Dict[str, dict] = {
    "percolation": dict(method="V60", water_temp_c=93.0, brew_ratio=16.0, grind_um=700.0,
                        contact_time_s=180.0, agitation_level=2, roast=74.0),
    "immersion":   dict(method="French Press", water_temp_c=94.0, brew_ratio=15.0, grind_um=900.0,
                        contact_time_s=240.0, agitation_level=1, roast=74.0),
    "pressure":    dict(method="Espresso", water_temp_c=93.0, brew_ratio=2.2, grind_um=300.0,
                        contact_time_s=28.0, pressure_bar=9.0, roast=70.0),
}


def _clamp(v: float) -> float:
    return round(min(10.0, max(0.0, v)), 1)


def _flavor(origin: str, process: str) -> Tuple[dict, List[str], str]:
    a = ARCHE[origin]
    pd = PROC_DELTA[process]
    ax = {name: _clamp(base + d) for name, base, d in zip(AXES, a["ax"], pd["d"])}
    notes: List[str] = []
    for n in list(a["notes"]) + list(pd["notes"]):
        if n not in notes:
            notes.append(n)
    acidity_type = pd["at"] or a["at"]
    return ax, notes[:5], acidity_type


def cell_signature(origin_tok: str, process: str, mechanism: str) -> Tuple[str, str, str]:
    return (origin_tok, process, mechanism)


def anchored_cells(corpus_rows: List[dict]) -> set:
    """已有**單元錨點**(variety 未指定的記錄)的 (origin_token, process, mechanism) 格。

    為何看「variety 未指定」而非「格非空」:同豆閘對**兩方都具體且不同**的 variety 判為**非同豆**
    (§3.2)。故一格內若全是特定品種的批次,彼此**不互為同豆**——查詢/留出豆在該格仍無同豆鄰居、
    硬湊物理先驗(這正是硬湊率的主因)。`variety=""` 的單元錨點對全品種寬鬆放行,才真正鋪平該格。
    因此:格內已有 variety="" 錨點 → 跳過;否則(全空 **或** 全是特定品種)→ 補錨點。
    """
    out = set()
    for r in corpus_rows:
        b, p = r.get("bean", {}), r.get("params", {})
        ot = origin_token(b.get("origin", ""))
        if not ot:
            continue
        if (b.get("variety") or "").strip():
            continue  # 特定品種的批次不算單元錨點
        out.add((ot, (b.get("process") or ""), p.get("brew_mechanism")))
    return out


def _load_corpus(path: Path = CORPUS) -> List[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_records(corpus_rows: List[dict] | None = None) -> List[dict]:
    """補缺單元錨點的格;`corpus_rows=None` 時讀 live 語料,測試可注入受控語料(hermetic)。"""
    have_anchor = anchored_cells(_load_corpus() if corpus_rows is None else corpus_rows)
    recs: List[dict] = []
    for origin, procs in ORIGIN_PROCS.items():
        ot = origin_token(origin)
        for process in procs:
            mechs = list(DEFAULT_MECHS)
            if (ot, process) in PRESSURE_CELLS:
                mechs.append("pressure")
            for mech in mechs:
                if (ot, process, mech) in have_anchor:
                    continue  # 該格已有單元錨點(variety="")→ 不重複
                recs.append(_make(origin, process, mech))
    return recs


def _make(origin: str, process: str, mechanism: str) -> dict:
    ax, notes, acidity_type = _flavor(origin, process)
    mp = MECH_PARAMS[mechanism]
    roast = mp["roast"]
    band = "light" if roast >= 70 else "medium"
    params = {
        "brew_mechanism": mechanism, "method": mp["method"],
        "water_temp_c": mp["water_temp_c"], "brew_ratio": mp["brew_ratio"],
        "grind_um": mp["grind_um"], "contact_time_s": mp["contact_time_s"],
    }
    if "agitation_level" in mp:
        params["agitation_level"] = mp["agitation_level"]
    if "pressure_bar" in mp:
        params["pressure_bar"] = mp["pressure_bar"]
    flavor = {"acidity": ax["acidity"], "acidity_type": acidity_type,
              "sweetness": ax["sweetness"], "bitterness": ax["bitterness"],
              "body": ax["body"], "aftertaste": ax["aftertaste"],
              "balance": ax["balance"], "clarity": ax["clarity"],
              "flavor_notes": notes}
    emb = (f"{band} roast {process} {origin} {mechanism} {mp['method']} "
           f"{acidity_type} {' '.join(notes)} community origin-process flavor archetype")
    return {
        "bean": {"origin": origin, "variety": "", "process": process, "roast_agtron": roast},
        "params": params,
        "flavor": flavor,
        "grade": "C",
        "source": "tier1:community origin×process flavor archetype (general knowledge, derived; not a roaster's proprietary notes)",
        "confidence": 0.35,
        "user_id": "global",
        "embedding_text": emb,
    }


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Tier-1 缺口填補(空格→C 級單元傾向)")
    ap.add_argument("--apply", action="store_true", help="追加到語料檔(預設 corpus/global.jsonl)")
    ap.add_argument("--dry-run", action="store_true", help="只預覽,不寫檔(預設行為)")
    ap.add_argument("--out", type=Path, default=CORPUS, help="輸出語料檔(追加)")
    args = ap.parse_args()

    recs = build_records()
    by_mech: Dict[str, int] = {}
    for r in recs:
        m = r["params"]["brew_mechanism"]
        by_mech[m] = by_mech.get(m, 0) + 1
    print(f"將補空格 = {len(recs)} 筆(全 grade=C 單元傾向)")
    print("  分機制:", by_mech)
    for r in recs:
        b = r["bean"]
        print(f"  + {b['origin']:13} {b['process']:10} {r['params']['brew_mechanism']:12} "
              f"acidity={r['flavor']['acidity']} notes={r['flavor']['flavor_notes']}")

    if args.apply:
        # 冪等:跳過 out 內已有 variety="" 單元錨點的相同 (origin_token, process, mechanism) 格。
        out = args.out
        existing = anchored_cells(_load_corpus(out)) if out.exists() else set()
        fresh = [r for r in recs
                 if (origin_token(r["bean"]["origin"]), r["bean"]["process"],
                     r["params"]["brew_mechanism"]) not in existing]
        with out.open("a", encoding="utf-8") as f:
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n已追加 {len(fresh)} 筆到 {out}(冪等跳過 {len(recs) - len(fresh)} 已存在格)")
    else:
        print("\n(dry-run:未寫檔。加 --apply 追加到語料檔)")


if __name__ == "__main__":
    main()
