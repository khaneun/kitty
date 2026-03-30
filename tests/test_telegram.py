"""텔레그램 봇 연결 검증 스크립트

실행: .venv/bin/python tests/test_telegram.py
"""
import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def test_get_me() -> None:
    """봇 토큰 유효성 확인"""
    print("\n[1] 봇 토큰 확인 (getMe)...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE}/getMe")
        data = resp.json()
        if data.get("ok"):
            bot = data["result"]
            print(f"    ✅ 봇 이름: {bot['first_name']} (@{bot['username']})")
            print(f"    ✅ 봇 ID  : {bot['id']}")
        else:
            print(f"    ❌ 실패: {data}")


async def test_get_updates() -> None:
    """최근 업데이트 조회 - Chat ID 확인용"""
    print("\n[2] 최근 메시지 조회 (getUpdates)...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE}/getUpdates", params={"limit": 5})
        data = resp.json()
        if not data.get("ok"):
            print(f"    ❌ 실패: {data}")
            return

        updates = data["result"]
        if not updates:
            print("    ⚠️  수신된 메시지 없음")
            print("    👉 텔레그램에서 봇에게 /start 를 먼저 보내주세요")
            return

        print(f"    ✅ 최근 업데이트 {len(updates)}건:")
        for u in updates:
            msg = u.get("message", {})
            chat = msg.get("chat", {})
            print(f"       - Chat ID: {chat.get('id')}  |  from: {chat.get('first_name')}  |  text: {msg.get('text')}")


async def test_send_message() -> None:
    """실제 메시지 전송 테스트"""
    print(f"\n[3] 메시지 전송 테스트 (Chat ID: {CHAT_ID})...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": "🐱 *Kitty 텔레그램 연결 테스트 성공!*\n\n봇이 정상적으로 동작합니다.",
                "parse_mode": "Markdown",
            },
        )
        data = resp.json()
        if data.get("ok"):
            print("    ✅ 메시지 전송 성공!")
            print(f"       Message ID: {data['result']['message_id']}")
        else:
            err = data.get("description", "")
            print(f"    ❌ 전송 실패: {err}")
            if "chat not found" in err.lower():
                print("    👉 봇에게 먼저 /start 메시지를 보내주세요")
            elif "blocked" in err.lower():
                print("    👉 봇이 차단된 상태입니다")


async def main() -> None:
    print("=" * 50)
    print("  Kitty 텔레그램 연결 검증")
    print("=" * 50)
    print(f"  BOT_TOKEN: ...{BOT_TOKEN[-10:] if BOT_TOKEN else '없음'}")
    print(f"  CHAT_ID  : {CHAT_ID or '없음'}")

    await test_get_me()
    await test_get_updates()
    await test_send_message()

    print("\n" + "=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
