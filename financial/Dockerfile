FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest dashboard from GitHub over HTTPS on start.
RUN apk add --no-cache ca-certificates
COPY akahu-proxy.py financial-plan-dashboard.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8765
CMD ["./run.sh"]
