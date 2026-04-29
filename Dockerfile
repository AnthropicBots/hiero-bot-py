FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt --break-system-packages 2>/dev/null || \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runner
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .
RUN addgroup --system botgroup && adduser --system --ingroup botgroup botuser
USER botuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
