FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    TZ=Pacific/Auckland \
    PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install mcp==1.30.0 uvicorn==0.52.4 starlette==1.6.0 sse-starlette==3.4.11 pydantic==2.13.5
COPY server/ /app/
COPY integration/ /integration/
COPY run.sh /run.sh
RUN chmod +x /run.sh && rm -f /app/test_*.py
WORKDIR /app
CMD ["/run.sh"]
