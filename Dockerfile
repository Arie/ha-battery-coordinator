FROM python:3.12-slim

WORKDIR /app
COPY battery-coordinator/app/ ./
RUN pip install --no-cache-dir aiohttp

CMD ["python", "main.py", "--live"]
