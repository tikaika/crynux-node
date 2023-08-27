import logging
import os.path
import shutil
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, contextmanager
from typing import List, Optional

import celery.exceptions as celery_exceptions
from anyio import Lock, fail_after, get_cancelled_exc_class, to_thread
from celery.result import AsyncResult

from h_server import models
from h_server.event_queue import EventQueue, get_event_queue
from h_server.celery import get_celery
from h_server.contracts import Contracts, TxRevertedError, get_contracts
from h_server.relay import Relay, RelayError, get_relay
from h_server.watcher import EventWatcher, get_watcher

from .exceptions import TaskError, TaskErrorSource, TaskFailure
from .state_cache import TaskStateCache
from .utils import make_result_commitments
from web3.types import EventData

_logger = logging.getLogger()


class TaskRunner(ABC):
    @abstractmethod
    def __init__(
        self,
        task_id: int,
        state_cache: TaskStateCache,
        queue: EventQueue,
    ):
        self.task_id = task_id
        self.cache = state_cache
        self.queue = queue

    @abstractmethod
    async def init(self):
        ...

    @abstractmethod
    async def process_event(self, event: models.TaskEvent) -> bool:
        ...


@contextmanager
def wrap_task_error():
    try:
        yield
    except get_cancelled_exc_class() as e:
        raise e
    except AssertionError as e:
        _logger.exception(e)
        _logger.error("Task assert error")
        raise TaskError(str(e), TaskErrorSource.Unknown, retry=False)
    except RelayError as e:
        retry = (
            e.status_code == 400
            and e.method == "getTask"
            and ("Task not found" in e.message or "Task not ready" in e.message)
        )
        _logger.exception(e)
        _logger.error("Task relay error")
        raise TaskError(str(e), TaskErrorSource.Relay, retry=retry)
    except TxRevertedError as e:
        _logger.exception(e)
        _logger.error("Task contracts error")
        raise TaskError(str(e), TaskErrorSource.Contracts, retry=False)
    except celery_exceptions.CeleryError as e:
        _logger.exception(e)
        _logger.error("Task celery error")
        retry = isinstance(e, celery_exceptions.TimeoutError) or isinstance(
            e, celery_exceptions.Retry
        )
        raise TaskError(str(e), TaskErrorSource.Celery, retry=retry)
    except TaskFailure as e:
        _logger.error("Task celery execution failed")
        raise TaskError(str(e), TaskErrorSource.Celery, retry=True)
    except Exception as e:
        _logger.exception(e)
        _logger.error("Task unknown error")
        raise TaskError(str(e), TaskErrorSource.Unknown, retry=True)


