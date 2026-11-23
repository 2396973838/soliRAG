# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn

COPY fake_dify.py ./

EXPOSE 5001
CMD ["uvicorn", "fake_dify:app", "--host", "0.0.0.0", "--port", "5001"]
