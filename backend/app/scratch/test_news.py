import asyncio

async def main():
    from app.data_sources.gdelt import fetch_global_news
    try:
        print("Fetching global news...")
        articles = await fetch_global_news()
        print(f"Total articles found: {len(articles)}")
        for art in articles:
            print(f"- [{art.get('feed')}] {art.get('title')} ({art.get('source')})")
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
