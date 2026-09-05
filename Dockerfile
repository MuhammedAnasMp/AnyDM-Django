FROM python:3.11

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    default-libmysqlclient-dev \
    pkg-config \
    build-essential \
    git

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Convert Windows line endings (CRLF) to Unix (LF) and grant execution permissions
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["daphne", "-b", "0.0.0.0", "-p", "8005", "core.asgi:application"]