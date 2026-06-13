# CIE remote MCP(streamable-http)— host-agnostic 容器。
# 任何能跑容器的平台皆可:Cloud Run(本輪生產目標)/ Fly / Railway / Render / VPS。不綁單一供應商。
#
# 建置:  docker build -t cie-mcp .
# 本地跑:docker run --rm -p 8000:8000 --env-file .env cie-mcp   # 未注入 $PORT → 預設 8000
# 連接:  https://<host>/mcp?token=<CIE_MCP_AUTH_TOKEN>(claude.ai 自訂連接器)
#
# 生產(本輪):記憶體自幹 index + D1 共用 canonical + Workers AI 嵌入;runtime 僅需
# stdlib+pydantic+REST(嵌入 / canonical 走 REST),numpy/qdrant 撐記憶體向量庫,映像仍輕。
FROM python:3.12-slim

# **不**硬寫 CIE_MCP_PORT:Cloud Run 等平台以 $PORT(預設 8080)注入監聽埠,cie/config.py
# 以 `CIE_MCP_PORT or $PORT or 8000` coalesce。硬寫 CIE_MCP_PORT 會蓋掉 $PORT → 容器監聽
# 錯埠、Cloud Run 路由不到。本地 docker run 未注入 $PORT 時自然退回 8000。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CIE_MCP_HOST=0.0.0.0

WORKDIR /app

# 先裝依賴(層快取);requirements 變動才重裝。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再放原始碼。
COPY . .

# 非 root 跑(最小權限)。
RUN useradd --create-home --uid 10001 cie && chown -R cie:cie /app
USER cie

# 文件用途;實際埠由 CIE_MCP_PORT / $PORT 決定(server_http 讀 CONFIG)。
EXPOSE 8000

# server_http.py 的 __main__ 以 CONFIG.mcp_host/mcp_port 起 uvicorn;
# CIE_MCP_PORT 未設時自動採平台注入的 $PORT(見 cie/config.py)。
CMD ["python", "server_http.py"]
