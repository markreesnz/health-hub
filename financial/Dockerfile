FROM python:3.12-alpine
WORKDIR /app
RUN apk add --no-cache ca-certificates tzdata
COPY akahu-proxy.py state_store.py financial-plan-dashboard.html app.js calculations.js migrations.js sync.js reference.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8765
CMD ["./run.sh"]
