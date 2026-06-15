"""近常數軸誠實標(honest near-constant-axis flagging,鐵則 §3 方向>絕對 / §4 誠實不確定)。

某風味軸在**該機制內**(§1 不跨機制)的分級加權離散度低於解析底(RANKABLE_STD_MIN)→
排序落在引擎雜訊內(pairwise direction ≈ chance)→ predict 標 `rankable=False` + 警告,
**只報量級、不宣稱方向**。本測試鎖三件事:

  1. 機制(`_Acc.stdev` / `GroupPrior.axis_stdev` / `.rankable`):加權標準差正確、近常數→0、
     薄資料→None、**§1 同軸跨機制各自判定不串味**。
  2. `engine.predict` 標註是**純 additive**:只加 rankable / within_mechanism_std,
     **絕不動 value/lower/upper**(§4 只可加寬不可收窄;覆蓋率不變)。
  3. **對得上 eval 的低方向軸**:用真語料 `corpus/global.jsonl`(GroupPrior.from_records
     不需嵌入器、純讀欄位 → 離線可跑)驗 balance/aftertaste 全機制被標、acidity/body/clarity
     全機制不被誤標、bitterness 在 immersion/percolation 被標而 pressure 不被標(§1 鐵證)。

全離線(雜湊嵌入 + 記憶體向量庫),不對 MAE 下硬門檻。
"""
from __future__ import annotations

import math

import pytest

from cie import physics
from cie.engine import Engine
from cie.portability import read_jsonl
from cie.retrieval import (MIN_GROUP_WEIGHT, RANKABLE_STD_MIN, GroupPrior, _Acc,
                           conformal_active_for, weighted_estimate)
from cie.schema import (FLAVOR_AXES, BeanRoast, BrewMechanism, BrewParams,
                        FlavorProfile, Grade, Process, Record)
from cie.store import VectorStore
from eval.run import CORPUS_PATH


# ────────────────────────────── 共用建構 ──────────────────────────────

def _rec(mech: BrewMechanism, *, grade: Grade = Grade.A, conf: float = 0.8,
         agtron: float = 72.0, origin: str = "Ethiopia", variety: str = "Heirloom",
         process: Process = Process.WASHED, **axes) -> Record:
    """一筆指定機制 / 風味值的記錄(未給的軸留 None,GroupPrior 會略過)。"""
    return Record(
        bean=BeanRoast(origin=origin, variety=variety, process=process,
                       roast_agtron=agtron),
        params=BrewParams(brew_mechanism=mech, method="x", water_temp_c=92.0,
                          brew_ratio=15.0, grind_um=300.0, contact_time_s=150.0),
        flavor=FlavorProfile(**axes),
        grade=grade, confidence=conf, user_id="global",
    )


# 6 筆(Σw=6×1.0×0.8=4.8 ≥ MIN_GROUP_WEIGHT)同機制記錄:acidity 大離散、balance 近常數。
_WIDE = [2.0, 3.0, 5.0, 6.0, 8.0, 9.0]      # pstdev = 2.5
_CONST = [6.0, 6.0, 6.0, 6.0, 6.0, 6.0]     # pstdev = 0.0


def _perc_records() -> list:
    return [_rec(BrewMechanism.PERCOLATION, acidity=a, balance=b)
            for a, b in zip(_WIDE, _CONST)]


def _perc_bean() -> BeanRoast:
    return BeanRoast(origin="Ethiopia", variety="Heirloom",
                     process=Process.WASHED, roast_agtron=72.0)


def _perc_params() -> BrewParams:
    return BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, method="V60",
                      water_temp_c=92.0, brew_ratio=15.0, grind_um=300.0,
                      contact_time_s=150.0)


def _cold_bean() -> BeanRoast:
    """與 _perc_records 完全不同豆(冷啟動:無同豆鄰居 → predict 走物理粗略支)。"""
    return BeanRoast(origin="Nepal", variety="Bourbon Pointu",
                     process=Process.NATURAL, roast_agtron=68.0)


# ────────────────── 1. _Acc 加權標準差 ──────────────────

