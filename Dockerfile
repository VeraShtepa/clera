FROM python:3.12-slim

WORKDIR /clera

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Создаем папку для базы данных (вместо директивы VOLUME)
RUN mkdir -p /clera/data

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]
