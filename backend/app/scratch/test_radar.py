import asyncio
import sys

async def main():
    from app.quant.momentum_scanner import get_breakout_candidates
    try:
        print("Scanning breakout candidates...")
        candidates = await get_breakout_candidates(ltf="5m", htf="1h")
        print(f"Candidates found: {len(candidates)}")
        for c in candidates[:5]:
            print(c)
    except Exception as e:
        import traceback
        print("Scanner failed with error:")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
