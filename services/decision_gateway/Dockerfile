FROM python:3.11-slim

WORKDIR /app

COPY services/decision_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY shared /app/shared
COPY services/decision_gateway /app/services/decision_gateway

ENV PYTHONPATH=/app
ENV PORT=8000

WORKDIR /app/services/decision_gateway
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]