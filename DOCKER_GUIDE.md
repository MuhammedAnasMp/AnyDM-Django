# Docker & Service Management Guide

This guide details how to build, run, and manage backend services using Docker Compose.

---

## 🚀 Services Overview

| Service Name | Command / Process | Port | Purpose |
| :--- | :--- | :--- | :--- |
| **`web`** | `daphne -b 0.0.0.0 -p 8005 core.asgi:application` | `8005` | General Backend API & Dashboard Services |
| **`webhook`** | `daphne -b 0.0.0.0 -p 8006 core.asgi:application` | `8006` | Dedicated Webhook Receiver (Instagram/Meta) |
| **`celery`** | `celery -A core worker --loglevel=info` | - | Background Task Execution Worker |
| **`celery-beat`** | `celery -A core beat --loglevel=info` | - | Scheduled / Periodic Task Runner |
| **`redis`** | `redis:7` | `6379` | In-memory Cache & Celery Message Broker |
| **`db`** | `mysql:8` | `3306` | Primary MySQL Database |
| **`nginx`** | `nginx:latest` | `8080` | Reverse Proxy Server |
| **`cloudflared`** | `cloudflare/cloudflared:latest` | - | Cloudflare Tunnel Client |

---

## 🛠️ Common Commands

### 1. Build and Start All Services
```bash
docker-compose up -d --build
```

### 2. Restart Services
- **Restart all services**:
  ```bash
  docker-compose restart
  ```
- **Restart only API and Webhook services**:
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
