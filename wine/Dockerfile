FROM python:3.12-alpine
WORKDIR /app
RUN apk add --no-cache ca-certificates tzdata
RUN pip install --no-cache-dir requests beautifulsoup4 lxml
COPY server.py index.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8775
CMD ["./run.sh"]
