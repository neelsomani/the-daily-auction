FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git rsync \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir awscli==1.32.110

COPY app /app/app
COPY scripts /app/scripts

EXPOSE 9090

CMD ["python", "/app/app/server.py"]
