FROM python:3.12-slim

RUN groupadd -r bot && useradd -r -g bot -d /app bot \
    && mkdir -p /data/files && chown -R bot:bot /data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R bot:bot /app

USER bot

CMD ["python", "app.py"]
