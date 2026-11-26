FROM python:3.11-slim-bookworm

# 基础环境与 Chromium 安装
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MUSE2API_HOME=/app \
    MUSE2API_HOST=0.0.0.0 \
    MUSE2API_PORT=18610 \
    MUSE2API_CHROMIUM=/usr/bin/chromium \
    MUSE2API_CDP_PORT=19210 \
    # patch22r：镜像部署拒绝容器内 git 升级（/admin/update/now 整树覆盖
    # 二开 = 2026-11-16 事故的显式按钮版，升级一律走 build + compose up）
    MUSE2API_MANAGED_DEPLOYMENT=1

# 上游 1.6.11：镜像构建溯源（合并 1.6.11 时并入）
ARG BUILD_SHA=
ENV MUSE2API_BUILD_SHA=$BUILD_SHA
LABEL org.opencontainers.image.revision=$BUILD_SHA

WORKDIR /app

# 国内镜像源加速（腾讯轻量内网直连，部署机本地化修改；恢复官方源用 Dockerfile.bak-mirror）
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true
RUN pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple

# 安装 Chromium、中文字体与核心依赖（上游 1.6.11 新增 ffmpeg：视频缩略图）
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    ffmpeg \
    fonts-wqy-zenhei \
    fonts-freefont-ttf \
    procps \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码与必要目录（二开模块 tetherpool/mediabak/idempotency + 自动登录 credentials/auto_login + 上游新模块 generation_timing/thumbnails）
COPY admin.html status.html remote_login.html login_session.py credentials.py auto_login.py app.py cdp.py config.py engine.py store.py proxy.py tetherpool.py mediabak.py idempotency.py updater.py generation_timing.py thumbnails.py version.json ./
COPY deploy/ ./deploy/
COPY extension/ ./extension/
COPY tools/ ./tools/
COPY ui/ ./ui/

# 创建持久化数据目录
RUN mkdir -p /app/data/media /app/data/downloads /root/muse2api-profiles

VOLUME ["/app/data"]

EXPOSE 18610

# patch22r：镜像级健康检查（与 compose healthcheck 双保险）
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD curl -sf http://127.0.0.1:18610/healthz || exit 1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18610"]
