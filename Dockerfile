FROM python:3.11-slim

# Small, reproducible image. No build tools needed for the pure-Python deps.
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# The dashboard binds inside the container; publish it host-side in compose.
EXPOSE 4000

CMD ["python", "run.py"]
