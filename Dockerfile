FROM python:3.11-slim

WORKDIR /app

# Docker CLI + git 설치 (AWS 원격 제어 명령어용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    git \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY kitty/ kitty/

# 로그 디렉토리
RUN mkdir -p logs

CMD ["python", "-m", "kitty.main"]
