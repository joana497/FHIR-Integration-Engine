FROM python:3.12-slim

WORKDIR /app

# NOTE: no gcc / libpq-dev needed — requirements.txt installs
# psycopg2-binary, which ships a pre-compiled wheel. Removing the
# build toolchain shrinks the image by roughly 150 MB.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# No --reload: auto-reload is a development convenience that watches the
# filesystem and restarts on change; in a container it wastes CPU and can
# mask import-time errors. For local development, override the command:
#   docker compose run python-fhir-engine uvicorn main:app --host 0.0.0.0 --port 8000 --reload
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
