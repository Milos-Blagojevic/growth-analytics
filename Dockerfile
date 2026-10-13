# Dashboard image for Railway (or any Docker host). The code lives in /app; your data (profile.toml, cv.md,
# companies.csv, out/, hunt.db, ...) lives in /data, which must be a persistent volume. See README "Deploy to Railway".
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Listens on $PORT (set by Railway; default 8000). Refuses to start without DASHBOARD_PASSWORD.
CMD ["python", "-m", "dashboard", "--root", "/data", "--host", "0.0.0.0"]
