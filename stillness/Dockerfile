FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest page from GitHub over HTTPS on start.
RUN apk add --no-cache ca-certificates
COPY server.py index.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8770
CMD ["./run.sh"]