def test_acc_stdev_matches_population_stdev():
    """等權 _Acc.stdev == statistics.pstdev。"""
    import statistics
    acc = _Acc()
    for v in _WIDE:
        acc.add(1.0, v)
    assert acc.stdev == pytest.approx(statistics.pstdev(_WIDE))
    assert acc.mean == pytest.approx(statistics.fmean(_WIDE))


def test_acc_stdev_weighted():
    """加權:值 5,5,8(權重 2,1)→ 均值 6、變異 2 → stdev √2。"""
    acc = _Acc()
    acc.add(2.0, 5.0)
    acc.add(1.0, 8.0)
    assert acc.mean == pytest.approx(6.0)
    assert acc.stdev == pytest.approx(math.sqrt(2.0))


def test_acc_stdev_constant_is_zero_not_negative():
    """近常數:浮點抵消不得吐負值,夾成 0.0。"""
    acc = _Acc()
    for _ in range(10):
        acc.add(0.5, 7.3)
    assert acc.stdev == 0.0


def test_acc_stdev_empty_is_none():
    assert _Acc().stdev is None
    assert _Acc().mean is None


# ────────────────── 2. GroupPrior.axis_stdev / rankable ──────────────────

def test_axis_stdev_within_mechanism():
    gp = GroupPrior.from_records(_perc_records())
    assert gp.axis_stdev("acidity", BrewMechanism.PERCOLATION) == pytest.approx(2.5)
    assert gp.axis_stdev("balance", BrewMechanism.PERCOLATION) == pytest.approx(0.0)


def test_rankable_wide_true_constant_false():
    gp = GroupPrior.from_records(_perc_records())
    assert gp.rankable("acidity", BrewMechanism.PERCOLATION) is True    # 大離散 → 可排序
    assert gp.rankable("balance", BrewMechanism.PERCOLATION) is False   # 近常數 → 標旗


def test_thin_data_returns_none_not_flag():
    """機制根層該軸 Σw < MIN_GROUP_WEIGHT → 無從判定 → None(不亂標)。"""
    thin = [_rec(BrewMechanism.PERCOLATION, acidity=3.0, conf=0.8)]   # Σw=0.8 < 3.0
    gp = GroupPrior.from_records(thin)
    assert gp.axis_stdev("acidity", BrewMechanism.PERCOLATION) is None
    assert gp.rankable("acidity", BrewMechanism.PERCOLATION) is None
    # 該機制完全無資料的軸亦 None(沒有桶)
    assert gp.rankable("acidity", BrewMechanism.PRESSURE) is None


def test_axis_stdev_root_bucket_not_subgroup():
    """axis_stdev 取機制根層母體離散,不被焙度/處理法子分群人為縮小。"""
    # 同機制、同軸,但跨兩個焙度帶各自近常數、合起來大離散:
    #   light(agtron 75)balance≈3、dark(agtron 40)balance≈8 → 子層各≈0,根層母體離散大。
    recs = ([_rec(BrewMechanism.IMMERSION, agtron=75.0, balance=v) for v in (3.0, 3.0, 3.0)]
            + [_rec(BrewMechanism.IMMERSION, agtron=40.0, balance=v) for v in (8.0, 8.0, 8.0)])
    gp = GroupPrior.from_records(recs)
    # 根層母體 std 應反映 3↔8 的大跨距(≈2.5),而非子層的 0 → rankable True。
    assert gp.axis_stdev("balance", BrewMechanism.IMMERSION) == pytest.approx(2.5)
    assert gp.rankable("balance", BrewMechanism.IMMERSION) is True


# ────────────────── 3. §1 同軸跨機制不串味(鐵則核心) ──────────────────

