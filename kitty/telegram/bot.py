"""텔레그램 봇 - 보고 및 원격 제어"""
import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from kitty.config import settings
from kitty.utils import logger


class TelegramReporter:
    """매매 결과 보고 및 원격 제어 봇"""

    def __init__(self) -> None:
        self._app: Application | None = None  # type: ignore[type-arg]
        self._paused = False
        self._start_time = datetime.now()
        self._last_cycle_time: datetime | None = None

        # 최근 에이전트 결과 캐시
        self._last_analysis: dict[str, Any] = {}
        self._last_evaluation: dict[str, Any] = {}
        self._last_strategy: dict[str, Any] = {}

        # 외부 주입 (main에서 설정)
        self._broker: Any = None
        self._daily_report: Any = None
        self._force_cycle_flag = asyncio.Event()
        self._cycle_callback: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._pending_live_confirm = False  # live 모드 전환 확인 대기 플래그

    def build(self) -> "TelegramReporter":
        self._app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )
        self._register_handlers()
        return self

    def set_broker(self, broker: Any) -> None:
        self._broker = broker

    def set_daily_report(self, report: Any) -> None:
        self._daily_report = report

    def set_cycle_callback(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._cycle_callback = callback

    def update_analysis(self, analysis: dict[str, Any]) -> None:
        self._last_analysis = analysis

    def update_evaluation(self, evaluation: dict[str, Any]) -> None:
        self._last_evaluation = evaluation

    def update_strategy(self, strategy: dict[str, Any]) -> None:
        self._last_strategy = strategy

    def mark_cycle_done(self) -> None:
        self._last_cycle_time = datetime.now()

    # ---- 메시지 전송 ----

    async def send(self, message: str) -> None:
        if self._app is None:
            logger.warning("텔레그램 봇이 초기화되지 않았습니다")
            return
        try:
            await self._app.bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=message,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")

    async def report_trade(self, action: str, symbol: str, quantity: int, price: int, reason: str) -> None:
        emoji = "🟢" if action == "BUY" else "🔴"
        await self.send(
            f"{emoji} *{action}* 체결\n"
            f"종목: `{symbol}`\n"
            f"수량: {quantity:,}주\n"
            f"가격: {price:,}원\n"
            f"사유: {reason}"
        )

    async def report_error(self, error: str) -> None:
        await self.send(f"⚠️ *오류 발생*\n```{error[:300]}```")

    # ---- 핸들러 등록 ----

    def _register_handlers(self) -> None:
        assert self._app is not None
        cmds = [
            ("help",       self._cmd_help),
            ("status",     self._cmd_status),
            ("portfolio",  self._cmd_portfolio),
            ("balance",    self._cmd_balance),
            ("analysis",   self._cmd_analysis),
            ("evaluation", self._cmd_evaluation),
            ("report",     self._cmd_report),
            ("cycle",      self._cmd_cycle),
            ("buy",        self._cmd_buy),
            ("sell",       self._cmd_sell),
            ("setbuy",     self._cmd_setbuy),
            ("setmode",    self._cmd_setmode),
            ("pause",      self._cmd_pause),
            ("resume",     self._cmd_resume),
            ("stop",       self._cmd_stop),
            ("logs",       self._cmd_logs),
            ("dashboard",  self._cmd_dashboard),
            ("deploy",     self._cmd_deploy),
            ("restart",    self._cmd_restart),
            ("shutdown",   self._cmd_shutdown),
            ("startall",   self._cmd_startall),
        ]
        for name, handler in cmds:
            self._app.add_handler(CommandHandler(name, self._guard(handler)))

    def _guard(
        self, fn: Callable[..., Coroutine[Any, Any, None]]
    ) -> Callable[..., Coroutine[Any, Any, None]]:
        """설정된 chat_id 이외의 요청 차단"""
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat and str(update.effective_chat.id) != str(settings.telegram_chat_id):
                await update.message.reply_text("⛔ 권한 없음")  # type: ignore[union-attr]
                return
            await fn(update, ctx)
        return wrapper

    # ---- 명령어 구현 ----

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "🐱 *Kitty 명령어 목록*\n\n"
            "*조회*\n"
            "/status      — 시스템 상태\n"
            "/portfolio   — 보유 종목 및 손익\n"
            "/balance     — 잔고 및 가용현금\n"
            "/analysis    — 최근 섹터 분석\n"
            "/evaluation  — 최근 포트폴리오 평가\n"
            "/report      — 오늘 매매 요약\n\n"
            "*제어*\n"
            "/pause       — 매매 일시정지\n"
            "/resume      — 매매 재개\n"
            "/cycle       — 즉시 사이클 실행\n"
            "/stop        — 시스템 종료\n\n"
            "*설정*\n"
            "/setbuy <금액>       — 최대 매수금액 변경\n"
            "/setmode <paper|live> — 매매 모드 전환\n\n"
            "*수동 매매*\n"
            "/buy <종목코드> <수량>  — 수동 매수\n"
            "/sell <종목코드> <수량> — 수동 매도\n\n"
            "*모니터*\n"
            "/dashboard   — 모니터 대시보드 URL\n\n"
            "*AWS 제어*\n"
            "/logs [n]    — 최근 로그 n줄 (기본 50)\n"
            "/deploy      — 코드 재배포 (git pull + 재빌드)\n"
            "/restart     — 컨테이너 재시작\n"
            "/shutdown    — 서비스 전체 중단\n"
            "/startall    — 서비스 전체 시작"
        )
        await update.message.reply_text(text, parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uptime = datetime.now() - self._start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60
        last_cycle = self._last_cycle_time.strftime("%H:%M:%S") if self._last_cycle_time else "없음"
        state = "⏸️ 일시정지" if self._paused else "✅ 운영 중"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"🤖 *Kitty 상태*\n"
            f"상태: {state}\n"
            f"모드: `{settings.trading_mode.value}`\n"
            f"AI: `{settings.ai_provider.value} / {settings.resolved_model}`\n"
            f"최대매수: `{settings.max_buy_amount:,}원`\n"
            f"마지막 사이클: `{last_cycle}`\n"
            f"가동시간: `{hours}h {minutes}m`",
            parse_mode="Markdown",
        )

    async def _cmd_portfolio(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._broker:
            await update.message.reply_text("브로커 미연결")  # type: ignore[union-attr]
            return
        try:
            balance = await self._broker.get_balance()
            holdings = balance.get("output1", [])
            if not holdings:
                await update.message.reply_text("📭 보유 종목 없음")  # type: ignore[union-attr]
                return

            lines = ["📦 *보유 종목*\n"]
            total_eval = 0
            total_buy = 0
            for h in holdings:
                qty = int(h.get("hldg_qty", 0))
                if qty == 0:
                    continue
                name = h.get("prdt_name", "")
                symbol = h.get("pdno", "")
                avg = int(float(h.get("pchs_avg_pric", 0)))
                eval_amt = int(h.get("evlu_amt", 0))
                pnl = float(h.get("evlu_pfls_rt", 0))
                emoji = "🔺" if pnl >= 0 else "🔻"
                lines.append(f"{emoji} `{symbol}` {name}\n   {qty:,}주 @ {avg:,}원 ({pnl:+.1f}%)")
                total_eval += eval_amt
                total_buy += avg * qty

            if total_buy > 0:
                total_pnl = (total_eval - total_buy) / total_buy * 100
                lines.append(f"\n총평가: `{total_eval:,}원` ({total_pnl:+.1f}%)")

            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")  # type: ignore[union-attr]
        except Exception as e:
            await update.message.reply_text(f"조회 실패: {e}")  # type: ignore[union-attr]

    async def _cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._broker:
            await update.message.reply_text("브로커 미연결")  # type: ignore[union-attr]
            return
        try:
            balance = await self._broker.get_balance()
            summary = balance.get("output2", [{}])[0]
            cash = int(summary.get("dnca_tot_amt", 0))
            total_eval = int(summary.get("tot_evlu_amt", 0))
            buy_amt = int(summary.get("pchs_amt_smtl_amt", 0))
            pnl = int(summary.get("evlu_pfls_smtl_amt", 0))
            await update.message.reply_text(  # type: ignore[union-attr]
                f"💰 *잔고 현황*\n"
                f"가용현금: `{cash:,}원`\n"
                f"주식매입: `{buy_amt:,}원`\n"
                f"평가금액: `{total_eval:,}원`\n"
                f"평가손익: `{pnl:+,}원`",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"조회 실패: {e}")  # type: ignore[union-attr]

    async def _cmd_analysis(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._last_analysis:
            await update.message.reply_text("아직 분석 결과 없음")  # type: ignore[union-attr]
            return
        a = self._last_analysis
        lines = [
            f"📊 *최근 섹터 분석*\n"
            f"시장: `{a.get('market_sentiment', '-')}` | 리스크: `{a.get('risk_level', '-')}`\n"
        ]
        for s in a.get("sectors", []):
            trend = s.get("trend", "-")
            emoji = "🔺" if trend == "bullish" else ("🔻" if trend == "bearish" else "➡️")
            candidates = ", ".join(s.get("candidate_symbols", []))
            lines.append(f"{emoji} *{s.get('name', '')}* ({trend})\n   후보: `{candidates}`\n   {s.get('reason', '')[:50]}")
        lines.append(f"\n_{a.get('summary', '')[:100]}_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_evaluation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._last_evaluation:
            await update.message.reply_text("아직 평가 결과 없음")  # type: ignore[union-attr]
            return
        ev = self._last_evaluation
        lines = [f"🗂️ *포트폴리오 평가*\n_{ev.get('summary', '')[:80]}_\n"]
        action_emoji = {"HOLD": "⏸️", "BUY_MORE": "🟢", "PARTIAL_SELL": "🟡", "SELL": "🔴"}
        for e in ev.get("evaluations", []):
            emoji = action_emoji.get(e.get("action", ""), "❓")
            lines.append(
                f"{emoji} `{e.get('symbol')}` {e.get('name', '')} "
                f"({e.get('pnl_rate', 0):+.1f}%) → *{e.get('action')}*"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._daily_report:
            await update.message.reply_text("리포트 없음")  # type: ignore[union-attr]
            return
        await update.message.reply_text(self._daily_report.telegram_summary(), parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_cycle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._paused:
            await update.message.reply_text("⏸️ 일시정지 중입니다. `/resume` 후 실행하세요.")  # type: ignore[union-attr]
            return
        if self._cycle_callback is None:
            await update.message.reply_text("사이클 콜백 미등록")  # type: ignore[union-attr]
            return
        await update.message.reply_text("🔄 즉시 매매 사이클을 실행합니다...")  # type: ignore[union-attr]
        try:
            await self._cycle_callback()
            await update.message.reply_text("✅ 사이클 완료")  # type: ignore[union-attr]
        except Exception as e:
            await update.message.reply_text(f"❌ 사이클 오류: {e}")  # type: ignore[union-attr]

    async def _cmd_buy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if len(args) < 2:
            await update.message.reply_text("사용법: `/buy <종목코드> <수량>`\n예: `/buy 005930 3`", parse_mode="Markdown")  # type: ignore[union-attr]
            return
        if not self._broker:
            await update.message.reply_text("브로커 미연결")  # type: ignore[union-attr]
            return
        symbol, qty_str = args[0], args[1]
        try:
            qty = int(qty_str)
            await update.message.reply_text(f"🛒 `{symbol}` {qty}주 시장가 매수 중...")  # type: ignore[union-attr]
            try:
                _q = await self._broker.get_quote(symbol)
                _name = _q.name
            except Exception:
                _name = ""
            _lbl = f"{_name}({symbol})" if _name else symbol
            order = await self._broker.buy(symbol, qty, 0, _name)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"🟢 *수동 매수 완료*\n종목: `{symbol}`\n수량: {qty:,}주\n주문번호: `{order.order_id}`",
                parse_mode="Markdown",
            )
            logger.info(f"[텔레그램] 수동 매수: {_lbl} {qty}주")
        except Exception as e:
            await update.message.reply_text(f"❌ 매수 실패: {e}")  # type: ignore[union-attr]

    async def _cmd_sell(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if len(args) < 2:
            await update.message.reply_text("사용법: `/sell <종목코드> <수량>`\n예: `/sell 005930 3`", parse_mode="Markdown")  # type: ignore[union-attr]
            return
        if not self._broker:
            await update.message.reply_text("브로커 미연결")  # type: ignore[union-attr]
            return
        symbol, qty_str = args[0], args[1]
        try:
            qty = int(qty_str)
            await update.message.reply_text(f"📤 `{symbol}` {qty}주 시장가 매도 중...")  # type: ignore[union-attr]
            try:
                _q = await self._broker.get_quote(symbol)
                _name = _q.name
            except Exception:
                _name = ""
            _lbl = f"{_name}({symbol})" if _name else symbol
            order = await self._broker.sell(symbol, qty, 0, _name)
            await update.message.reply_text(  # type: ignore[union-attr]
                f"🔴 *수동 매도 완료*\n종목: `{symbol}`\n수량: {qty:,}주\n주문번호: `{order.order_id}`",
                parse_mode="Markdown",
            )
            logger.info(f"[텔레그램] 수동 매도: {_lbl} {qty}주")
        except Exception as e:
            await update.message.reply_text(f"❌ 매도 실패: {e}")  # type: ignore[union-attr]

    async def _cmd_setbuy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if not args:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"사용법: `/setbuy <금액>`\n현재: `{settings.max_buy_amount:,}원`",
                parse_mode="Markdown",
            )
            return
        try:
            amount = int(args[0].replace(",", ""))
            if amount < 10_000:
                await update.message.reply_text("최소 10,000원 이상 입력하세요.")  # type: ignore[union-attr]
                return
            settings.max_buy_amount = amount
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ 최대 매수금액 변경: `{amount:,}원`", parse_mode="Markdown"
            )
            logger.info(f"[텔레그램] 최대 매수금액 변경: {amount:,}원")
        except ValueError:
            await update.message.reply_text("숫자를 입력하세요. 예: `/setbuy 500000`", parse_mode="Markdown")  # type: ignore[union-attr]

    async def _cmd_setmode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from kitty.config import TradingMode
        args = ctx.args or []

        # 확인 단계: /setmode live confirm
        if self._pending_live_confirm:
            self._pending_live_confirm = False
            if args and args[0].lower() == "confirm":
                settings.trading_mode = TradingMode.LIVE
                if self._broker:
                    self._broker.reset_token()
                await update.message.reply_text(  # type: ignore[union-attr]
                    "🔴 *실전 매매 모드로 전환되었습니다.*\n실제 자금으로 거래됩니다.",
                    parse_mode="Markdown",
                )
                logger.info("[텔레그램] 매매 모드 전환: live")
                return
            else:
                await update.message.reply_text("❌ 취소되었습니다. 다시 `/setmode live`를 입력하세요.", parse_mode="Markdown")  # type: ignore[union-attr]
                return

        if not args:
            current = settings.trading_mode.value
            await update.message.reply_text(  # type: ignore[union-attr]
                f"현재 모드: `{current}`\n사용법: `/setmode paper` 또는 `/setmode live`",
                parse_mode="Markdown",
            )
            return

        mode = args[0].lower()
        if mode not in ("paper", "live"):
            await update.message.reply_text("❌ `paper` 또는 `live`만 입력 가능합니다.", parse_mode="Markdown")  # type: ignore[union-attr]
            return

        if mode == settings.trading_mode.value:
            await update.message.reply_text(f"이미 `{mode}` 모드입니다.", parse_mode="Markdown")  # type: ignore[union-attr]
            return

        if mode == "live":
            self._pending_live_confirm = True
            await update.message.reply_text(  # type: ignore[union-attr]
                "⚠️ *실전 매매 모드로 전환하려 합니다.*\n"
                "실제 자금으로 거래됩니다. 계속하려면:\n"
                "`/setmode confirm`",
                parse_mode="Markdown",
            )
            return

        # paper로 전환 (확인 불필요)
        settings.trading_mode = TradingMode.PAPER
        if self._broker:
            self._broker.reset_token()
        await update.message.reply_text(  # type: ignore[union-attr]
            "📄 *모의 매매 모드로 전환되었습니다.*",
            parse_mode="Markdown",
        )
        logger.info("[텔레그램] 매매 모드 전환: paper")

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._paused = True
        await update.message.reply_text("⏸️ 매매를 일시정지했습니다.")  # type: ignore[union-attr]
        logger.info("텔레그램 명령으로 매매 일시정지")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._paused = False
        await update.message.reply_text("▶️ 매매를 재개합니다.")  # type: ignore[union-attr]
        logger.info("텔레그램 명령으로 매매 재개")

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🛑 Kitty를 종료합니다.")  # type: ignore[union-attr]
        logger.info("텔레그램 명령으로 시스템 종료")
        # 메인 루프가 확인하는 플래그 세팅
        self._paused = True
        self._force_cycle_flag.set()
        # 프로세스 종료
        import os, signal
        os.kill(os.getpid(), signal.SIGINT)

    async def _cmd_dashboard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        host = settings.monitor_host
        if not host:
            host = await self._fetch_ec2_public_ip()
        url = f"http://{host}:{settings.monitor_port}"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📊 *Kitty Monitor*\n[{url}]({url})",
            parse_mode="Markdown",
        )

    @staticmethod
    async def _fetch_ec2_public_ip() -> str:
        """EC2 IMDSv2로 퍼블릭 IP 조회 (토큰 기반 2단계 요청)"""
        import aiohttp
        _imds = "http://169.254.169.254"
        try:
            async with aiohttp.ClientSession() as session:
                # 1단계: IMDSv2 토큰 발급
                async with session.put(
                    f"{_imds}/latest/api/token",
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    token = await resp.text()
                # 2단계: 토큰으로 퍼블릭 IP 조회
                async with session.get(
                    f"{_imds}/latest/meta-data/public-ipv4",
                    headers={"X-aws-ec2-metadata-token": token},
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    return (await resp.text()).strip()
        except Exception:
            return "EC2-IP"

    # ---- AWS 제어 명령어 ----

    async def _run_shell(self, cmd: str, timeout: int = 300) -> tuple[str, str, int]:
        """비동기 셸 명령 실행 → (stdout, stderr, returncode)"""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return "", "timeout", -1
        return stdout.decode(errors="replace"), stderr.decode(errors="replace"), proc.returncode

    async def _cmd_logs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        n = 50
        if args:
            try:
                n = min(int(args[0]), 200)
            except ValueError:
                pass

        log_file = Path(f"/app/logs/kitty_{date.today()}.log")
        if not log_file.exists():
            await update.message.reply_text("📭 오늘 로그 파일 없음")  # type: ignore[union-attr]
            return

        lines = log_file.read_text(encoding="utf-8").splitlines()
        tail = "\n".join(lines[-n:])
        if not tail:
            await update.message.reply_text("로그 내용 없음")  # type: ignore[union-attr]
            return
        if len(tail) > 3800:
            tail = "...(생략)...\n" + tail[-3800:]
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📋 *최근 로그 {min(n, len(lines))}줄*\n```\n{tail}\n```",
            parse_mode="Markdown",
        )

    async def _cmd_deploy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🚀 재배포를 시작합니다. 잠시 후 봇이 재연결됩니다...")  # type: ignore[union-attr]
        logger.info("[텔레그램] 재배포 요청")

        # git pull
        stdout, stderr, rc = await self._run_shell("cd /host/kitty && git pull")
        if rc != 0:
            await update.message.reply_text(f"❌ git pull 실패\n```{stderr[:300]}```", parse_mode="Markdown")  # type: ignore[union-attr]
            return
        await update.message.reply_text(f"✅ git pull\n```{stdout[:200].strip()}```", parse_mode="Markdown")  # type: ignore[union-attr]

        # docker build
        await update.message.reply_text("🔨 이미지 빌드 중...")  # type: ignore[union-attr]
        _, stderr, rc = await self._run_shell("docker build -t kitty-trader /host/kitty", timeout=600)
        if rc != 0:
            await update.message.reply_text(f"❌ 빌드 실패\n```{stderr[:300]}```", parse_mode="Markdown")  # type: ignore[union-attr]
            return

        # 현재 컨테이너 env 추출 후 재기동 (이 시점부터 컨테이너가 교체됨)
        await update.message.reply_text("🔄 컨테이너 교체 중... (연결이 잠시 끊깁니다)")  # type: ignore[union-attr]
        restart_cmd = (
            "docker inspect kitty-trader --format '{{json .Config.Env}}' | "
            "python3 -c \"import sys,json; [print(x) for x in json.load(sys.stdin)]\" "
            "> /tmp/kitty_env.txt && "
            "docker stop kitty-trader && docker rm kitty-trader && "
            "docker run -d --name kitty-trader --restart unless-stopped "
            "--env-file /tmp/kitty_env.txt "
            "-v /home/ec2-user/kitty/logs:/app/logs "
            "-v /home/ec2-user/kitty/feedback:/app/feedback "
            "-v /home/ec2-user/kitty/token_usage:/app/token_usage "
            "-v /var/run/docker.sock:/var/run/docker.sock "
            "-v /home/ec2-user/kitty:/host/kitty "
            "kitty-trader && "
            "rm -f /tmp/kitty_env.txt"
        )
        await self._run_shell(restart_cmd)
        # 컨테이너가 교체되므로 이후 코드는 실행되지 않음

    async def _cmd_restart(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🔄 컨테이너를 재시작합니다. 잠시 후 재연결됩니다...")  # type: ignore[union-attr]
        logger.info("[텔레그램] 컨테이너 재시작 요청")
        await self._run_shell("docker restart kitty-trader")
        # 컨테이너 재시작으로 이후 코드 실행 안 됨

    async def _cmd_shutdown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("⛔ 모든 서비스를 중단합니다.")  # type: ignore[union-attr]
        logger.info("[텔레그램] 서비스 전체 중단 요청")
        await self._run_shell("docker stop kitty-trader")

    async def _cmd_startall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("▶️ 서비스를 시작합니다. 잠시 후 재연결됩니다...")  # type: ignore[union-attr]
        logger.info("[텔레그램] 서비스 전체 시작 요청")
        # 중단된 컨테이너 재시작 (이미 실행 중이면 무시됨)
        _, stderr, rc = await self._run_shell("docker start kitty-trader")
        if rc != 0:
            await update.message.reply_text(f"❌ 시작 실패\n```{stderr[:300]}```", parse_mode="Markdown")  # type: ignore[union-attr]

    # ---- 속성 ----

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def start_polling(self) -> None:
        assert self._app is not None
        await self._app.initialize()
        await self._app.start()
        # drop_pending_updates=True: 재시작 전에 쌓인 메시지 무시
        # 봇이 오프라인이었던 동안의 명령이 뒤늦게 실행되는 것 방지
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

    async def stop_polling(self) -> None:
        if self._app:
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
