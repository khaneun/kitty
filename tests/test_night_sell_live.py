"""
Night mode 모의투자 매도 최종 테스트 — SLL_TYPE: "00" 적용 검증
"""
import asyncio, os, sys
from pathlib import Path

_ENV_FILE = Path(__file__).parent.parent / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))
from kitty_night.broker.kis_overseas import KISOverseasBroker


async def main():
    broker = KISOverseasBroker()
    try:
        print(f"[모드] {broker._mode}  |  [계좌] {broker._cano}-{broker._acnt_prdt_cd}\n")

        balance = await broker.get_balance()
        holdings = balance.get("holdings", [])
        print(f"보유 종목 {len(holdings)}개:")
        for h in holdings:
            print(f"  {h['symbol']:6s} qty={h['quantity']:3d} avg=${h['avg_price']:,.2f} excd={h['excd']}")

        if not holdings:
            return

        target = holdings[0]
        symbol, excd = target["symbol"], target["excd"]
        print(f"\n[목표] {symbol} ({excd}) 1주 매도")

        print("rate limit 회피를 위해 5초 대기...")
        await asyncio.sleep(5)

        print(f"broker.sell('{symbol}', '{excd}', 1, 0) 호출\n")
        result = await broker.sell(symbol, excd, 1, 0)

        print(f"[결과]")
        print(f"  order_id : {result.order_id}")
        print(f"  status   : {result.status}")
        print(f"  side     : {result.side}  qty={result.quantity}  price=${result.price:,.2f}")

        if result.status == "SUBMITTED":
            print("\n[OK] 매도 주문 접수 완료!")
        else:
            print(f"\n[?] status={result.status}")

    except Exception as e:
        print(f"\n[FAIL] {type(e).__name__}: {e}")
    finally:
        await broker.close()

asyncio.run(main())
