"""Background work for "Find a case": running searches and fetching newly added cases."""
import asyncio

from models.session import worker_session
from scraper.ecourts import ECourtsScraper
from search.jobs import run_job
from workers.celery_app import celery_app
from workers.redis_client import get_sync_redis


@celery_app.task(name="workers.tasks.search.run_search")
def run_search(job_id: int) -> None:
    asyncio.run(run_job(job_id, worker_session, get_sync_redis()))


@celery_app.task(name="workers.tasks.search.fetch_new_case")
def fetch_new_case(case_id: int) -> None:
    """Full eCourts details for a case added straight from search results (search queue, so it's prompt)."""
    from workers.tasks.poll_cases import _make_scraper, poll_one

    async def run():
        scraper: ECourtsScraper = _make_scraper()
        try:
            async with worker_session() as db:
                await poll_one(db, case_id, scraper)
        finally:
            await scraper.close()

    asyncio.run(run())
