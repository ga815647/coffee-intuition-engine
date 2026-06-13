"""單一工具註冊點 — stdio 與 HTTP 兩入口共用一份工具邏輯(設計 §13 / §16「三層 + 晉升」)。

分層:
  do_*(engine, principal, **kw)   純函式工具邏輯;principal 顯式傳入。**唯一一份邏輯。**
                                   讀:套用 principal 的讀範圍(加性過濾;member=[global,自己]、
                                       reader=[global]、owner=不過濾)。
                                   寫:過 apply_write_trust(member confinement + grade≤B)+ 流量閘,
                                       再交 engine。
  register_tools(mcp, engine, *, include_writes, include_promotion)
                                   把 do_* 包成 MCP 工具;從 contextvar 取當前 principal。
                                   `include_writes`(預設 True):掛 `log_calibration`(member 受限寫 /
                                       owner 自由寫)。`include_promotion`(預設 False):掛晉升工具
                                       (`list_customizations` / `promote_customization`)——**只在 stdio
                                       owner 門**,HTTP 不掛(網路無晉升 / global 寫入路徑)。

鐵則:HTTP 層**不重寫引擎邏輯** — 檢索 / 收縮 / conformal / 機制三軌 / 物理先驗全在
engine 與其下游;本檔只做「身分 → 讀範圍 / 寫入閘 / 晉升」這層薄治理 + 參數打包。
"""
from __future__ import annotations

from typing import List, Optional

from .engine import Engine
from .mcp_principal import (
    GLOBAL_USER_ID, Principal, apply_write_trust, current_principal, register_write,
)
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
區間是傾向不是真值,鄰居越少越寬。讀範圍依身分:member 見 global + 自己的 self;純讀 token 見 global。"""

LOG_DESC = """寫回一筆校準(寫工具)。三層寫入:
  - HTTP member(具命名空間 token):寫入**強制落自己的 self 客製層**(指定 global / 他人 ns
    會被改寫回自有並回 note);grade 上限 **B**(A 級客觀真值須由 owner 在本機晉升);拒 prediction。
  - 本機 stdio owner:可寫 global 客觀層或任一 self;A 級須附 protocol(如 SCA_cupping)。
寫入完整性鐵則(套用於所有人,擋失誤):
  - grade=prediction 為內部保留級(引擎自身預測),**不得當人類真值注入**(注入即拒收)。
  - A 級(人類感官真值)須附 protocol,否則 engine 拒收。
