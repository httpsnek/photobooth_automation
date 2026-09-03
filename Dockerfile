# For demos / hosted previews only. Real booths run natively on Windows.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PAYMENT_PROVIDER=mock \
    BOOTH_NAME=InstaBOX \
    PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
