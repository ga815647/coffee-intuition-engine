"""單一工具註冊點 — stdio 與 HTTP 兩入口共用一份工具邏輯(設計 §13 / §16「兩扇門」)。

分層:
  do_*(engine, principal, **kw)   純函式工具邏輯;principal 顯式傳入。**唯一一份邏輯。**
                                   讀:套用 principal 的讀範圍(加性過濾,預設不過濾)。
                                   寫:過 apply_write_trust 寫入閘(唯本機 owner 能寫)再交 engine。
  register_tools(mcp, engine, *, include_writes)
                                   把 do_* 包成 MCP 工具;從 contextvar 取當前 principal。
                                   **include_writes=False(HTTP 公開門)只掛讀工具**,
                                   `log_calibration` 根本不在 HTTP 暴露(網路上無寫入路徑)。
                                   stdio 用預設 True,掛全部(含寫)。

鐵則:HTTP 層**不重寫引擎邏輯** — 檢索 / 收縮 / conformal / 機制三軌 / 物理先驗全在
engine 與其下游;本檔只做「身分 → 讀範圍 / 寫入閘」這層薄治理 + 參數打包。
"""
from __future__ import annotations

from typing import List, Optional

from .engine import Engine
from .mcp_principal import Principal, apply_write_trust, current_principal
from .schema import (
    BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)

# ── 工具描述(寫滿約束,給呼叫端模型;沿用 Aiden 的 rich-description 慣例) ──

QUERY_DESC = """查相似情境並依 mode 推理。讀工具(不寫入)。

brew_mechanism(必填,硬分區鍵,三軌永不互通):
  immersion(浸泡/法壓:E 對研磨/溫度不敏感)| percolation(滴濾/手沖:E 對研磨/流速極敏感)
  | pressure(義式加壓:研磨→E 非單調、通道效應主導)。查 A 機制絕不混入 B 機制證據。
mode: recommend(起手參數)| predict(預測風味,需給 params)| diagnose(問題歸因,給 defect)。
輸出帶 conformal 區間 / 證據 / 警告。**定位:方向與排序的信心 > 絕對數值**(R² 天花板 ~0.5);
區間是傾向不是真值,鄰居越少越寬。讀共享真相(global 客觀層 + owner 校準);此門唯讀。"""

LOG_DESC = """寫回一筆校準(寫工具,**只在本機 stdio owner 通道**;HTTP 公開門不暴露此工具)。
寫入完整性鐵則(套用於 owner 自己,擋失誤):
  - 預設 grade=C;A 級(人類感官真值)須附 protocol(如 SCA_cupping),否則 engine 拒收。
  - grade=prediction 為內部保留級(引擎自身預測),**不得當人類真值注入**(注入即拒收)。
  - user_id:global 客觀因果層 / self 個人偏好層由你指定(本機刻意校正)。
回傳 {"ok":bool,...};被閘擋下時 ok=False 並說明原因。"""

SWAP_DESC = """換泡法推味道(讀工具,純物理先驗)。跨機制僅定性、標高不確定
(物理軸不足以涵蓋壓力/流動動力學);同機制較可信。請配合 predict() 在目標機制重新預測。"""


def _bean(origin="", variety="", process="other", roast_agtron=None) -> BeanRoast:
    return BeanRoast(
        origin=origin, variety=variety,
        process=Process(process if process in Process._value2member_map_ else "other"),
        roast_agtron=roast_agtron,
    )


# ────────────────────────────── 純邏輯(do_*) ──────────────────────────────

def do_query(
    engine: Engine,
    principal: Principal,
    *,
    brew_mechanism: str,
    mode: str = "recommend",
    origin: str = "",
    variety: str = "",
    process: str = "other",
    roast_agtron: Optional[float] = None,
    water_temp_c: Optional[float] = None,
    brew_ratio: Optional[float] = None,
    grind_um: Optional[float] = None,
    contact_time_s: Optional[float] = None,
    tds_pct: Optional[float] = None,
    ey_pct: Optional[float] = None,
    defect: str = "",
) -> dict:
    """query_flavor_map 的邏輯。讀範圍由 principal 決定(§16.3)。"""
    mech = BrewMechanism(brew_mechanism)
    bean = _bean(origin, variety, process, roast_agtron)
    scope = principal.read_user_ids  # None=不過濾(本地/owner);否則 global + 自己

    if mode == "recommend":
        return engine.recommend(bean, mech, user_ids=scope)
    if mode == "predict":
        params = BrewParams(
            brew_mechanism=mech, water_temp_c=water_temp_c, brew_ratio=brew_ratio,
            grind_um=grind_um, contact_time_s=contact_time_s, tds_pct=tds_pct, ey_pct=ey_pct,
        )
        return engine.predict(bean, params, user_ids=scope)
    if mode == "diagnose":
        # 純物理先驗,無召回 → 不涉讀範圍。
        return engine.diagnose(mech, defect or "未指定", bean)
    return {"error": f"未知 mode: {mode};可用 recommend|predict|diagnose"}


