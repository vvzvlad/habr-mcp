FROM python:3.11-slim

WORKDIR /app

# Dependencies as a separate layer: change less often than code → cached better
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime state directory (per-token credential store lives here)
RUN mkdir -p data

# Code (this project ships no templates/ or static/ assets)
COPY src/ src/
COPY main.py .

# No EXPOSE: the service is published by Traefik via docker-compose labels.

CMD ["python", "main.py"]
