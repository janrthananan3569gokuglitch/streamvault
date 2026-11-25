FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir --upgrade fastapi uvicorn httpx yt-dlp

COPY app.py /app/app.py

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
