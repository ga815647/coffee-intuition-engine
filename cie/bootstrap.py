"""一次性 bootstrap:把策展語料 `corpus/global.jsonl` 載入 canonical 真相層。

    python -m cie.bootstrap            # canonical 為空時載入 corpus/global.jsonl
    python -m cie.bootstrap --force    # 清空 canonical 後整份重載(覆寫;謹慎)
    python -m cie.bootstrap --force --path corpus/global.export.jsonl
                                       # 災後整庫重建:從 snapshot 快照(策展種子+晉升)整份重灌
                                       # ⚠️ --force=replace_all,會連 self 一起清。只還原 global
                                       #    且不動 self → 改用 `python -m cie.snapshot --restore`(upsert)。

之後跑 `python -m cie.rebuild` 從 canonical 用『當前』嵌入器重嵌、灌入向量庫
(Vectorize / Qdrant / 記憶體)。**驗收**:rebuild 後向量庫筆數 ≈ corpus/global.jsonl
行數(目前 446),而非 seeds/anchors.jsonl 的 6 筆。

為何不直接用 `cie.seed`:`seed` 只灌 `seeds/anchors.jsonl`(6 筆冷啟動錨點,
給空庫 demo 用)。真正的策展真相是 `corpus/global.jsonl`(446 筆,
`tools/qa_merge.py` 由 `corpus/raw/` provenance 重生)。canonical 真相層 =
此策展語料(初始) + 之後 `log_calibration` 累積的人類校準回饋。

鐵則對齊:`corpus/raw/` 是 provenance、`global.jsonl` 是策展真相、向量是衍生物
(§14.5)。本步驟把策展真相灌進 canonical sink,讓 Vectorize 這種無法自存全量的
後端**有源可重建**(換嵌入模型必須重嵌;canonical 不變、向量重生)。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from .canonical import CanonicalStore, get_canonical
from .config import CONFIG
from .portability import read_jsonl
from .schema import Record

CORPUS_PATH = Path(__file__).resolve().parent.parent / "corpus" / "global.jsonl"


def load_corpus(path: Path = CORPUS_PATH) -> List[Record]:
    """讀策展語料 `corpus/global.jsonl` → Record 清單(不觸碰向量庫 / canonical)。"""
    return read_jsonl(path)


def bootstrap(canonical: Optional[CanonicalStore] = None, path: Path = CORPUS_PATH,
              force: bool = False, config=CONFIG) -> int:
    """把 `corpus/global.jsonl` 載入 canonical sink。回傳寫入筆數。

    canonical 非空且未 `force` → 拒絕(避免重複 append 與誤覆蓋累積回饋);
    `force=True` → `replace_all` 清空後整份重載(re-init / 災後重建)。
    """
    canonical = canonical if canonical is not None else get_canonical(config)
    records = load_corpus(path)
    existing = sum(1 for _ in canonical.iter_records())
    if existing and not force:
        raise RuntimeError(
            f"canonical 已有 {existing} 筆;bootstrap 是一次性初始化,不重複灌入。"
            f"確定要清空重載請用 force=True(CLI:python -m cie.bootstrap --force)。"
        )
    if force:
        return canonical.replace_all(records)
    return canonical.extend(records)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    argv = sys.argv[1:]
    force = "--force" in argv
    path = CORPUS_PATH
    for i, a in enumerate(argv):                 # --path <p> 或 --path=<p>(災後從 snapshot 重灌)
        if a == "--path" and i + 1 < len(argv):
            path = Path(argv[i + 1]).resolve()
        elif a.startswith("--path="):
            path = Path(a.split("=", 1)[1]).resolve()
    canonical = get_canonical()
    n = bootstrap(canonical=canonical, path=path, force=force)
    print(f"已將 {path.name} 的 {n} 筆語料載入 canonical"
          f"({type(canonical).__name__})。")
    print("下一步:python -m cie.rebuild   # 從 canonical 重嵌、灌入向量庫")


if __name__ == "__main__":
    main()
