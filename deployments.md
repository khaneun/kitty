# Kitty 배포 가이드

## 인프라 요약

| 항목 | 값 |
|------|-----|
| EC2 인스턴스명 | kitty-trader |
| EC2 리전 | ap-northeast-2 (서울) |
| EC2 유저 | ec2-user |
| SSH 키 | `~/kitty-key.pem` |
| Secrets Manager | `kitty/prod` |
| 대시보드 포트 | 8080 |

> EC2 퍼블릭 IP는 재시작 시 바뀔 수 있음. 현재 IP 조회:
> ```bash
> aws ec2 describe-instances --filters "Name=tag:Name,Values=kitty-trader" \
>   --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
> ```

---

## EC2 SSH 접속

```bash
ssh -i ~/kitty-key.pem ec2-user@<EC2-IP>
```

---

## 1. 코드 변경 → Git Push

```bash
git add <파일>
git commit -m "설명"
git push origin main
```

---

## 2. EC2에 코드 반영 (git pull)

```bash
ssh -i ~/kitty-key.pem ec2-user@<EC2-IP>
cd /home/ec2-user/kitty
git pull origin main
```

---

## 3. kitty-monitor 재배포

monitor/app.py 또는 monitor/ 관련 파일 변경 시.

```bash
# EC2 SSH 접속 후
cd /home/ec2-user/kitty

# Secrets Manager에서 환경변수 추출
MONITOR_PASSWORD=$(aws secretsmanager get-secret-value --secret-id kitty/prod \
  --query SecretString --output text | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('MONITOR_PASSWORD',''))")
TELEGRAM_BOT_TOKEN=$(aws secretsmanager get-secret-value --secret-id kitty/prod \
  --query SecretString --output text | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('TELEGRAM_BOT_TOKEN',''))")
TELEGRAM_CHAT_ID=$(aws secretsmanager get-secret-value --secret-id kitty/prod \
  --query SecretString --output text | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('TELEGRAM_CHAT_ID',''))")

# 이미지 빌드
docker build -t kitty-monitor ./monitor

# 컨테이너 교체
docker stop kitty-monitor && docker rm kitty-monitor
docker run -d --name kitty-monitor --restart unless-stopped \
  -v $(pwd)/logs:/logs:ro \
  -v $(pwd)/feedback:/feedback:ro \
  -v $(pwd)/token_usage:/token_usage:ro \
  -v $(pwd)/commands:/commands \
  -v $(pwd)/monitor-data:/data \
  -e MONITOR_PASSWORD="$MONITOR_PASSWORD" \
  -e TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  -e TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
  -p 8080:8080 kitty-monitor

# 정상 확인
sleep 5 && curl -s http://localhost:8080/health
```

---

## 4. kitty-trader 재배포

kitty/ 하위 Python 코드 변경 시.

```bash
# EC2 SSH 접속 후
cd /home/ec2-user/kitty

# start.sh 실행 (git pull + Secrets 주입 + 전체 빌드/재시작)
sudo systemctl restart kitty

# 또는 직접 실행
bash start.sh
```

> `start.sh`는 git pull → Secrets Manager → .env 생성 → docker build → 컨테이너 교체 → .env 삭제 순으로 동작.

---

## 5. 전체 재시작 (kitty-trader + kitty-monitor 동시)

```bash
sudo systemctl restart kitty
```

> kitty.service → start.sh 실행 → kitty-trader + kitty-monitor 모두 재시작.

---

## 6. 로컬에서 원클릭 배포 (SSH 직접 실행)

```bash
EC2_IP=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=kitty-trader" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

ssh -i ~/kitty-key.pem ec2-user@$EC2_IP "cd /home/ec2-user/kitty && git pull origin main && bash start.sh"
```

---

## 7. 컨테이너 상태 확인

```bash
# EC2 SSH 접속 후
docker ps                          # 실행 중인 컨테이너
docker logs kitty-trader --tail 50  # kitty-trader 최근 로그
docker logs kitty-monitor --tail 20 # kitty-monitor 최근 로그
```

---

## 8. 디스크 / 로그 정리

로그가 쌓여 디스크가 가득 차면 kitty-monitor 시작 시 OOM으로 죽을 수 있음.

```bash
# 디스크 사용량 확인
df -h /
du -sh /home/ec2-user/kitty/logs/*

# 오래된 로그 삭제 (오늘 제외)
sudo find /home/ec2-user/kitty/logs -name "kitty_*.log" -not -name "kitty_$(date +%Y-%m-%d).log" -delete

# Docker 미사용 이미지 정리
docker image prune -af
```

---

## 9. EventBridge 스케줄 (자동 켜기/끄기)

| 동작 | 시각 (KST) | 요일 |
|------|-----------|------|
| EC2 시작 | 08:40 | 월~금 |
| EC2 중지 | 15:40 | 월~금 |

EC2 시작 시 `kitty.service` (start.sh) 가 자동 실행되어 최신 코드를 배포함.

---

## 10. 주의사항

- **pochaco 서비스 혼재 금지** — kitty EC2에 pochaco 관련 서비스/디렉토리가 설치되면 포트 8080 충돌 발생. 확인: `ls /opt/` 및 `systemctl list-units | grep pochaco`
- **포트 8080 독점** — kitty-monitor 외 다른 프로세스가 8080을 쓰는 경우 `ss -tlnp | grep 8080` 으로 확인
- **.env 파일** — start.sh가 실행 도중 임시 생성 후 자동 삭제. Git에 커밋 금지
- **실전/모의 전환** — Telegram `/setmode live` 또는 모니터 GNB 셀렉터 사용. `.env` 직접 편집 불필요
