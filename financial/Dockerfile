FROM python:3.12-alpine
WORKDIR /app
COPY akahu-proxy.py financial-plan-dashboard.html run.sh ./
RUN chmod +x run.sh
EXPOSE 8765
CMD ["./run.sh"]
