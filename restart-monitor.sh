#!/bin/bash
# kitty-monitor 컨테이너만 재빌드·재시작하는 스크립트
# 용도: monitor/app.py 수정 후 kitty-trader / kitty-night-trader 중단 없이 빠르게 반영
# 사용: bash restart-monitor.sh  (EC2에서 ~/kitty 디렉토리 기준)

set -e
cd "$(dirname "$0")"

echo "[1/4] AWS Secrets 로드 중..."
SECRET=$(aws secretsmanager get-secret-value \
  --secret-id kitty/prod \
  --region ap-northeast-2 \
  --query SecretString \
  --output text)

_TG_TOKEN=$(python3 -c "import sys,json; print(json.loads(sys.argv[1])['TELEGRAM_BOT_TOKEN'])"   "$SECRET")
_TG_CHAT=$(python3  -c "import sys,json; print(json.loads(sys.argv[1])['TELEGRAM_CHAT_ID'])"     "$SECRET")
_MON_PW=$(python3   -c "import sys,json; print(json.loads(sys.argv[1])['MONITOR_PASSWORD'])"     "$SECRET")
_OPENAI_KEY=$(python3 -c "import sys,json; print(json.loads(sys.argv[1]).get('OPENAI_API_KEY',''))"      "$SECRET")
_ANTHROPIC_KEY=$(python3 -c "import sys,json; print(json.loads(sys.argv[1]).get('ANTHROPIC_API_KEY',''))" "$SECRET")
_AI_PROVIDER=$(python3 -c "import sys,json; print(json.loads(sys.argv[1]).get('AI_PROVIDER','openai'))"  "$SECRET")
_AI_MODEL=$(python3  -c "import sys,json; print(json.loads(sys.argv[1]).get('AI_MODEL','gpt-4o'))"       "$SECRET")

echo "[2/4] 기존 컨테이너 중지 및 제거..."
docker stop kitty-monitor 2>/dev/null || true
docker rm   kitty-monitor 2>/dev/null || true

echo "[3/4] 이미지 재빌드..."
docker build -t kitty-monitor ./monitor
docker image prune -f

echo "[4/4] 컨테이너 재시작 (전체 볼륨 마운트)..."
mkdir -p /home/ec2-user/kitty/monitor-data
mkdir -p /home/ec2-user/kitty/reports
mkdir -p /home/ec2-user/kitty/night-reports

docker run -d \
  --name kitty-monitor \
  --restart unless-stopped \
  --log-opt max-size=5m --log-opt max-file=3 \
  -v /home/ec2-user/kitty/logs:/logs:ro \
  -v /home/ec2-user/kitty/feedback:/feedback \
  -v /home/ec2-user/kitty/token_usage:/token_usage:ro \
  -v /home/ec2-user/kitty/commands:/commands \
  -v /home/ec2-user/kitty/monitor-data:/data \
  -v /home/ec2-user/kitty/night-logs:/night-logs:ro \
  -v /home/ec2-user/kitty/night-feedback:/night-feedback \
  -v /home/ec2-user/kitty/night-token_usage:/night-token_usage:ro \
  -v /home/ec2-user/kitty/reports:/reports:ro \
  -v /home/ec2-user/kitty/night-reports:/night-reports:ro \
  -v /home/ec2-user/kitty/night-commands:/night-commands \
  -e TELEGRAM_BOT_TOKEN="$_TG_TOKEN" \
  -e TELEGRAM_CHAT_ID="$_TG_CHAT" \
  -e MONITOR_PASSWORD="$_MON_PW" \
  -e OPENAI_API_KEY="$_OPENAI_KEY" \
  -e ANTHROPIC_API_KEY="$_ANTHROPIC_KEY" \
  -e AI_PROVIDER="${_AI_PROVIDER:-openai}" \
  -e AI_MODEL="${_AI_MODEL:-gpt-4o}" \
  -p 8080:8080 \
  kitty-monitor

echo ""
echo "✅ kitty-monitor 재시작 완료"
docker ps --filter name=kitty-monitor --format "  {{.Names}}: {{.Status}}"
