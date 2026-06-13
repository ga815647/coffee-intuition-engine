# CIE remote MCP(streamable-http)— host-agnostic 容器。
# 任何能跑容器的平台皆可:Fly / Railway / Render / Cloud Run / VPS。不綁單一供應商。
#
# 建置:  docker build -t cie-mcp .
# 跑:    docker run --rm -p 8000:8000 --env-file .env cie-mcp
# 連接:  https://<host>/mcp?token=<CIE_MCP_AUTH_TOKEN>(claude.ai 自訂連接器)
#
# 嵌入器恆 workers_ai + 向量庫 vectorize 時,runtime 僅需 stdlib+pydantic+REST(無重 ML);
# numpy/qdrant 仍裝以支援本地 / Qdrant 後端,映像仍輕。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CIE_MCP_HOST=0.0.0.0 \
    CIE_MCP_PORT=8000

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
