"""One lifecycle for enabled jobs in HTTP and stdio MCP hosts."""
import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(application):
    tasks=[]
    try:
        tasks=[asyncio.create_task(factory(),name=name)
               for name,factory in application.contributions.jobs.items()]
        if application.settings.writable:
            from .model_jobs import run
            tasks.append(asyncio.create_task(run(application),name='configured-model-tasks'))
            from .work_tasks import run as run_work
            tasks.append(asyncio.create_task(run_work(application.settings),name='requested-work'))
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)


async def index_job(settings):
    from .recall.worker import IndexWorker
    worker=IndexWorker(settings)
    worker.thread.start()
    try:
        await asyncio.Future()
    finally:
        worker.stop.set()
        await asyncio.to_thread(worker.thread.join,45)
