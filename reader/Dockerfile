FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest app from GitHub over HTTPS on start;
# tzdata for the Pacific/Auckland weekly refresh schedule.
RUN apk add --no-cache ca-certificates tzdata
RUN pip install --no-cache-dir feedparser
COPY reader-proxy.py index.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8774
CMD ["./run.sh"]