def test_rankability_is_per_mechanism_no_cross_contamination():
    """同一軸:immersion 大離散(可排序)、pressure 近常數(標旗)——各自判定,§1 不跨機制平均。

    對得上真語料 bitterness:immersion/percolation wstd 0.60–0.64(標)vs pressure 1.38(不標)。
    """
    recs = ([_rec(BrewMechanism.IMMERSION, bitterness=v) for v in _WIDE]      # std 2.5
            + [_rec(BrewMechanism.PRESSURE, bitterness=v) for v in _CONST])   # std 0
    gp = GroupPrior.from_records(recs)
    assert gp.rankable("bitterness", BrewMechanism.IMMERSION) is True
    assert gp.rankable("bitterness", BrewMechanism.PRESSURE) is False
    # 反證:若曾跨機制平均,immersion 的大離散會被 pressure 的常數拉低 → 兩者離散度應一致;
    # 實際 immersion std 仍 ≈2.5、pressure ≈0,彼此獨立。
    assert gp.axis_stdev("bitterness", BrewMechanism.IMMERSION) == pytest.approx(2.5)
    assert gp.axis_stdev("bitterness", BrewMechanism.PRESSURE) == pytest.approx(0.0)


# ────────────────── 4. engine.predict 純 additive 標註 ──────────────────

def test_predict_cold_start_flags_near_constant_only():
    """冷啟動 predict:near-constant 軸標 rankable=False、signal 軸 True、薄軸不標旗。"""
    store = VectorStore()
    store.upsert_many(_perc_records())
    eng = Engine(store, canonical=None)
    out = eng.predict(_cold_bean(), _perc_params())     # 冷啟動(無同豆)
    pf = out["predicted_flavor"]

    assert pf["balance"]["rankable"] is False            # 近常數 → 標旗
    assert pf["acidity"]["rankable"] is True             # 大離散 → 不標
    assert pf["balance"]["within_mechanism_std"] == pytest.approx(0.0)
    assert pf["acidity"]["within_mechanism_std"] == pytest.approx(2.5)
    # 薄/無資料軸(records 未給值)→ rankable=None → **不掛鍵**(不亂標)
    assert "rankable" not in pf["sweetness"]
    # 彙總警告轉達近常數軸(對得上 §3/§4)
    assert any("近常數" in w and "balance" in w for w in out["warnings"])


def test_predict_flag_is_purely_additive_never_narrows_interval():
    """§4 鐵則:標旗只加鍵,**絕不動 value/lower/upper**——與底層估計逐位相同。"""
    store = VectorStore()
    store.upsert_many(_perc_records())
    eng = Engine(store, canonical=None)
    bean, params = _cold_bean(), _perc_params()

    # 底層真值:同一 GroupPrior 下的物理粗略(predict 冷啟動支的數值來源)
    gp = eng._group_prior()
    coarse = physics.coarse_flavor_axes(bean, params, group_prior=gp)

    out = eng.predict(bean, params)
    pf = out["predicted_flavor"]
    for axis in FLAVOR_AXES:
        val, lo, hi = coarse[axis]
        assert pf[axis]["value"] == val                  # 點估未被標旗改動
        assert pf[axis]["lower"] == lo                   # 下界未被收窄
        assert pf[axis]["upper"] == hi                   # 上界未被收窄
        assert pf[axis]["lower"] <= pf[axis]["upper"]    # 區間方向健全


def test_predict_same_bean_path_also_annotated():
    """warm(同豆)支也標旗,且 value/lower/upper 與底層 weighted_estimate **逐位相同**(§4 additive)。

    冷啟動支已逐位比 coarse_flavor_axes(上一測);此處對 warm 支同樣重建底層真值——以同一召回 +
    同一群組先驗呼叫 weighted_estimate——證標旗未把點估/區間挪進區間內(弱 lower<=value<=upper 攔不到)。
    """
    store = VectorStore()
    store.upsert_many(_perc_records())
    eng = Engine(store, canonical=None)
    bean, params = _perc_bean(), _perc_params()
    out = eng.predict(bean, params)                      # 同豆 → warm 支
    pf = out["predicted_flavor"]
    assert "balance" in pf and "acidity" in pf           # 有值的軸被估
    assert pf["balance"]["rankable"] is False
    assert pf["acidity"]["rankable"] is True

    # 重建 predict warm 支的底層真值(同召回 hits + 同群組先驗,皆決定性)→ 逐位比對未被標旗改動。
    hits = eng._recall(bean, params.brew_mechanism, FlavorProfile())
    same = eng._same_bean(bean, hits)
    assert same                                          # 確認確實走 warm 支(非空同豆鄰居)
    gp = eng._group_prior()
    proc = bean.process.value if bean.process else ""
    prior_axes = gp.axis_priors(params.brew_mechanism, bean.roast_band(), proc)
    # 須鏡射 predict() 的實際呼叫(含 conformal q̂ 讀取閘):線上嵌入器 == 校準嵌入器才傳
    # mechanism 走 per-機制 q̂,否則(離線 hash 嵌入器)傳 None 退回 1.64。否則 truth 與
    # predict 用不同係數,lower/upper 不符(非標旗 bug,是 conformal 閘生效)。
    use_mech = (params.brew_mechanism
                if conformal_active_for(getattr(eng.store, "model_id", None)) else None)
    for axis in ("balance", "acidity"):
        truth = weighted_estimate(same, f"flavor_{axis}", prior_value=prior_axes.get(axis),
                                  mechanism=use_mech)
        assert pf[axis]["value"] == truth.value          # 點估逐位相同(未被標旗挪動)
        assert pf[axis]["lower"] == truth.lower          # 下界未被收窄
        assert pf[axis]["upper"] == truth.upper          # 上界未被收窄