class InferenceTaskRunner(TaskRunner):
    def __init__(
        self,
        task_id: int,
        state_cache: TaskStateCache,
        queue: EventQueue,
        contracts: Optional[Contracts] = None,
        relay: Optional[Relay] = None,
        watcher: Optional[EventWatcher] = None,
    ) -> None:
        super().__init__(task_id=task_id, state_cache=state_cache, queue=queue)
        if contracts is None:
            self.contracts = get_contracts()
        else:
            self.contracts = contracts
        if relay is None:
            self.relay = get_relay()
        else:
            self.relay = relay
        if watcher is None:
            self.watcher = get_watcher()
        else:
            self.watcher = watcher

        self._state: Optional[models.TaskState] = None

        self._lock: Optional[Lock] = None

        async def _push_event(event_data: EventData):
            event = models.load_event_from_contracts(event_data)
            await self.queue.put(event)

        self._commitment_watch_id = self.watcher.watch_event(
            "task",
            "TaskResultCommitmentsReady",
            callback=_push_event,
            filter_args={"taskId": self.task_id},
        )
        self._success_watch_id = self.watcher.watch_event(
            "task",
            "TaskSuccess",
            callback=_push_event,
            filter_args={"taskId": self.task_id, "resultNode": self.contracts.account},
        )
        self._aborted_watch_id = self.watcher.watch_event(
            "task",
            "TaskAborted",
            callback=_push_event,
            filter_args={"taskId": self.task_id},
        )

    async def init(self):
        assert self._state is None, "The task runner has already been initialized."

        try:
            state = await self.cache.load(self.task_id)
            self._state = state
        except KeyError:
            self._state = models.TaskState(
                task_id=self.task_id,
                round=0,
                status=models.TaskStatus.Pending,
            )

    @asynccontextmanager
    async def state_context(self):
        try:
            yield
        finally:
            if self._state is not None:
                with fail_after(5, shield=True):
                    await self.cache.dump(task_state=self._state)

    @property
    def lock(self) -> Lock:
        if self._lock is None:
            self._lock = Lock()
        return self._lock

    async def process_event(self, event: models.TaskEvent):
        with wrap_task_error():
            async with self.lock:
                if event.kind == "TaskCreated":
                    assert isinstance(event, models.TaskCreated)
                    await self.task_created(event)
                    return False
                elif event.kind == "TaskResultReady":
                    assert isinstance(event, models.TaskResultReady)
                    await self.result_ready(event)
                    return False
                elif event.kind == "TaskResultCommitmentsReady":
                    assert isinstance(event, models.TaskResultCommitmentsReady)
                    await self.commitment_ready(event)
                    return False
                elif event.kind == "TaskSuccess":
                    assert isinstance(event, models.TaskSuccess)
                    await self.task_success(event)
                    return True
                elif event.kind == "TaskAborted":
                    assert isinstance(event, models.TaskAborted)
                    await self.task_aborted(event)
                    return True
                else:
                    raise ValueError(f"Unknown event kind {event.kind}")

    async def task_created(self, event: models.TaskCreated):
        async with self.state_context():
            assert self._state is not None, "The task runner has not been initialized."
            assert (
                self._state.status == models.TaskStatus.Pending
            ), "Task status is not pending when receive event TaskCreated."

            self._state.round = event.round

            task = await self.relay.get_task(event.task_id)

            def run_task():
                celery = get_celery()
                kwargs = {
                    "task_id": task.task_id,
                    "prompts": task.prompt,
                    "base_model": task.base_model,
                    "lora_model": task.lora_model,
                }
                if task.task_config is not None:
                    kwargs["task_config"] = task.task_config.model_dump()
                if task.pose is not None:
                    kwargs["pose"] = task.pose.model_dump()
                res: AsyncResult = celery.send_task(
                    "sd_lora_inference",
                    kwargs=kwargs,
                )
                try:
                    res.get()
                except celery_exceptions.CeleryError:
                    raise
                except Exception as e:
                    _logger.exception(e)
                    raise TaskFailure(str(e))

            await to_thread.run_sync(run_task, cancellable=True)

            self._state.status = models.TaskStatus.Executing

    async def result_ready(self, event: models.TaskResultReady):
        async with self.state_context():
            assert self._state is not None, "The task runner has not been initialized."
            assert (
                self._state.status == models.TaskStatus.Executing
            ), "Task status is not executing when receive event TaskResultReady."

            result, commitment, nonce = make_result_commitments(event.hashes)
            await self.contracts.task_contract.submit_task_result_commitment(
                task_id=self.task_id,
                round=self._state.round,
                commitment=commitment,
                nonce=nonce,
            )

            self._state.status = models.TaskStatus.ResultUploaded
            self._state.files = event.files
            self._state.result = result

    async def commitment_ready(self, event: models.TaskResultCommitmentsReady):
        async with self.state_context():
            assert self._state is not None, "The task runner has not been initialized."
            assert (
                self._state.status == models.TaskStatus.ResultUploaded
            ), "Task status is not result_uploaded when receive event TaskResultCommitmentsReady."
            assert (
                len(self._state.result) > 0
            ), "Task result not found when receive event TaskResultCommitmentsReady."
            await self.contracts.task_contract.disclose_task_result(
                task_id=self.task_id, round=self._state.round, result=self._state.result
            )

            self._state.status = models.TaskStatus.Disclosed

        self.watcher.unwatch_event(self._commitment_watch_id)

    async def task_success(self, event: models.TaskSuccess):
        async with self.state_context():
            assert self._state is not None, "The task runner has not been initialized."
            assert (
                self._state.status == models.TaskStatus.Disclosed
            ), "Task status is not disclosed when receive event TaskSuccess."

            await self.relay.upload_task_result(self.task_id, self._state.files)

            self._state.status = models.TaskStatus.Success

        await self.cleanup()

    async def task_aborted(self, event: models.TaskAborted):
        async with self.state_context():
            assert self._state is not None, "The task runner has not been initialized."

            self._state.status = models.TaskStatus.Aborted

        await self.cleanup()

    async def cleanup(self):
        assert self._state is not None, "The task runner has not been initialized."
        assert (
            self._state.status == models.TaskStatus.Success
            or self._state.status == models.TaskStatus.Aborted
        ), "Task status is not success or aborted when shutdown."

        self.watcher.unwatch_event(self._success_watch_id)
        self.watcher.unwatch_event(self._aborted_watch_id)

        def delete_result_files(files: List[str]):
            assert len(files) > 0
            dirname = os.path.dirname(files[0])
            if os.path.exists(dirname):
                shutil.rmtree(dirname)

        with fail_after(5, shield=True):
            await to_thread.run_sync(delete_result_files, self._state.files)


