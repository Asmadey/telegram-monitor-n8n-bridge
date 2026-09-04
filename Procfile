# Два процесса из одного образа (задача 4.1). Монолит server.py здесь не
# запускается никогда: ~40 эндпоинтов без auth (К2) — см. tests/test_70_deploy.py.
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m app.worker
