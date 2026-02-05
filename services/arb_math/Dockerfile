FROM python:3.11-slim

WORKDIR /app

COPY services/arb_math/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY shared /app/shared
COPY services/arb_math /app/services/arb_math

ENV PYTHONPATH=/app
ENV PORT=8000

WORKDIR /app/services/arb_math
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]