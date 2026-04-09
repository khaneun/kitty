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

mkdir -p /home/ec2-user/kitty/feedback
mkdir -p /home/ec2-user/kitty/logs
mkdir -p /home/ec2-user/kitty/token_usage
mkdir -p /home/ec2-user/kitty/commands
mkdir -p /home/ec2-user/kitty/reports

# Docker build context가 feedback 파일을 읽을 수 있도록 권한 보정 (빌드 전 적용)
sudo chmod -R 644 /home/ec2-user/kitty/feedback/*.json 2>/dev/null || true
sudo chmod -R 644 /home/ec2-user/kitty/night-feedback/*.json 2>/dev/null || true

docker build -t kitty-trader .

# 빌드 후 미사용 이미지 레이어 정리 (디스크 누적 방지)
docker image prune -f

docker run -d \
  --name kitty-trader \
  --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 \
  --env-file /home/ec2-user/kitty/.env \
  -v /home/ec2-user/kitty/logs:/app/logs \
  -v /home/ec2-user/kitty/feedback:/app/feedback \
  -v /home/ec2-user/kitty/token_usage:/app/token_usage \
  -v /home/ec2-user/kitty/commands:/app/commands \
  -v /home/ec2-user/kitty/reports:/app/reports \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/ec2-user/kitty:/host/kitty \
  kitty-trader

echo "[4.5/5] 모니터 서비스 빌드 및 실행 중..."
docker stop kitty-monitor 2>/dev/null || true
docker rm kitty-monitor 2>/dev/null || true

docker build -t kitty-monitor ./monitor
docker image prune -f

mkdir -p /home/ec2-user/kitty/monitor-data
mkdir -p /home/ec2-user/kitty/reports

# 모니터 자격증명 추출 (.env 삭제 전)
_TG_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' /home/ec2-user/kitty/.env | cut -d= -f2-)
_TG_CHAT=$(grep '^TELEGRAM_CHAT_ID=' /home/ec2-user/kitty/.env | cut -d= -f2-)
_MON_PW=$(grep '^MONITOR_PASSWORD=' /home/ec2-user/kitty/.env | cut -d= -f2-)

echo "[4.6/5] kitty-night-trader 빌드 및 실행 중..."
docker stop kitty-night-trader 2>/dev/null || true
docker rm kitty-night-trader 2>/dev/null || true

docker build -t kitty-night-trader -f Dockerfile.night .
docker image prune -f

mkdir -p /home/ec2-user/kitty/night-logs
mkdir -p /home/ec2-user/kitty/night-feedback
mkdir -p /home/ec2-user/kitty/night-token_usage
mkdir -p /home/ec2-user/kitty/night-reports
mkdir -p /home/ec2-user/kitty/night-commands

# Night 환경변수 파일 생성 (NIGHT_ prefix 시크릿 + 공유 Telegram 토큰)
_NIGHT_AI_PROVIDER=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_AI_PROVIDER','openai'))" "$SECRET")
_NIGHT_AI_MODEL=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_AI_MODEL','gpt-4o'))" "$SECRET")
_NIGHT_KIS_APP_KEY=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_KIS_APP_KEY',''))" "$SECRET")
_NIGHT_KIS_APP_SECRET=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_KIS_APP_SECRET',''))" "$SECRET")
_NIGHT_KIS_ACCOUNT=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_KIS_ACCOUNT',''))" "$SECRET")
_NIGHT_OPENAI_API_KEY=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_OPENAI_API_KEY', d.get('OPENAI_API_KEY','')))" "$SECRET")
_NIGHT_ANTHROPIC_API_KEY=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_ANTHROPIC_API_KEY', d.get('ANTHROPIC_API_KEY','')))" "$SECRET")

_NIGHT_KIS_PAPER_KEY=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_KIS_PAPER_APP_KEY',''))" "$SECRET")
_NIGHT_KIS_PAPER_SECRET=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_KIS_PAPER_APP_SECRET',''))" "$SECRET")
_NIGHT_KIS_PAPER_ACCOUNT=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('NIGHT_KIS_PAPER_ACCOUNT_NUMBER',''))" "$SECRET")

cat > /home/ec2-user/kitty/.env.night << NIGHTEOF
NIGHT_TRADING_MODE=paper
NIGHT_AI_PROVIDER=${_NIGHT_AI_PROVIDER}
NIGHT_AI_MODEL=${_NIGHT_AI_MODEL}
NIGHT_KIS_APP_KEY=${_NIGHT_KIS_APP_KEY}
NIGHT_KIS_APP_SECRET=${_NIGHT_KIS_APP_SECRET}
NIGHT_KIS_ACCOUNT_NUMBER=${_NIGHT_KIS_ACCOUNT}
NIGHT_KIS_PAPER_APP_KEY=${_NIGHT_KIS_PAPER_KEY}
NIGHT_KIS_PAPER_APP_SECRET=${_NIGHT_KIS_PAPER_SECRET}
NIGHT_KIS_PAPER_ACCOUNT_NUMBER=${_NIGHT_KIS_PAPER_ACCOUNT}
OPENAI_API_KEY=${_NIGHT_OPENAI_API_KEY}
ANTHROPIC_API_KEY=${_NIGHT_ANTHROPIC_API_KEY}
TELEGRAM_BOT_TOKEN=${_TG_TOKEN}
TELEGRAM_CHAT_ID=${_TG_CHAT}
NIGHTEOF

docker run -d \
  --name kitty-night-trader \
  --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 \
  --env-file /home/ec2-user/kitty/.env.night \
  -v /home/ec2-user/kitty/night-logs:/app/night-logs \
  -v /home/ec2-user/kitty/night-feedback:/app/night-feedback \
  -v /home/ec2-user/kitty/night-token_usage:/app/night-token_usage \
  -v /home/ec2-user/kitty/night-reports:/app/night-reports \
  -v /home/ec2-user/kitty/night-commands:/app/night-commands \
  kitty-night-trader

# 모니터 실행 (night 볼륨 마운트 포함)
docker run -d \
  --name kitty-monitor \
  --restart unless-stopped \
  --log-opt max-size=5m --log-opt max-file=3 \
  -v /home/ec2-user/kitty/logs:/logs:ro \
  -v /home/ec2-user/kitty/feedback:/feedback:ro \
  -v /home/ec2-user/kitty/token_usage:/token_usage:ro \
  -v /home/ec2-user/kitty/commands:/commands \
  -v /home/ec2-user/kitty/monitor-data:/data \
  -v /home/ec2-user/kitty/night-logs:/night-logs:ro \
  -v /home/ec2-user/kitty/night-feedback:/night-feedback:ro \
  -v /home/ec2-user/kitty/night-token_usage:/night-token_usage:ro \
  -v /home/ec2-user/kitty/reports:/reports:ro \
  -v /home/ec2-user/kitty/night-reports:/night-reports:ro \
  -e TELEGRAM_BOT_TOKEN="$_TG_TOKEN" \
  -e TELEGRAM_CHAT_ID="$_TG_CHAT" \
  -e MONITOR_PASSWORD="$_MON_PW" \
  -p 8080:8080 \
  kitty-monitor

echo "[5/5] 시크릿 파일 삭제 중..."
rm -f /home/ec2-user/kitty/.env
rm -f /home/ec2-user/kitty/.env.night

echo "완료! 컨테이너 상태:"
docker ps
echo ""
echo "디스크 사용량:"
df -h /
echo ""
echo "Docker 이미지/컨테이너 용량:"
docker system df
