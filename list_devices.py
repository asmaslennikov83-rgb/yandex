"""Run locally/on hosting to obtain device IDs without printing tokens."""
import asyncio
import os
from dotenv import load_dotenv
from core import APIError, Yandex

async def main():
    load_dotenv()
    token=os.getenv('YANDEX_OAUTH_TOKEN','').strip()
    if not token:
        raise SystemExit('Заполните YANDEX_OAUTH_TOKEN.')
    api=Yandex(token)
    try:
        info=await api.info()
        for d in info.get('devices',[]):
            print(d['id'], '|', d.get('type'), '|', d.get('name'))
    except APIError as e:
        raise SystemExit(str(e)) from None
    finally:
        await api.client.aclose()

if __name__=='__main__': asyncio.run(main())
