"""owner curation:把「偏酸 fix 方向爭議」的 global 知識條目寫入 canonical 真相層(→ live D1)。

這是 Phase 2 的 **data 層對應物**:physics.py 的 `contested_diagnosis`(code 標記)驅動 live
`diagnose` 輸出爭議狀態;本條目把同一份『Cotter 第二訊號 = B 級』記成 canonical 真相,讓它
(a) 可被 `snapshot` 匯出回 git 可回溯,(b) 出現在 percolation recall 的 evidence(B 級鄰居)。

鐵則對齊:
  - user_id=global、grade=B(第二訊號,不覆蓋 working prior;非 A 級真值)。protocol/source 與
    physics 常數共用(code 與 data 一致)。
  - **機制 = percolation**:Cotter 證據本就是 drip;依機制三軌硬隔離,它只該出現在 percolation
    recall,不可跨進 immersion/pressure(故知識條目也綁 percolation)。
  - **數值風味軸全部留 None**:不污染 `predict` 的數值估計(weighted_estimate 跳過 None 軸),
    只作為 evidence 鄰居與審計痕跡;方向/語意走 flavor_notes 與 embedding_text。
  - 固定 id + 固定 timestamp → INSERT OR REPLACE 冪等;重跑不產生重複、snapshot diff 穩定。

用法:
  python -m tools.seed_contested_acidity            # dry-run:只印出將寫入的 Record JSON
  python -m tools.seed_contested_acidity --write    # 真寫入 canonical(.env=d1 → live D1)
  python -m tools.seed_contested_acidity --write --restore-memory  # 寫後不重建(預設就不重建)

⚠️ `--write` 會寫 live 共用 D1(刻意 owner curation,合法)。寫完請跑 `python -m cie.snapshot`
   把 global 快照進 git。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 允許 `python tools/seed_contested_acidity.py` 直接跑(補 sys.path)。
sys.path.insert(0, str(_ROOT))


def _load_dotenv() -> None:
    """把 repo 根 .env 注入 os.environ(**只補未設定的鍵**,已存在的 shell env 優先)。

    本 repo 無 python-dotenv 自動載入,而 canonical 後端(local vs d1)由 CONFIG 在 import
    cie.config 時就從 os.environ 定案——故**必須在 import cie.* 之前**載入,否則 --write 會
    悄悄落到 local jsonl 而非 live D1(memory:.env 不會自動載入)。token 只進 os.environ,
    絕不印出。
    """
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:   # shell env 優先,不覆蓋
            os.environ[key] = val


_load_dotenv()  # 必須早於下列 cie 匯入(CONFIG 在 import 時定後端)

from cie import physics  # noqa: E402
from cie.canonical import get_canonical  # noqa: E402
from cie.schema import (  # noqa: E402
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)

# 固定 id / timestamp → 冪等寫入 + 穩定 snapshot diff。
RECORD_ID = "contested-acidity-direction-ucdavis"
CURATION_TS = "2026-06-14T00:00:00+00:00"


def build_record() -> Record:
    """偏酸 fix 方向爭議的 global B 級知識條目(第二訊號 = Cotter/UC Davis)。"""
    return Record(
        id=RECORD_ID,
        bean=BeanRoast(origin="", process=Process.OTHER),  # 一般原則,非特定豆
        params=BrewParams(
            brew_mechanism=BrewMechanism.PERCOLATION,       # Cotter 證據 = drip;機制硬隔離
            method="(knowledge) UC Davis strength-vs-extraction sensory",
        ),
        flavor=FlavorProfile(
            acidity_type=AcidityType.MIXED,                 # 數值軸留 None:不污染 predict 數值
            flavor_notes=["偏酸_fix_方向_已知爭議", "降濃度降酸(↓TDS_穩健)",
                          "drip_EY_與酸度_弱_且隨機制變號", "增萃常同時升TDS_可能反升酸"],
            defects=["sour (contested fix direction)"],
        ),
        grade=Grade.B,                                      # 第二訊號,不覆蓋 working prior
        protocol=physics.CONTESTED_ACIDITY_PROTOCOL,
        source=physics.CONTESTED_ACIDITY_SOURCE,
        confidence=0.4,                                     # B 級量級
        user_id="global",                                  # 客觀因果層(owner curation)
        timestamp=CURATION_TS,
        embedding_text=(
            "偏酸 fix 方向是已知爭議 open question。working prior=增萃降酸(酸=萃取不足,磨細/升溫/"
            "延長,convergent + 物理先驗)。second signal(UC Davis Coffee Center drip 感官研究,把"
            "濃度 TDS 軸與萃取 EY 軸分離):①穩健——加水/降 TDS 真的降知覺酸度(知覺 sour 追隨可滴定"
            "酸度,後者與 TDS 線性、與 EY 幾乎無關);②較弱且常被誤掛——『多萃即降酸』不可靠,drip"
            " 提高 EY 只讓酸微降,且 percolation 磨細/升溫/延長會同時拉高 EY(弱降酸)與 TDS(升酸)、"
            "淨效不定,一味增萃可能反升酸;EY→酸度符號隨機制翻面。B 級第二訊號(單一實驗室、drip 限定、"
            "無獨立複現),不覆蓋 working prior、不可跨機制外推;唯一 A 級裁決 = 使用者自己的閉環 A/B。"
            "percolation drip。"
        ),
    )


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    from cie.config import CONFIG  # 延後匯入:確保在 _load_dotenv 之後

    argv = sys.argv[1:]
    rec = build_record()
    backend = CONFIG.canonical_backend  # "d1" = live 共用 D1;"local" = 本機 jsonl
    if "--write" not in argv:
        print("=== DRY-RUN(未寫入)===")
        print(rec.model_dump_json(indent=2))
        print("\nprotocol =", physics.CONTESTED_ACIDITY_PROTOCOL)
        print("source   =", physics.CONTESTED_ACIDITY_SOURCE)
        print(f"\n>>> canonical 後端 = {backend!r}"
              + ("(將寫入 live 共用 D1)" if backend == "d1"
                 else "(注意:非 d1 → 不會寫到 live D1)"))
        print("要真寫入請加 --write,寫完跑:python -m cie.snapshot")
        return

    canonical = get_canonical()
    canonical.append(rec)  # INSERT OR REPLACE(固定 id → 冪等)
    print(f"已寫入 global 知識條目 id={rec.id}(grade=B)到 canonical"
          f"({type(canonical).__name__})。")
    print("下一步:python -m cie.snapshot   # 匯出 global → corpus/global.export.jsonl + git commit")


if __name__ == "__main__":
    main()
