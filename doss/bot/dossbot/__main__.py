import asyncio

from .bot import run

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        print(exc)
