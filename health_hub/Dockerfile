FROM python:3.12-alpine
WORKDIR /app
COPY server.py mti_blocks.py index.html config.json program.json run.sh ./
COPY mti/ ./mti/
RUN chmod +x run.sh
EXPOSE 8768
CMD ["./run.sh"]