def do_log_calibration(
    engine: Engine,
    principal: Principal,
    *,
    brew_mechanism: str,
    grade: str = "C",
    protocol: str = "",
    origin: str = "",
    variety: str = "",
    process: str = "other",
    roast_agtron: Optional[float] = None,
    method: str = "",
    water_temp_c: Optional[float] = None,
    brew_ratio: Optional[float] = None,
    grind_um: Optional[float] = None,
    contact_time_s: Optional[float] = None,
    tds_pct: Optional[float] = None,
    ey_pct: Optional[float] = None,
    acidity: Optional[float] = None,
    sweetness: Optional[float] = None,
    bitterness: Optional[float] = None,
    body: Optional[float] = None,
    aftertaste: Optional[float] = None,
    balance: Optional[float] = None,
    clarity: Optional[float] = None,
    flavor_notes: Optional[List[str]] = None,
    user_id: str = "self",
) -> dict:
    """log_calibration 的邏輯:先過寫入信任閘(§16.2),再交 engine(單一真相把關)。"""
    record = Record(
        bean=_bean(origin, variety, process, roast_agtron),
        params=BrewParams(
            brew_mechanism=BrewMechanism(brew_mechanism), method=method,
            water_temp_c=water_temp_c, brew_ratio=brew_ratio, grind_um=grind_um,
            contact_time_s=contact_time_s, tds_pct=tds_pct, ey_pct=ey_pct,
        ),
        flavor=FlavorProfile(
            acidity=acidity, sweetness=sweetness, bitterness=bitterness, body=body,
            aftertaste=aftertaste, balance=balance, clarity=clarity,
            flavor_notes=flavor_notes or [],
        ),
        grade=Grade(grade), protocol=protocol, user_id=user_id,
    )
    decision = apply_write_trust(record, principal)
    if not decision.ok:
        return {"ok": False, "error": decision.error, "gate": "write_trust"}
    out = engine.log_calibration(decision.record)  # A 須 protocol 等仍由 engine 把關
    if isinstance(out, dict) and decision.notes:
        out = {**out, "trust_notes": decision.notes}
    return out


def do_method_swap(
    engine: Engine,
    principal: Principal,
    *,
    to_mechanism: str,
    from_mechanism: str,
    to_method: str = "",
    origin: str = "",
    process: str = "other",
    roast_agtron: Optional[float] = None,
) -> dict:
    """predict_method_swap 的邏輯(純物理先驗,不涉讀範圍 / 寫入)。"""
    return engine.method_swap(
        bean=_bean(origin, "", process, roast_agtron),
        from_params=BrewParams(brew_mechanism=BrewMechanism(from_mechanism)),
        to_mechanism=BrewMechanism(to_mechanism), to_method=to_method,
    )


# ────────────────────────────── MCP 註冊(薄包裝) ──────────────────────────────

def register_tools(mcp, engine: Engine, *, include_writes: bool = True) -> None:
    """把 do_* 註冊成 MCP 工具。工具讀 contextvar 取當前 principal:
    HTTP 由認證中介層設定(reader);stdio 未設定 → LOCAL_PRINCIPAL(owner,零回歸)。

    include_writes:**HTTP 公開門傳 False → 只掛讀工具**(`log_calibration` 不在
    HTTP 暴露,網路上無寫入路徑;§16「兩扇門」第一道)。stdio 用預設 True 掛全部。
    """

    @mcp.tool(description=QUERY_DESC)
    def query_flavor_map(
        brew_mechanism: str,
        mode: str = "recommend",
        origin: str = "",
        variety: str = "",
        process: str = "other",
        roast_agtron: Optional[float] = None,
        water_temp_c: Optional[float] = None,
        brew_ratio: Optional[float] = None,
        grind_um: Optional[float] = None,
        contact_time_s: Optional[float] = None,
        tds_pct: Optional[float] = None,
        ey_pct: Optional[float] = None,
        defect: str = "",
    ) -> dict:
        return do_query(
            engine, current_principal(),
            brew_mechanism=brew_mechanism, mode=mode, origin=origin, variety=variety,
            process=process, roast_agtron=roast_agtron, water_temp_c=water_temp_c,
            brew_ratio=brew_ratio, grind_um=grind_um, contact_time_s=contact_time_s,
            tds_pct=tds_pct, ey_pct=ey_pct, defect=defect,
        )

    # 寫工具:**只在 include_writes(stdio)註冊**;HTTP 公開門根本不掛 → 網路上無寫入路徑。
    if include_writes:
        @mcp.tool(description=LOG_DESC)
        def log_calibration(
            brew_mechanism: str,
            grade: str = "C",
            protocol: str = "",
            origin: str = "",
            variety: str = "",
            process: str = "other",
            roast_agtron: Optional[float] = None,
            method: str = "",
            water_temp_c: Optional[float] = None,
            brew_ratio: Optional[float] = None,
            grind_um: Optional[float] = None,
            contact_time_s: Optional[float] = None,
            tds_pct: Optional[float] = None,
            ey_pct: Optional[float] = None,
            acidity: Optional[float] = None,
            sweetness: Optional[float] = None,
            bitterness: Optional[float] = None,
            body: Optional[float] = None,
            aftertaste: Optional[float] = None,
            balance: Optional[float] = None,
            clarity: Optional[float] = None,
            flavor_notes: Optional[List[str]] = None,
            user_id: str = "self",
        ) -> dict:
            return do_log_calibration(
                engine, current_principal(),
                brew_mechanism=brew_mechanism, grade=grade, protocol=protocol, origin=origin,
                variety=variety, process=process, roast_agtron=roast_agtron, method=method,
                water_temp_c=water_temp_c, brew_ratio=brew_ratio, grind_um=grind_um,
                contact_time_s=contact_time_s, tds_pct=tds_pct, ey_pct=ey_pct, acidity=acidity,
                sweetness=sweetness, bitterness=bitterness, body=body, aftertaste=aftertaste,
                balance=balance, clarity=clarity, flavor_notes=flavor_notes, user_id=user_id,
            )

    @mcp.tool(description=SWAP_DESC)
    def predict_method_swap(
        to_mechanism: str,
        from_mechanism: str,
        to_method: str = "",
        origin: str = "",
        process: str = "other",
        roast_agtron: Optional[float] = None,
    ) -> dict:
        return do_method_swap(
            engine, current_principal(),
            to_mechanism=to_mechanism, from_mechanism=from_mechanism, to_method=to_method,
            origin=origin, process=process, roast_agtron=roast_agtron,
        )