回傳 {"ok":bool,...};被閘擋下時 ok=False 並說明;經 confinement / 降級時附 trust_notes。"""

SWAP_DESC = """換泡法推味道(讀工具,純物理先驗)。跨機制僅定性、標高不確定
(物理軸不足以涵蓋壓力/流動動力學);同機制較可信。請配合 predict() 在目標機制重新預測。"""

LIST_CUSTOM_DESC = """列出待審的個人客製記錄(self 客製層,非 global)。**owner / stdio 限定**。
供晉升審查:逐筆檢視 member / 你自己累積的 self 校準,決定哪些值得升格為 global 客觀真值。
可選 user_id 過濾單一命名空間。預設不動作=留個人客製(晉升是刻意行為)。"""

PROMOTE_DESC = """把一筆個人客製記錄(self 層)晉升為 global 客觀真值。**owner / stdio 限定**。
就地改寫該記錄 user_id→global、套用 global 鐵則:grade 須 A 或 B,A 級須附 protocol(如 SCA_cupping)。
這是 self→global 的唯一通道(網路面永遠寫不到 global)。回傳 {"ok":bool, promoted_id, ...}。"""


def _bean(origin="", variety="", process="other", roast_agtron=None) -> BeanRoast:
    return BeanRoast(
        origin=origin, variety=variety,
        process=Process(process if process in Process._value2member_map_ else "other"),
        roast_agtron=roast_agtron,
    )


def _iter_all_records(engine: Engine):
    """全量列舉真相記錄(晉升審查用)。memory/Qdrant 走 store.iter_records;
    Vectorize(無 iter_records)走 canonical sink。皆無 → 空。"""
    store = engine.store
    if hasattr(store, "iter_records"):
        yield from store.iter_records()
    elif engine.canonical is not None:
        yield from engine.canonical.iter_records()


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
    """query_flavor_map 的邏輯。讀範圍由 principal 決定(§16.3):
    member=[global,自己]、reader=[global]、owner=None(不過濾)。"""
    mech = BrewMechanism(brew_mechanism)
    bean = _bean(origin, variety, process, roast_agtron)
    scope = principal.read_user_ids  # None=不過濾(owner);否則 global + 自己(member)/ 只 global(reader)

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
    """log_calibration 的邏輯:過寫入信任閘(member confinement + grade≤B,§16.2)+ 流量閘,
    再交 engine(A 須 protocol 等單一真相把關)。"""
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
    if not register_write(principal):  # 流量閘:防公開端被灌爆(owner 豁免)
        return {"ok": False, "gate": "rate_limit",
                "error": "寫入次數已達上限,請稍後再試(防灌爆)。"}
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


# ────────────────────────────── 晉升邏輯(owner / stdio 限定) ──────────────────────────────

def do_list_customizations(
    engine: Engine,
    principal: Principal,
    *,
    user_id: str = "",
    limit: int = 50,
) -> dict:
    """list_customizations 的邏輯:列 self 客製層(非 global、非 prediction)待審記錄。
    owner 限定(防禦縱深;工具本就不在 HTTP 註冊)。"""
    if principal.role != "owner":
        return {"ok": False, "gate": "promote",
                "error": "晉升審查只在本機 owner(stdio)門;此通道不可。"}
    want = user_id.strip()
    out: List[dict] = []
    for r in _iter_all_records(engine):
        if r.user_id == GLOBAL_USER_ID or r.grade == Grade.PREDICTION:
            continue
        if want and r.user_id != want:
            continue
        out.append({
            "id": r.id, "user_id": r.user_id, "grade": r.grade.value,
            "mechanism": r.params.brew_mechanism.value, "method": r.params.method,
            "origin": r.bean.origin, "roast_band": r.bean.roast_band(),
            "flavor": r.flavor.axis_vector(),
        })
        if len(out) >= limit:
            break
    return {"ok": True, "count": len(out), "customizations": out,
            "note": "預設不動作=留個人客製;用 promote_customization 升格為 global。"}


def do_promote_customization(
    engine: Engine,
    principal: Principal,
    *,
    record_id: str,
    grade: str = "A",
    protocol: str = "",
) -> dict:
    """promote_customization 的邏輯:把 self 記錄就地晉升為 global 客觀真值。
    owner 限定。套 global 鐵則:grade∈{A,B},A 須 protocol(交 engine 把關)。

    就地改寫:沿用原 record id(同 id upsert 覆寫 → self 升格為 global,非重複);
    雙寫 canonical(append;rebuild 同 id 後寫者勝 → 還原為 global)。"""
    if principal.role != "owner":
        return {"ok": False, "gate": "promote",
                "error": "晉升只在本機 owner(stdio)門;此通道不可(網路面永遠寫不到 global)。"}
    try:
        target = Grade(grade)
    except ValueError:
        return {"ok": False, "error": f"未知 grade: {grade};晉升目標須 A 或 B。"}
    if target not in (Grade.A, Grade.B):
        return {"ok": False, "error": f"晉升目標 grade 須 A 或 B(收到 {target.value})。"}

    src: Optional[Record] = None
    for r in _iter_all_records(engine):
        if r.id == record_id:
            src = r
            break
    if src is None:
        return {"ok": False, "error": f"找不到記錄 {record_id}(或後端不支援列舉)。"}
    if src.user_id == GLOBAL_USER_ID:
        return {"ok": False, "error": "該記錄已是 global,無需晉升。"}

    promoted = src.model_copy(update={
        "user_id": GLOBAL_USER_ID, "grade": target, "protocol": protocol,
    })
    out = engine.log_calibration(promoted)  # A 須 protocol 由 engine 單一把關
    if isinstance(out, dict) and out.get("ok"):
        return {**out, "promoted_id": promoted.id, "from_user_id": src.user_id,
                "to_grade": target.value,
                "note": f"已將 {record_id} 從 self 層 '{src.user_id}' 晉升為 global({target.value})。"}
    return out


# ────────────────────────────── MCP 註冊(薄包裝) ──────────────────────────────

def register_tools(mcp, engine: Engine, *, include_writes: bool = True,
                   include_promotion: bool = False) -> None:
    """把 do_* 註冊成 MCP 工具。工具讀 contextvar 取當前 principal:
    HTTP 由認證中介層設定(member / reader);stdio 未設定 → LOCAL_PRINCIPAL(owner,零回歸)。

    include_writes(預設 True):掛 `log_calibration`(member 受限寫 / owner 自由寫)。
    include_promotion(預設 False):掛 `list_customizations` / `promote_customization`
        ——**只在 stdio owner 門**(`mcp_server.py` 傳 True);HTTP 不掛 → 網路無 global 寫入 /
        晉升路徑(§16「三層」)。
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

    # 寫工具:HTTP member 受限寫(confinement + grade≤B)/ stdio owner 自由寫。
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

    # 晉升工具:**只在 stdio owner 門**註冊;HTTP 不掛 → 網路無 self→global 晉升路徑。
    if include_promotion:
        @mcp.tool(description=LIST_CUSTOM_DESC)
        def list_customizations(user_id: str = "", limit: int = 50) -> dict:
            return do_list_customizations(engine, current_principal(),
                                          user_id=user_id, limit=limit)

        @mcp.tool(description=PROMOTE_DESC)
        def promote_customization(record_id: str, grade: str = "A", protocol: str = "") -> dict:
            return do_promote_customization(engine, current_principal(),
                                            record_id=record_id, grade=grade, protocol=protocol)

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