def test_predict_no_group_prior_no_annotation():
    """store 無 iter_records(gp=None)→ 完全不標旗(優雅降級,維持現行為)。"""
    eng = Engine(VectorStore(), canonical=None)
    # 空庫 → gp 建得出但所有軸 Σw=0 → rankable 全 None → 無 rankable 鍵
    out = eng.predict(_perc_bean(), _perc_params())
    for est in out["predicted_flavor"].values():
        assert "rankable" not in est
    assert not any("近常數" in w for w in out["warnings"])


# ────────────────── 5. 對得上 eval:真語料低方向軸(離線,純讀欄位) ──────────────────

# 鎖定於當前 checked-in corpus/global.jsonl + RANKABLE_STD_MIN=0.75 的離散度型態
#(= workers_ai k=5 CV 量到的 wstd;見 retrieval.RANKABLE_STD_MIN 註解)。
# ⚠ 刻意只斷言「距門檻有餘裕」的格(balance/aftertaste 全機制 <0.72、acidity/body/clarity 全 ≥0.83、
#   bitterness 0.60/0.64 vs 1.38);仍有薄邊際格(aftertaste/immersion 0.72、body/percolation 0.83 距
#   0.75 僅 0.03/0.08)。**這是經驗 pin、非永恆不變式**:語料增刪(尤其新增 A/B 校準)會挪動 wstd,
#   個別薄格可能翻轉、本組測試須隨之更新——這正是「動語料後重跑 CV 重校門檻」的提醒點,非 bug。
_MECHS = [BrewMechanism.IMMERSION, BrewMechanism.PERCOLATION, BrewMechanism.PRESSURE]


@pytest.fixture(scope="module")
def corpus_gp() -> GroupPrior:
    return GroupPrior.from_records(read_jsonl(CORPUS_PATH))


@pytest.mark.parametrize("axis", ["balance", "aftertaste"])
def test_corpus_low_direction_axes_flagged_all_mechanisms(corpus_gp, axis):
    """eval 低方向軸(balance/aftertaste,pairwise dir ≈ chance)→ 全機制 rankable=False。"""
    for m in _MECHS:
        assert corpus_gp.rankable(axis, m) is False, f"{axis}/{m.value} 應被標近常數"


@pytest.mark.parametrize("axis", ["acidity", "body", "clarity"])
def test_corpus_signal_axes_never_misflagged(corpus_gp, axis):
    """高變異訊號軸(acidity/body/clarity,wstd 0.83–1.42)→ 全機制 rankable=True,不誤標。"""
    for m in _MECHS:
        assert corpus_gp.rankable(axis, m) is True, f"{axis}/{m.value} 不應被誤標"


def test_corpus_bitterness_per_mechanism_split(corpus_gp):
    """§1 鐵證:bitterness immersion/percolation(wstd 0.60–0.64)標、pressure(1.38)不標。"""
    assert corpus_gp.rankable("bitterness", BrewMechanism.IMMERSION) is False
    assert corpus_gp.rankable("bitterness", BrewMechanism.PERCOLATION) is False
    assert corpus_gp.rankable("bitterness", BrewMechanism.PRESSURE) is True
