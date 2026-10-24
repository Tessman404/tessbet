FROM python:3.12-slim
ENV TZ=Europe/Paris PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY engine.py server.py live.py config.json start.py ./
COPY web ./web
CMD ["python3", "start.py"]
