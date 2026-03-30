FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저 복사 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY kitty/ kitty/

# 로그 디렉토리
RUN mkdir -p logs

CMD ["python", "-m", "kitty.main"]
