# Два процесса из одного образа (задача 4.1). Монолит удалён задачей 7.4;
# что именно здесь запускается, сторожит tests/test_70_deploy.py.
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m app.worker
