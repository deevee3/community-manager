FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=America/New_York

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

VOLUME ["/app/data"]

CMD ["python", "-m", "bot"]
