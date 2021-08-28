import asyncio
import logging
import logging.config
import signal
import traceback
from contextlib import suppress
from functools import partial
from typing import Dict, List, Optional, Set, Tuple, Type

import aioredis

from yatq.defaults import DEFAULT_QUEUE_NAMESPACE
from yatq.dto import TaskWrapper
from yatq.enums import TaskState
from yatq.exceptions import TaskRescheduleException
from yatq.queue import Queue
from yatq.worker.factory.base import BaseJobFactory
from yatq.worker.worker_settings import WorkerSettings

LOGGER = logging.getLogger("yatq.worker")
LOGGER.setLevel("INFO")
GRAVEKEEPER_LOGGER = logging.getLogger("yatq.gravekeeper")


class Worker:
    def __init__(
        self,
        queue_list: List[Queue],
        task_factory: BaseJobFactory,
        poll_interval: float = 2.0,
        max_jobs: int = 8,
        gravekeeper_interval: float = 30.0,
    ) -> None:
        self.queue_list = queue_list
        self.task_factory = task_factory

        self.started = asyncio.Event()
        self.got_task = asyncio.Event()
        self.completed_task = asyncio.Event()

        self._poll_event = asyncio.Event()
        self._poll_interval = poll_interval

        self._gravekeeper_interval = gravekeeper_interval

        self._stop_event = asyncio.Event()

        self._max_jobs = max_jobs
        self._job_handlers: Set[asyncio.Task] = set()

    @property
    def should_get_new_task(self) -> bool:
        return len(self._job_handlers) < self._max_jobs

    async def _periodic_poll(self) -> None:
        while True:
            self._poll_event.set()
            await asyncio.sleep(self._poll_interval)

    async def _wait_poll(self) -> None:
        _, pending = await asyncio.wait(
            {self._poll_event.wait(), self._stop_event.wait()},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        self._poll_event.clear()

    async def _run_gravekeeper(self):
        while True:
            await self._call_gravekeeper()
            await asyncio.sleep(self._gravekeeper_interval)

    async def _call_gravekeeper(self):
        for queue in self.queue_list:
            buried_count = await queue.bury_tasks()

            if buried_count:
                GRAVEKEEPER_LOGGER.warning(
                    "Buried %s tasks in queue '%s'", buried_count, queue.name
                )

    async def _try_fetch_task(self) -> bool:
        for queue in self.queue_list:
            LOGGER.debug("Requesting new task from queue %s", queue.name)

            try:
                wrapper: Optional[TaskWrapper] = await queue.get_task()
            except Exception:  # pragma: no cover
                LOGGER.exception("Error getting task from queue %s", queue.name)
                continue

            if not wrapper:
                continue

            await self._start_task_processing(wrapper, queue)
            return True

        return False

    async def _start_task_processing(self, wrapper: TaskWrapper, queue: Queue) -> None:
        LOGGER.info("Got task %s", wrapper.task.id)
        LOGGER.debug("Task data: %s", wrapper.task.encoded_data)

        self.got_task.clear()
        self.got_task.set()

        handle_task = asyncio.create_task(self._handle_task(wrapper, queue))
        handle_task.add_done_callback(self._remove_completed_handle_task)
        self._job_handlers.add(handle_task)

    def _remove_completed_handle_task(self, task: asyncio.Task) -> None:
        self._job_handlers.discard(task)
        self.completed_task.clear()
        self.completed_task.set()

    async def _handle_task(self, wrapper, queue) -> None:
        try:
            task_job = self.task_factory.create_job(wrapper.task)
        except Exception:
            LOGGER.exception("Failed to create job")
            await queue.fail_task(wrapper)
            return

        try:
            coro = task_job.process()
        except Exception:
            LOGGER.exception("Failed to create job coroutine")
            await queue.fail_task(wrapper)
            return

        LOGGER.info("Starting job")
        try:
            await coro
        except Exception:
            LOGGER.exception("Exception in job")
            wrapper.task.result = {"traceback": traceback.format_exc()}

            await self._try_reschedule_task(wrapper, queue)
            return

        wrapper.task.state = TaskState.COMPLETED
        await queue.complete_task(wrapper)

        try:
            await task_job.do_post_process()
        except Exception:
            LOGGER.exception("Exception in job post processing")

        LOGGER.info(
            "Finished job after %s seconds (%s seconds postprocessing) with state %s",
            task_job.process_duration,
            task_job.post_process_duration,
            wrapper.task.state.value,
        )

    async def _try_reschedule_task(
        self, wrapped_task: TaskWrapper, queue: Queue, force: bool = False
    ):
        task_id = wrapped_task.task.id
        try:
            scheduled_after = await queue.auto_reschedule_task(
                wrapped_task, force=force
            )
        except TaskRescheduleException as error:
            LOGGER.warning("Failed to reschedule task %s: %s", task_id, error)
        else:
            LOGGER.info(
                "Rescheduling task %s, next try after %s seconds",
                task_id,
                scheduled_after,
            )

    async def _complete_pending_jobs(self):
        if not self._job_handlers:
            LOGGER.info("No running jobs; exiting")
            return

        LOGGER.info("Waiting for %s running job(s) to finish", len(self._job_handlers))
        await asyncio.wait(self._job_handlers, return_when=asyncio.ALL_COMPLETED)

    async def stop(self) -> None:
        LOGGER.info("Stopping worker")
        self._stop_event.set()
        self._poll_event.set()

    async def run(self) -> None:
        LOGGER.info(
            "Starting worker, queue list: %s", [q.name for q in self.queue_list]
        )
        self._stop_event.clear()
        self.started.clear()
        self.started.set()

        periodic_poll_task = asyncio.create_task(self._periodic_poll())
        gravekeeper_task = asyncio.create_task(self._run_gravekeeper())

        while not self._stop_event.is_set():
            if self.should_get_new_task:
                fetched = await self._try_fetch_task()
                if fetched:
                    continue
            await self._wait_poll()

        await self._complete_pending_jobs()

        periodic_poll_task.cancel()
        gravekeeper_task.cancel()


DEFAULT_LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}

DEFAULT_MAX_JOBS = 8


def build_worker(
    redis_client: aioredis.Redis,
    worker_settings: Type[WorkerSettings],
    queue_names: List[str],
    max_jobs: Optional[int] = None,
) -> Worker:
    max_jobs = max_jobs or DEFAULT_MAX_JOBS
    factory_kwargs = worker_settings.factory_kwargs or {}
    task_factory = worker_settings.factory_cls(**factory_kwargs)

    queue_list: List[Queue] = [
        Queue(
            client=redis_client,
            name=queue_name,
            namespace=worker_settings.queue_namespace or DEFAULT_QUEUE_NAMESPACE,
        )
        for queue_name in queue_names
    ]
    worker = Worker(
        queue_list=queue_list,
        task_factory=task_factory,
        max_jobs=max_jobs,
    )

    return worker
