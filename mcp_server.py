"""CIE MCP server — 暴露三支工具給模型(設計 §6)。

工具:
  query_flavor_map     查相似情境 → 推薦 / 預測 / 診斷
  log_calibration      寫回一筆校準
  predict_method_swap  換泡法推味道

執行:  python mcp_server.py   (stdio transport)

注意:記憶體向量庫不跨行程持久化。上線請設 CIE_QDRANT_URL 指向 Qdrant Cloud,
否則每次啟動需重新 seed。
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from cie.engine import Engine
from cie.schema import BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record
from cie.seed import seed as seed_store

mcp = FastMCP("coffee-intuition-engine")
_engine = Engine()

# 開發便利:啟動時若庫空,自動灌種子。
try:
    if _engine.store.count() == 0:
        seed_store(_engine.store)
except Exception:  # pragma: no cover
    pass


def _bean(origin="", variety="", process="other", roast_agtron=None) -> BeanRoast:
    return BeanRoast(origin=origin, variety=variety,
                     process=Process(process if process in Process._value2member_map_ else "other"),
                     roast_agtron=roast_agtron)


@mcp.tool()
def query_flavor_map(
    brew_mechanism: str,
    mode: str = "recommend",
    origin: str = "",
    variety: str = "",
    process: str = "other",
    roast_agtron: Optional[float] = None,
    # predict 模式用的參數
    water_temp_c: Optional[float] = None,
    brew_ratio: Optional[float] = None,
    grind_um: Optional[float] = None,
    contact_time_s: Optional[float] = None,
    tds_pct: Optional[float] = None,
    ey_pct: Optional[float] = None,
    # diagnose 模式用
    defect: str = "",
) -> dict:
    """查相似情境並依 mode 輸出。

    brew_mechanism: immersion | percolation | pressure(必填,硬分區鍵)
    mode: recommend(起手參數) | predict(預測風味) | diagnose(問題歸因)
    """
    mech = BrewMechanism(brew_mechanism)
    bean = _bean(origin, variety, process, roast_agtron)

    if mode == "recommend":
        return _engine.recommend(bean, mech)
    if mode == "predict":
        params = BrewParams(brew_mechanism=mech, water_temp_c=water_temp_c,
                            brew_ratio=brew_ratio, grind_um=grind_um,
                            contact_time_s=contact_time_s, tds_pct=tds_pct, ey_pct=ey_pct)
        return _engine.predict(bean, params)
    if mode == "diagnose":
        return _engine.diagnose(mech, defect or "未指定", bean)
    return {"error": f"未知 mode: {mode};可用 recommend|predict|diagnose"}


@mcp.tool()
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
    flavor_notes: Optional[list] = None,
    user_id: str = "self",
) -> dict:
    """寫回一筆校準。A 級須附 protocol(人類感官真值來源)。"""
    record = Record(
        bean=_bean(origin, variety, process, roast_agtron),
        params=BrewParams(brew_mechanism=BrewMechanism(brew_mechanism), method=method,
                          water_temp_c=water_temp_c, brew_ratio=brew_ratio, grind_um=grind_um,
                          contact_time_s=contact_time_s, tds_pct=tds_pct, ey_pct=ey_pct),
        flavor=FlavorProfile(acidity=acidity, sweetness=sweetness, bitterness=bitterness,
                             body=body, aftertaste=aftertaste, balance=balance, clarity=clarity,
                             flavor_notes=flavor_notes or []),
        grade=Grade(grade), protocol=protocol, user_id=user_id,
    )
    return _engine.log_calibration(record)


@mcp.tool()
def predict_method_swap(
    to_mechanism: str,
    from_mechanism: str,
    to_method: str = "",
    origin: str = "",
    process: str = "other",
    roast_agtron: Optional[float] = None,
) -> dict:
    """換泡法推味道。跨機制標高不確定。"""
    return _engine.method_swap(
        bean=_bean(origin, "", process, roast_agtron),
        from_params=BrewParams(brew_mechanism=BrewMechanism(from_mechanism)),
        to_mechanism=BrewMechanism(to_mechanism), to_method=to_method,
    )


if __name__ == "__main__":
    mcp.run()
