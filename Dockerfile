# cv-tailor — one image, three entry points (SPEC §7.5).
# Serves the CLI (corpus ingestion + tailoring runs), the FastAPI backend, and
# pytest. No separate images for pipeline vs API — same codebase, same image.
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# No CMD — the entry point is specified per service in compose
# (cli: python -m ... / pytest; backend: uvicorn api.main:app).
