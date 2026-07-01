FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest dashboard from GitHub over HTTPS on start.
RUN apk add --no-cache ca-certificates
# websocket-client: the proxy reads HA recorder statistics over the WebSocket API.
RUN pip install --no-cache-dir websocket-client
COPY energy-proxy.py index.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8766
CMD ["./run.sh"]
