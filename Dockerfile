FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -r telegram-bot/requirements.txt
CMD ["python", "telegram-bot/main.py"]
