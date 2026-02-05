FROM python:3.11-slim

WORKDIR /app

COPY services/slack_notifier/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY shared /app/shared
COPY services/slack_notifier /app/services/slack_notifier

ENV PYTHONPATH=/app
ENV PORT=8000

WORKDIR /app/services/slack_notifier
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]