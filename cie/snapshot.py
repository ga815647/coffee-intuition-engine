"""global 真相快照 → git(誤刪可一鍵復原)。

定位:**owner 的 curation 收尾步驟**。owner 在本機晉升 / 直接寫 global / 修正之後,
把 D1 的**全量 global**匯出成 git-tracked JSONL 並 commit;之後 D1 被誤刪,也能從 git
還原 global 客觀真相層。**self 層(各 guest 個資、持續變動)不在此檔**——那走 `cie.export`
的私密全量備份(§B,絕不進公開 git)。

    python -m cie.snapshot              # 匯出 global → corpus/global.export.jsonl + git commit
    python -m cie.snapshot --no-commit  # 只匯出,不 commit(自行 review / 手動 commit)
    python -m cie.snapshot --restore    # 反向:從 export 檔把 global upsert 回 D1(不動 self)

## 為何另存 `corpus/global.export.jsonl`(不覆蓋 `corpus/global.jsonl`)

`corpus/global.jsonl` 是 `tools/qa_merge.py` 由 `corpus/raw/*.jsonl`(provenance + 對抗式
降級表)**確定性重生**的**策展種子**;它的角色是「可重現的初始語料」,鍵在 (file, lineno)。
而 owner 晉升的 self 記錄 / 直接 global 寫入 / 修正**沒有 raw provenance**,塞回 raw 會破壞
qa_merge 的可重現性。故快照另存一檔:

  `corpus/global.export.jsonl` = **完整 live global 快照**(策展種子 + 累積的晉升 / global 寫入 / 修正)
                               = **災後復原的權威來源**。

`corpus/global.jsonl` 維持策展種子角色不變(qa_merge 仍可重生它)。

## 復原語義(load-bearing)

`restore_global` 用 **`canonical.extend`(INSERT OR REPLACE 逐筆 upsert)**,只還原 / 覆寫
**global** 列,**不碰 self**(self 不在此檔;絕不因復原 global 而清掉 guest 的 self)。
這刻意**不是** `replace_all`(那會 `DELETE FROM` 連 self 一起清)。整庫災後重建(D1 全空)
要連策展種子一起重灌,另走 `python -m cie.bootstrap --force --path corpus/global.export.jsonl`。

## 唯讀 / git 副作用邊界

匯出對 D1 **唯讀**(只 `SELECT`,不 mutate)。git 副作用(`add` / `commit`)**只在本 CLI**,
**絕不在任何 MCP 工具呼叫裡**做;`promote_customization` 只在回傳附「請跑 snapshot」提醒,
一個 curation session 收尾跑一次 = 一個 commit(非一筆一 commit)。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from .canonical import CanonicalStore, get_canonical
from .config import CONFIG
from .mcp_principal import GLOBAL_USER_ID
from .portability import export_jsonl, read_jsonl
from .schema import Record

ROOT = Path(__file__).resolve().parent.parent
GLOBAL_EXPORT_PATH = ROOT / "corpus" / "global.export.jsonl"


def _global_sorted(records: Iterable[Record]) -> List[Record]:
    """濾出 global 列、依 **id 確定性排序**(穩定 diff:同 id 永遠同一行,修正只改該行)。"""
    return sorted((r for r in records if r.user_id == GLOBAL_USER_ID), key=lambda r: r.id)


def export_global(canonical: Optional[CanonicalStore] = None,
                  path: Path = GLOBAL_EXPORT_PATH, config=CONFIG) -> int:
    """讀 canonical 全量 → 濾 global → 依 id 排序 → 寫 git-tracked JSONL。回傳寫入筆數。

    對 D1 **唯讀**(`iter_records` = 單一 `SELECT`,不 mutate)。排序確定性 → 重跑只在
    新增 / 修正處產生乾淨 diff。self 列被排除(不入公開 git)。
    """
    canonical = canonical if canonical is not None else get_canonical(config)
    records = _global_sorted(canonical.iter_records())
    return export_jsonl(records, path)


def restore_global(canonical: Optional[CanonicalStore] = None,
                   path: Path = GLOBAL_EXPORT_PATH, config=CONFIG) -> int:
    """從 export 檔把 global **upsert** 回 canonical(`extend` = INSERT OR REPLACE)。回傳筆數。

    **只還原 global、不動 self**(self 不在此檔)。刻意非 `replace_all`(那會清掉 self)。
    檔內若混入非 global 列(理論上不會)→ 拒絕,避免把錯誤命名空間灌進真相。
    """
    canonical = canonical if canonical is not None else get_canonical(config)
    records = read_jsonl(path)
    bad = [r.id for r in records if r.user_id != GLOBAL_USER_ID]
    if bad:
        raise ValueError(
            f"export 檔含非 global 列({len(bad)} 筆,如 {bad[:3]});"
            f"global 快照只應有 global,復原中止以免污染命名空間。")
    return canonical.extend(records)


# ────────────────────────────── git 副作用(只在本 CLI) ──────────────────────────────

def _git(args: List[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8")


def git_commit_snapshot(path: Path = GLOBAL_EXPORT_PATH, count: Optional[int] = None,
                        cwd: Path = ROOT) -> bool:
    """`git add <path>` → 若該檔有變更才 `git commit`(只 stage 這一檔)。回傳是否真的 commit。

    防呆:非 git repo / 無 git → 印警告回 False(匯出仍已落檔,可手動 commit)。
    只 stage 快照檔,不掃進其他髒檔;無變更則跳過(不產空 commit)。
    """
    rel = str(path.relative_to(cwd)) if path.is_absolute() else str(path)
    inside = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        print(f"[snapshot] 非 git 工作區(或無 git):已匯出 {rel},未 commit。")
        return False
    add = _git(["add", "--", rel], cwd)
    if add.returncode != 0:
        print(f"[snapshot] git add 失敗:{add.stderr.strip()}")
        return False
    # 該檔相對 HEAD 無變更 → 不 commit(--quiet:有差異回 1)。
    if _git(["diff", "--cached", "--quiet", "--", rel], cwd).returncode == 0:
        print(f"[snapshot] {rel} 無變更,跳過 commit。")
        return False
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    n = "" if count is None else f" ({count} 筆)"
    msg = f"chore(snapshot): global 真相快照{n} @ {ts}"
    # `commit -- <rel>`:只 commit 該 pathspec 的變更(不夾帶其他已暫存的髒檔)。
    commit = _git(["commit", "-m", msg, "--", rel], cwd)
    if commit.returncode != 0:
        print(f"[snapshot] git commit 失敗:{commit.stderr.strip()}")
        return False
    print(f"[snapshot] 已 commit {rel}{n}:{msg}")
    return True


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    argv = sys.argv[1:]
    path = GLOBAL_EXPORT_PATH
    for a in argv:                            # --path=... 覆寫(預設 corpus/global.export.jsonl)
        if a.startswith("--path="):
            path = Path(a.split("=", 1)[1]).resolve()

    canonical = get_canonical()
    if "--restore" in argv:
        n = restore_global(canonical=canonical, path=path)
        print(f"已從 {path} upsert 還原 {n} 筆 global 回 canonical"
              f"({type(canonical).__name__});self 未受影響。")
        return

    n = export_global(canonical=canonical, path=path)
    print(f"已把 {n} 筆 global 真相匯出到 {path}(依 id 確定性排序,對 D1 唯讀)。")
    if "--no-commit" in argv:
        print("(--no-commit:略過 git commit;請自行 review 後 commit。)")
        return
    git_commit_snapshot(path=path, count=n)


if __name__ == "__main__":
    main()
