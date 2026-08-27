FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY eventbot/ ./eventbot/
RUN pip install --no-cache-dir -e .

ENV DATA_DIR=/data
EXPOSE 8080

CMD ["uvicorn", "eventbot.main:app", "--host", "0.0.0.0", "--port", "8080"]
