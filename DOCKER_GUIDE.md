# Docker & Service Management Guide

This guide details how to build, run, deploy code updates, handle migrations, and manage backend services using Docker Compose.

---

## 🚀 Services Overview

| Service Name | Command / Process | Port | Purpose |
| :--- | :--- | :--- | :--- |
| **`web`** | `daphne -b 0.0.0.0 -p 8005 core.asgi:application` | `8005` | General Backend API & Dashboard Services (Runs Migrations) |
| **`webhook`** | `daphne -b 0.0.0.0 -p 8006 core.asgi:application` | `8006` | Dedicated Webhook Receiver (Instagram/Meta) |
| **`celery`** | `celery -A core worker --loglevel=info` | - | Background Task Execution Worker |
| **`celery-beat`** | `celery -A core beat --loglevel=info` | - | Scheduled / Periodic Task Runner |
| **`redis`** | `redis:7` | `6379` | In-memory Cache & Celery Message Broker |
| **`db`** | `mysql:8` | `3306` | Primary MySQL Database |
| **`nginx`** | `nginx:latest` | `8080` | Reverse Proxy Server |
| **`cloudflared`** | `cloudflare/cloudflared:latest` | - | Cloudflare Tunnel Client |

---

## 🔄 Code Deployment Workflow (`git pull` & Migrations)

### Standard Deployment Step:
```bash
# 1. Pull latest code updates from Git repository
git pull origin main

# 2. Rebuild images and start containers (Runs database migrations automatically)
docker-compose up -d --build
```

---

## ⚡ Database Migrations

### Automatic Migrations:
Database migrations are automatically executed by `docker-entrypoint.sh` when the `web` container boots up, enabled via `RUN_MIGRATIONS=true` in `docker-compose.yml`.

### Manual Migration Commands (If needed):
```bash
# Generate new migration files
docker-compose exec web python manage.py makemigrations

# Apply migrations to database
docker-compose exec web python manage.py migrate
```

---

## 🛠️ Common Docker Commands

### 1. Build and Start All Services
```bash
docker-compose up -d --build
```

### 2. Restart Services
- **Restart all services**:
  ```bash
  docker-compose restart
  ```
- **Restart API and Webhook services**:
  ```bash
  docker-compose restart web webhook
  ```

### 3. Check Running Containers
```bash
docker-compose ps
```

### 4. View Real-time Logs
- **View logs for all containers**:
  ```bash
  docker-compose logs -f
  ```
- **View logs for API and Webhook services**:
  ```bash
  docker-compose logs -f web webhook
  ```

### 5. Stop Containers
- **Stop containers (keep data & volumes)**:
  ```bash
  docker-compose stop
  ```
- **Stop and remove containers**:
  ```bash
  docker-compose down
  ```

---

## 🌐 Endpoint Routing Quick Reference

- **General API (`api.locanydm.online`)**: Point Nginx / Proxy to `http://127.0.0.1:8005`
- **Instagram Webhooks (`wb.locanydm.online`)**: Point Nginx / Proxy to `http://127.0.0.1:8006`
