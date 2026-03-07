# LLM Gateway 部署指南

## Prerequisites

- Python 3.12+（方法一）或 Docker（方法二）
- PostgreSQL
- Nginx（透過 `systemctl` 管理）
- AuthCenter RS256 public key（`keys/public.pem`）

---

## 方法一：systemd user service + Nginx

使用 user-level systemd 執行，不需要 root（nginx 設定除外）。

### 首次部署

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway

# 建立並編輯設定檔
cp .env.example .env && nano .env
cp config.toml.example config.toml && nano config.toml

# 放置 AuthCenter 公鑰
mkdir -p keys && cp /path/to/public.pem keys/public.pem

# 如需 Proxy，先設定環境變數
export http_proxy=http://proxy.company.com:8080

# 執行部署
bash deploy/setup.sh
```

腳本會自動：
1. rsync 程式碼到 `~/llm-gateway`（排除 `.env`、`config.toml`、`keys/`）
2. 建立 venv 並安裝依賴
3. 設定 user-level systemd service
4. 啟用 lingering（登出後服務持續運行）
5. 設定 nginx reverse proxy

### 更新程式碼

```bash
cd /path/to/llm-gateway   # 原始 clone 目錄
git pull

# 重新執行部署腳本（會 rsync + 更新依賴 + 重啟服務）
bash deploy/setup.sh
```

如果只改了程式碼、沒有新增依賴，也可以手動更新：

```bash
cd /path/to/llm-gateway
git pull
rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    --exclude='config.toml' --exclude='.env' --exclude='keys/' \
    ./ ~/llm-gateway/
systemctl --user restart llm-gateway
```

> `config.toml`、`.env`、`keys/` 不會被覆蓋。

### 服務管理

```bash
systemctl --user status llm-gateway     # 查看狀態
systemctl --user restart llm-gateway    # 重啟
systemctl --user stop llm-gateway       # 停止
journalctl --user -u llm-gateway -f     # 查看日誌
```

### 相關檔案

- [setup.sh](setup.sh) — 部署腳本
- [llm-gateway.service](llm-gateway.service) — systemd unit file
- [llm-gateway.nginx.conf](llm-gateway.nginx.conf) — nginx 設定

---

## 方法二：Docker Compose + Nginx

使用 Docker Compose 同時啟動 Gateway 和 PostgreSQL。

### 首次部署

```bash
git clone https://github.com/bwinken/llm-gateway.git
cd llm-gateway

# 建立並編輯設定檔
cp .env.example .env && nano .env
cp config.toml.example config.toml && nano config.toml

# 重要：Docker 環境下 DATABASE_URL 使用 'postgres' 而非 'localhost'
# DATABASE_URL=postgresql://llm_gateway:your_password@postgres:5432/llm_gateway

# 放置 AuthCenter 公鑰
mkdir -p keys && cp /path/to/public.pem keys/public.pem

# 如需 Proxy，先設定環境變數（會自動傳入 Docker build）
export http_proxy=http://proxy.company.com:8080

# 執行部署
bash deploy/setup-docker.sh
```

### 更新程式碼

```bash
cd /path/to/llm-gateway
git pull

# 重建 image 並重啟（有 layer cache，只改 code 很快）
docker compose up -d --build
```

> `config.toml` 和 `keys/` 是 volume mount，不受 image 重建影響。

### 服務管理

```bash
docker compose logs -f gateway          # 查看日誌
docker compose restart gateway          # 重啟
docker compose down                     # 停止
docker compose up -d --build            # 重建並啟動
```

### 相關檔案

- [setup-docker.sh](setup-docker.sh) — 部署腳本
- [../Dockerfile](../Dockerfile) — Docker image
- [../docker-compose.yml](../docker-compose.yml) — Compose 設定

---

## Nginx 設定

兩種方法都會自動安裝 nginx 設定。如需手動調整：

```bash
sudo nano /etc/nginx/sites-available/llm-gateway
sudo nginx -t && sudo systemctl reload nginx
```

SSL 憑證（可選）：

```bash
sudo certbot --nginx -d your-domain.com
```

---

## Proxy 設定

在 airgapped 環境下，部署前設定 proxy 環境變數：

```bash
export http_proxy=http://proxy.company.com:8080
```

- **systemd 方式**：`setup.sh` 會自動使用；如需 runtime proxy，取消 `llm-gateway.service` 中的 proxy 註解
- **Docker 方式**：`setup-docker.sh` 和 `docker-compose.yml` 會自動將 proxy 傳入 Docker build
