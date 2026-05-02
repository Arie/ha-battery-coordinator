FROM python:3.12-slim

WORKDIR /app
COPY battery-coordinator/app/ ./
RUN pip install --no-cache-dir 'aiohttp>=3.9,<4'

# Default: respect DRY_RUN env (false → live). Pass --live as an arg to
# force live mode regardless of env. Set DRY_RUN=true in your .env to
# observe-only.
CMD ["python", "main.py"]
