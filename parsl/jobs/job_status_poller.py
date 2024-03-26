import logging
import parsl
import time
import zmq
from typing import Dict, List, Sequence, Optional, Union

from parsl.jobs.states import JobStatus, JobState
from parsl.jobs.strategy import Strategy
from parsl.executors.status_handling import BlockProviderExecutor
from parsl.monitoring.message_type import MessageType


from parsl.utils import Timer


logger = logging.getLogger(__name__)


class PolledExecutorFacade:
    def __init__(self, executor: BlockProviderExecutor, dfk: Optional["parsl.dataflow.dflow.DataFlowKernel"] = None):
        self._executor = executor
        self._dfk = dfk
        self.first = True

        # Create a ZMQ channel to send poll status to monitoring
        self.monitoring_enabled = False
        if self._dfk and self._dfk.monitoring is not None:
            self.monitoring_enabled = True
            hub_address = self._dfk.hub_address
            hub_port = self._dfk.hub_zmq_port
            context = zmq.Context()
            self.hub_channel = context.socket(zmq.DEALER)
            self.hub_channel.set_hwm(0)
            self.hub_channel.connect("tcp://{}:{}".format(hub_address, hub_port))
            logger.info("Monitoring enabled on job status poller")

    def poll(self, now: float) -> None:
        previous_status = self.executor._poller_mutable_status

        if self.executor._should_poll(now):
            self.executor._poller_mutable_status = self._executor.status()
            self.executor._last_poll_time = now

        if previous_status != self.executor._poller_mutable_status:
            # short circuit the case where the two objects are identical so
            # delta_status must end up empty.

            delta_status = {}
            for block_id in self.executor._poller_mutable_status:
                if block_id not in previous_status \
                   or previous_status[block_id].state != self.executor._poller_mutable_status[block_id].state:
                    delta_status[block_id] = self.executor._poller_mutable_status[block_id]

            if delta_status:
                self.send_monitoring_info(delta_status)

    def send_monitoring_info(self, status: Dict) -> None:
        # Send monitoring info for HTEX when monitoring enabled
        if self.monitoring_enabled:
            msg = self._executor.create_monitoring_info(status)
            logger.debug("Sending message {} to hub from job status poller".format(msg))
            self.hub_channel.send_pyobj((MessageType.BLOCK_INFO, msg))

    @property
    def status(self) -> Dict[str, JobStatus]:
        """Return the status of all jobs/blocks of the executor of this poller.

        :return: a dictionary mapping block ids (in string) to job status
        """
        return self.executor._poller_mutable_status

    @property
    def executor(self) -> BlockProviderExecutor:
        return self._executor

    def scale_in(self, n: int, max_idletime: Optional[float] = None) -> List[str]:

        if max_idletime is None:
            block_ids = self._executor.scale_in(n)
        else:
            # This is a HighThroughputExecutor-specific interface violation.
            # This code hopes, through pan-codebase reasoning, that this
            # scale_in method really does come from HighThroughputExecutor,
            # and so does have an extra max_idletime parameter not present
            # in the executor interface.
            block_ids = self._executor.scale_in(n, max_idletime=max_idletime)  # type: ignore[call-arg]
        if block_ids is not None:
            new_status = {}
            for block_id in block_ids:
                new_status[block_id] = JobStatus(JobState.CANCELLED)
                del self.executor._poller_mutable_status[block_id]
            self.send_monitoring_info(new_status)
        return block_ids

    def scale_out(self, n: int) -> List[str]:
        block_ids = self._executor.scale_out(n)
        if block_ids is not None:
            new_status = {}
            for block_id in block_ids:
                new_status[block_id] = JobStatus(JobState.PENDING)
            self.send_monitoring_info(new_status)
            self.executor._poller_mutable_status.update(new_status)
        return block_ids

    def __repr__(self) -> str:
        return self.executor._poller_mutable_status.__repr__()


class JobStatusPoller(Timer):
    def __init__(self, *, strategy: Optional[str], max_idletime: float,
                 strategy_period: Union[float, int],
                 dfk: Optional["parsl.dataflow.dflow.DataFlowKernel"] = None) -> None:
        self._executor_facades = []  # type: List[PolledExecutorFacade]
        self.dfk = dfk
        self._strategy = Strategy(strategy=strategy,
                                  max_idletime=max_idletime)
        super().__init__(self.poll, interval=strategy_period, name="JobStatusPoller")

    def poll(self) -> None:
        self._update_state()
        self._run_error_handlers(self._executor_facades)
        self._strategy.strategize(self._executor_facades)

    def _run_error_handlers(self, status: List[PolledExecutorFacade]) -> None:
        for es in status:
            es.executor.handle_errors(es.status)

    def _update_state(self) -> None:
        now = time.time()
        for item in self._executor_facades:
            item.poll(now)

    def add_executors(self, executors: Sequence[BlockProviderExecutor]) -> None:
        for executor in executors:
            if executor.status_polling_interval > 0:
                logger.debug("Adding executor {}".format(executor.label))
                self._executor_facades.append(PolledExecutorFacade(executor, self.dfk))
        self._strategy.add_executors(executors)

    def close(self):
        super().close()
        for ef in self._executor_facades:
            if not ef.executor.bad_state_is_set:
                logger.info(f"Scaling in executor {ef.executor.label}")

                # this code needs to be at least as many blocks as need
                # cancelling, but it is safe to be more, as the scaling
                # code will cope with being asked to cancel more blocks
                # than exist.
                block_count = len(ef.status)
                ef.scale_in(block_count)

            else:  # and bad_state_is_set
                logger.warning(f"Not scaling in executor {ef.executor.label} because it is in bad state")
