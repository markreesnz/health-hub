FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest dashboard from GitHub over HTTPS on start.
RUN apk add --no-cache ca-certificates
COPY server.py mti_blocks.py index.html config.json program.json run.sh ./
COPY mti/ ./mti/
RUN chmod +x run.sh
EXPOSE 8768
CMD ["./run.sh"]
