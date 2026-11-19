FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest page from GitHub over HTTPS on start.
# tzdata so "today" follows HA's timezone (TZ) rather than UTC.
RUN apk add --no-cache ca-certificates tzdata
COPY server.py index.html run.sh ./
COPY arete ./arete
RUN chmod +x run.sh
EXPOSE 8770
CMD ["./run.sh"]
