"""全量 D1 備份(global + 各 self)→ 私密 JSONL(選配排程備份)。

定位:**選配的全量備份**,把 D1 整庫(global 客觀層 + 每個 guest 的 self 客製層)dump 成
JSONL。對 D1 **唯讀**、確定性排序。與 §A `cie.snapshot`(只 global、進公開 git)互補:本檔
含 self → **含 guest 個資(user_id 命名空間)**,目的地**必須私密**。

    python -m cie.export                       # → ./backups/d1-full-export.jsonl(預設,gitignored)
    python -m cie.export --out path/to/bak.jsonl

## 隱私(鐵則)

self 列帶各 guest 的命名空間(user_id)與其校準內容 = **個資**。本檔**絕不可進公開 git**:
  - 預設輸出 `./backups/`(已 .gitignore);
  - 排程備份請送**私密**目的地:本機磁碟 / 私有 R2 bucket / 私有 repo;
  - 檔內**不含任何 token**(token 只在 `.env` / Secret Manager,從不入任何匯出)。

global-only 的「可公開、進 git、誤刪可復原」走 `cie.snapshot`;本檔是「含個資的完整備份」。

## 排程(交使用者設,CF token 當 secret;範本見 `tools/backup_self.example.yml`)

不綁死平台。三選一(範例見該檔 / docs):GitHub Action(每週,推私有目的地)/ VPS cron /
Cloud Scheduler → Cloud Run job。CF 金鑰一律走平台 secret,不寫進 repo。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, List, Optional

from .canonical import CanonicalStore, get_canonical
from .config import CONFIG
from .portability import export_jsonl
from .schema import Record

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT_PATH = ROOT / "backups" / "d1-full-export.jsonl"


def _all_sorted(records: Iterable[Record]) -> List[Record]:
    """全量,依 **(user_id, id) 確定性排序**:命名空間分群、穩定 diff、可重現。"""
    return sorted(records, key=lambda r: (r.user_id, r.id))


def export_all(canonical: Optional[CanonicalStore] = None,
               path: Path = DEFAULT_EXPORT_PATH, config=CONFIG) -> int:
    """讀 canonical 全量(global + 各 self)→ 依 (user_id, id) 排序 → 寫私密 JSONL。回傳筆數。

    對 D1 **唯讀**(`iter_records` = 單一 `SELECT`)。**含 self 個資**:呼叫端須確保 `path`
    為私密位置(預設 `./backups/`,已 gitignore)。
    """
    canonical = canonical if canonical is not None else get_canonical(config)
    records = _all_sorted(canonical.iter_records())
    return export_jsonl(records, path)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    argv = sys.argv[1:]
    path = DEFAULT_EXPORT_PATH
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            path = Path(argv[i + 1]).resolve()
        elif a.startswith("--out="):
            path = Path(a.split("=", 1)[1]).resolve()

    canonical = get_canonical()
    n = export_all(canonical=canonical, path=path)
    print(f"已把全量 {n} 筆(global + 各 self)匯出到 {path}"
          f"(依 (user_id,id) 確定性排序,對 D1 唯讀)。")
    print("⚠️ 隱私:本檔含 self 個資(各 guest 命名空間),"
          "請存私密位置、勿進公開 git;token 不在檔內。")


if __name__ == "__main__":
    main()
