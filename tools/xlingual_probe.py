"""跨語言檢索探針(P-headline 真瓶頸實測)。

問題:bge-m3 有沒有把中文風味描述詞(如『烏梅 紅酒 發酵感』)對齊到語料裡的
英文 notes(plum/wine/fermented)?這決定 descriptor→bean 的中文召回到底卡在
『嵌入空間沒橋接(要做多語詞表)』還是『只缺查詢入口(嵌入已通)』。

兩道獨立測試:
  A. 直接 zh↔en 風味詞 cosine(含負控)——模型有沒有在『詞』層對齊兩語言。
  B. 用中文 query 與其英文孿生 query 對真語料(corpus/global.jsonl)各召回 top-k,
     比 top-5 命中重疊——詞層對齊有沒有轉成『召回到同一批豆』。

唯讀:記憶體 store、不碰 D1 / 不寫 canonical / 不動 engine。
**硬 gate**:啟動即 assert 嵌入器是 workers_ai;.env / CF 金鑰沒載會靜默退回雜湊版
(LocalHashEmbedder 結構上不跨語言),那會得出假陰性,故缺金鑰直接中止。

跑法:python -m tools.xlingual_probe   (需 .env 含 CF 金鑰)
"""
from __future__ import annotations

import math
import os
import sys

# ── .env 必須在 import cie.config 之前載入(否則靜默走 LOCAL,見 cie-cli-needs-env-loaded)──
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Windows 終端預設 cp950,印 CJK / 箭頭會 UnicodeEncodeError;強制 UTF-8 輸出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

from cie.config import CONFIG  # noqa: E402
from cie.embedding import get_embedder  # noqa: E402
from cie.portability import read_jsonl  # noqa: E402
from cie.schema import BrewMechanism, Record  # noqa: E402
from eval.run import CORPUS_PATH, _isolated_memory_store  # noqa: E402


# ────────────────────────────── 探針資料 ──────────────────────────────

# A. 直接詞對(中文風味詞 → 預期對齊的英文 note)。最後兩組是負控(語意無關,應低分)。
WORD_PAIRS = [
    ("烏梅", "plum"), ("紅酒", "wine"), ("發酵", "fermented"), ("莓果", "berry"),
    ("茉莉花", "jasmine"), ("柑橘", "citrus"), ("檸檬", "lemon"), ("花香", "floral"),
    ("黑巧克力", "dark chocolate"), ("堅果", "nutty"), ("焦糖", "caramel"),
    ("蜂蜜", "honey"), ("水蜜桃", "peach"), ("杏桃", "apricot"),
    ("龍眼", "longan"), ("紅茶", "black tea"), ("乾淨", "clean"), ("平衡", "balanced"),
    # 負控:應該明顯低於上面的真對
    ("烏梅", "stainless steel wrench"), ("茉莉花", "diesel engine oil"),
]

# B. 檢索概念(中文 query, 英文孿生 query, 說明)。中文 query = 模擬使用者真的打的字。
CONCEPTS = [
    ("烏梅 紅酒 發酵感 莓果", "plum wine fermented winey berry",
     "厭氧/日曬 winey(headline 例子)"),
    ("茉莉花 佛手柑 檸檬 明亮酸", "jasmine bergamot lemon bright acidity floral",
     "水洗衣索比亞 花香柑橘明亮"),
    ("黑巧克力 烤堅果 焦糖 醇厚", "dark chocolate roasted nuts caramel heavy body",
     "深焙 巧克力堅果厚體"),
    ("蜂蜜 水蜜桃 杏桃 甜感", "honey peach apricot sweetness",
     "蜜處理 核果甜"),
    ("龍眼 紅茶 黑糖", "longan black tea brown sugar",
     "台灣慣用詞 longan/black-tea"),
    ("乾淨 平衡 柔順 圓潤", "clean balanced smooth round",
     "通用平衡乾淨"),
]

TOP_K = 10
OVERLAP_AT = 5


# ────────────────────────────── 小工具 ──────────────────────────────

def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _hit_record(h) -> Record:
    return Record.model_validate_json(h["payload"]["_canonical"])


def _notes(r: Record) -> str:
    return ",".join(r.flavor.flavor_notes) if r.flavor.flavor_notes else "—"


def _label(r: Record) -> str:
    org = (r.bean.origin or "?").strip() or "?"
    var = (r.bean.variety or "").strip()
    return f"{org}{('/' + var) if var else ''} [{r.bean.process.value}] {r.grade.value}"