class TestTaskRunner(TaskRunner):
    def __init__(
        self,
        task_id: int,
        state_cache: TaskStateCache,
        queue: EventQueue
    ):
        super().__init__(task_id=task_id, state_cache=state_cache, queue=queue)

        self._state: Optional[models.TaskState] = None
        self._lock: Optional[Lock] = None

    async def init(self):
        assert self._state is None, "The task runner has already been initialized."

        try:
            state = await self.cache.load(self.task_id)
            self._state = state
        except KeyError:
            self._state = models.TaskState(
                task_id=self.task_id,
                round=0,
                status=models.TaskStatus.Pending,
            )

    @asynccontextmanager
    async def state_context(self):
        try:
            yield
        finally:
            if self._state is not None:
                with fail_after(5, shield=True):
                    await self.cache.dump(task_state=self._state)

    @property
    def lock(self) -> Lock:
        if self._lock is None:
            self._lock = Lock()
        return self._lock

    async def process_event(self, event: models.TaskEvent):
        with wrap_task_error():
            async with self.lock:
                if event.kind == "TaskCreated":
                    assert isinstance(event, models.TaskCreated)
                    await self.task_created(event)
                    return False
                elif event.kind == "TaskResultReady":
                    assert isinstance(event, models.TaskResultReady)
                    await self.result_ready(event)
                    return False
                elif event.kind == "TaskResultCommitmentsReady":
                    assert isinstance(event, models.TaskResultCommitmentsReady)
                    await self.commitment_ready(event)
                    return False
                elif event.kind == "TaskAborted":
                    assert isinstance(event, models.TaskAborted)
                    await self.task_aborted(event)
                    return True
                elif event.kind == "TaskSuccess":
                    assert isinstance(event, models.TaskSuccess)
                    await self.task_success(event)
                    return True
                else:
                    raise ValueError(f"Unknown event kind {event.kind}")

    async def task_created(self, event: models.TaskCreated):
        async with self.state_context():
            assert self._state is not None
            assert self._state.status == models.TaskStatus.Pending

            self._state.round = event.round
            self._state.status = models.TaskStatus.Executing

    async def result_ready(self, event: models.TaskResultReady):
        async with self.state_context():
            assert self._state is not None
            assert self._state.status == models.TaskStatus.Executing

            self._state.files = event.files
            self._state.result = b"".join([bytes.fromhex(h[2:]) for h in event.hashes])
            self._state.status = models.TaskStatus.ResultUploaded

    async def commitment_ready(self, event: models.TaskResultCommitmentsReady):
        async with self.state_context():
            assert self._state is not None
            assert self._state.status == models.TaskStatus.ResultUploaded

            self._state.status = models.TaskStatus.Disclosed

    async def task_success(self, event: models.TaskSuccess):
        async with self.state_context():
            assert self._state is not None
            assert self._state.status == models.TaskStatus.Disclosed

            self._state.status = models.TaskStatus.Success

        await self.cleanup()

    async def task_aborted(self, event: models.TaskAborted):
        async with self.state_context():
            assert self._state is not None
            self._state.status = models.TaskStatus.Aborted

        await self.cleanup()

    async def cleanup(self):
        assert self._state is not None
        self._state = None
