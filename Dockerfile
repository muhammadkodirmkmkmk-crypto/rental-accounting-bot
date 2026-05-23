FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir -r telegram-bot/requirements.txt
CMD ["python", "telegram-bot/main.py"]
