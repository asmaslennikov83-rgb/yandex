FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data PORT=8000
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py core.py mini_app.py list_devices.py ./
COPY web ./web
EXPOSE 8000
CMD ["python", "bot.py"]