# ────────────────────────────── 主程式 ──────────────────────────────

def main() -> int:
    embedder = get_embedder(CONFIG)
    # 硬 gate:缺金鑰會靜默退回 local-hash → 中文必然假陰性,直接中止。
    if not embedder.model_id.startswith("workers_ai:"):
        print(f"✗ 中止:需要 workers_ai 真嵌入,實得 {embedder.model_id!r}。"
              f"\n  八成 .env / CF 金鑰沒載(get_embedder 靜默退回雜湊版)。"
              f"\n  雜湊版結構上不跨語言,跑出來是假陰性。先確認 .env 有 CF 金鑰。",
              file=sys.stderr)
        return 2
    print(f"嵌入器:{embedder.model_id}")

    # ── A. 詞層 zh↔en cosine ──
    print("\n" + "=" * 64)
    print("A. 直接 zh↔en 風味詞 cosine(模型詞層對齊;末兩組=負控)")
    print("=" * 64)
    true_scores, neg_scores = [], []
    for i, (zh, en) in enumerate(WORD_PAIRS):
        c = cosine(embedder.embed(zh), embedder.embed(en))
        is_neg = i >= len(WORD_PAIRS) - 2
        (neg_scores if is_neg else true_scores).append(c)
        tag = "  (負控)" if is_neg else ""
        print(f"  cos('{zh}', '{en}') = {c:+.3f}{tag}")
    mean_true = sum(true_scores) / len(true_scores)
    mean_neg = sum(neg_scores) / len(neg_scores)
    print(f"\n  真對平均 = {mean_true:+.3f}   負控平均 = {mean_neg:+.3f}   "
          f"分離 = {mean_true - mean_neg:+.3f}")
    print("  讀法:真對平均明顯高於負控 → bge-m3 在詞層橋接 CN→EN。")

    # ── 建記憶體庫(workers_ai 嵌入)──
    store = _isolated_memory_store(CONFIG, embedder=embedder)
    corpus = read_jsonl(CORPUS_PATH)
    loaded = store.upsert_many(corpus)
    print("\n" + "=" * 64)
    print(f"B. 檢索重疊:中文 query vs 英文孿生 query(語料 {loaded}/{len(corpus)} 筆,"
          f"top-{OVERLAP_AT} 命中重疊)")
    print("=" * 64)
    mechs = list(BrewMechanism)

    overlaps = []
    for zh_q, en_q, gloss in CONCEPTS:
        print(f"\n── 概念:{gloss}")
        print(f"   ZH = 「{zh_q}」   EN = \"{en_q}\"")
        for mech in mechs:
            zh_hits = store.search(zh_q, mech, top_k=TOP_K)
            en_hits = store.search(en_q, mech, top_k=TOP_K)
            if not zh_hits or not en_hits:
                print(f"   [{mech.value:<11}] (該機制無召回)")
                continue
            zh_ids = [h["id"] for h in zh_hits[:OVERLAP_AT]]
            en_ids = [h["id"] for h in en_hits[:OVERLAP_AT]]
            ov = len(set(zh_ids) & set(en_ids)) / OVERLAP_AT
            overlaps.append(ov)
            print(f"   [{mech.value:<11}] top-{OVERLAP_AT} 重疊 = {ov:.0%}")
            # 印中文 query 的 top-3 命中,供人工判讀語意是否對題
            for h in zh_hits[:3]:
                r = _hit_record(h)
                print(f"        zh#{h['score']:+.3f}  {_label(r):<46} notes={_notes(r)}")
    if overlaps:
        mean_ov = sum(overlaps) / len(overlaps)
        print("\n" + "=" * 64)
        print(f"總結:zh↔en top-{OVERLAP_AT} 平均重疊 = {mean_ov:.0%}  "
              f"(n={len(overlaps)} 概念×機制)")
        print(f"      詞層分離 = {mean_true - mean_neg:+.3f}(真對 {mean_true:+.3f} / "
              f"負控 {mean_neg:+.3f})")
        print("=" * 64)
        print("判讀:重疊高 + 詞層分離大 → 嵌入已橋接,中文採購非阻塞,headline 只缺查詢入口。")
        print("      重疊低 → bge-m3 對這些(尤其台灣慣用)詞橋接不足,需 P0 多語詞表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
