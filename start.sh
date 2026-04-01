#!/bin/bash
set -e

echo "[1/5] 최신 코드 가져오는 중..."
cd /home/ec2-user/kitty
git pull origin main

echo "[2/5] Secrets Manager에서 시크릿 가져오는 중..."
SECRET=$(aws secretsmanager get-secret-value \
  --secret-id kitty/prod \
  --region ap-northeast-2 \
  --query SecretString \
  --output text)

echo "[3/5] 환경변수 파일 생성 중..."
python3 -c "
import sys, json
data = json.loads(sys.argv[1])
lines = []
for k, v in data.items():
    lines.append(f'{k}={v}')
print('\n'.join(lines))
" "$SECRET" > /home/ec2-user/kitty/.env

cat >> /home/ec2-user/kitty/.env << 'ENVEOF'
AI_PROVIDER=openai
AI_MODEL=gpt-4o
TRADING_MODE=paper
MAX_BUY_AMOUNT=1000000
MAX_POSITION_SIZE=5000000
AWS_REGION=ap-northeast-2
ENVEOF

echo "[4/5] Docker 빌드 및 실행 중..."
docker stop kitty-trader 2>/dev/null || true
docker rm kitty-trader 2>/dev/null || true

docker build -t kitty-trader .

mkdir -p /home/ec2-user/kitty/feedback
mkdir -p /home/ec2-user/kitty/logs

docker run -d \
  --name kitty-trader \
  --restart unless-stopped \
  --env-file /home/ec2-user/kitty/.env \
  -v /home/ec2-user/kitty/logs:/app/logs \
  -v /home/ec2-user/kitty/feedback:/app/feedback \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/ec2-user/kitty:/host/kitty \
  kitty-trader

echo "[4.5/5] 모니터 서비스 빌드 및 실행 중..."
docker stop kitty-monitor 2>/dev/null || true
docker rm kitty-monitor 2>/dev/null || true

docker build -t kitty-monitor ./monitor

mkdir -p /home/ec2-user/kitty/monitor-data

# 텔레그램 자격증명 추출 (.env 삭제 전)
_TG_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' /home/ec2-user/kitty/.env | cut -d= -f2-)
_TG_CHAT=$(grep '^TELEGRAM_CHAT_ID=' /home/ec2-user/kitty/.env | cut -d= -f2-)

docker run -d \
  --name kitty-monitor \
  --restart unless-stopped \
  -v /home/ec2-user/kitty/logs:/logs:ro \
  -v /home/ec2-user/kitty/monitor-data:/data \
  -e TELEGRAM_BOT_TOKEN="$_TG_TOKEN" \
  -e TELEGRAM_CHAT_ID="$_TG_CHAT" \
  -p 8080:8080 \
  kitty-monitor

echo "[5/5] 시크릿 파일 삭제 중..."
rm -f /home/ec2-user/kitty/.env

echo "완료! 컨테이너 상태:"
docker ps
