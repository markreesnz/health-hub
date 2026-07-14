FROM python:3.12-alpine
WORKDIR /app
# ca-certificates so run.sh can pull the latest app from GitHub over HTTPS on start.
RUN apk add --no-cache ca-certificates
COPY books-proxy.py index.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8772
CMD ["./run.sh"]
