FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest app from GitHub on start.
RUN apk add --no-cache ca-certificates
COPY server.py index.html run.sh ./
COPY maps/ ./maps/
RUN chmod +x run.sh
EXPOSE 8776
CMD ["./run.sh"]
