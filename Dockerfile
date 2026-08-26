FROM python:3.11-slim

WORKDIR /app

# Системные зависимости не нужны — все пакеты в requirements.txt чистый Python/wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Структура важна: backend/app/config.py вычисляет PROJECT_ROOT как
# parent.parent.parent от своего файла и резолвит относительные пути
# (./data/..., sqlite:///./data/...) от него — поэтому раскладка каталогов
# внутри контейнера должна повторять раскладку репозитория 1:1.
COPY backend/ backend/
COPY frontend/ frontend/
COPY data/ data/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"

# --app-dir делает backend/ корнем для импорта пакета app.*, при этом
# рабочая директория процесса остаётся /app, что не важно: все пути в
# config.py абсолютны (резолвятся от __file__), не от cwd.
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
