FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest dashboard from GitHub over HTTPS on start.
# tzdata so the weekly digest scheduler can run on Pacific/Auckland time.
RUN apk add --no-cache ca-certificates tzdata
COPY server.py mti_blocks.py digest.py index.html config.json program.json food_reference.md run.sh ./
COPY mti/ ./mti/
RUN chmod +x run.sh
EXPOSE 8768
CMD ["./run.sh"]
