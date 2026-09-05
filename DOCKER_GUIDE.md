# Docker & Production Performance Tuning Guide

This guide details the production architecture tuned for high-performance hardware (**Intel Core i7-12700 20 threads, 32 GB RAM**).

---

## 🚀 Production Services Allocation

| Service Name | Runner / Workers | Assigned Port / Resources | Purpose |
| :--- | :--- | :--- | :--- |
| **`web`** | Gunicorn + 4 Uvicorn Workers | `8005` | General API & Dashboard Requests |
| **`webhook`** | Gunicorn + 4 Uvicorn Workers | `8006` | High-volume Instagram/Meta Webhook Ingestion |
| **`celery`** | Celery Worker (`concurrency=8`) | 8 CPU Threads | Parallel Background Job & DM Processing |
| **`celery-beat`** | Celery Beat Scheduler | 1 Process | Scheduled Periodic Tasks |
| **`redis`** | Redis 7 (`maxmemory 2GB`) | `6379` | In-memory Cache & High-Speed Queue |
| **`db`** | MySQL 8 (`innodb_buffer_pool=2G`) | `3306` (max 250 connections) | Primary Relational Storage |
| **`nginx`** | Nginx Reverse Proxy (Upstream) | `8080` (`80`) | Upstream Load Balancer & Buffer Optimizer |
| **`cloudflared`** | Cloudflare Tunnel Client | Tunnel | Secure Public Tunneling |

---

## 🛠️ Commands Cheatsheet

### 1. Build and Start Production Containers
```bash
cd backend
docker-compose up -d --build
```

### 2. View Live Performance Logs
```bash
docker-compose logs -f web webhook celery
```

### 3. Check Container Health & Memory Usage
```bash
docker stats
```

---

## 🌐 Production Nginx Routing

- `http://127.0.0.1:8005`: Main API Backend (`web`)
- `http://127.0.0.1:8006`: Webhook Backend (`webhook`)
- `http://127.0.0.1:8080`: Nginx Unified Reverse Proxy




docker-compose up -d --build