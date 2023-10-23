import os
import socket
import pickle
import uuid
import logging

from abc import ABCMeta, abstractmethod

from typing import Optional

from parsl.serialize import serialize

_db_manager_excepts: Optional[Exception]


logger = logging.getLogger(__name__)

# need to be careful about thread-safety here:
# there will be multiple radio instances writing
# to this, along with (eg in thread local case)
# potentially many result deliverers.
# in that latter case, should there be per-task-id
# segregation of who sends which results back? or
# do we just care about *anyone* can send the results
# back, first come first serve?

# There are potentials for duplicates here when the
# queue is split into two queues at fork time when
# it already has results, and then those two copies
# of the results are merged again at result send
# time. To fix that, probably de-duplication should
# happen at return time?
result_radio_queue = []


class MonitoringRadio(metaclass=ABCMeta):
    @abstractmethod
    def send(self, message: object) -> None:
        pass


class FilesystemRadio(MonitoringRadio):
    """A MonitoringRadio that sends messages over a shared filesystem.

    The messsage directory structure is based on maildir,
    https://en.wikipedia.org/wiki/Maildir

    The writer creates a message in tmp/ and then when it is fully
    written, moves it atomically into new/

    The reader ignores tmp/ and only reads and deletes messages from
    new/

    This avoids a race condition of reading partially written messages.

    This radio is likely to give higher shared filesystem load compared to
    the UDPRadio, but should be much more reliable.
    """

    def __init__(self, *, monitoring_url: str, source_id: int, timeout: int = 10, run_dir: str):
        logger.info("filesystem based monitoring channel initializing")
        self.source_id = source_id
        self.base_path = f"{run_dir}/monitor-fs-radio/"
        self.tmp_path = f"{self.base_path}/tmp"
        self.new_path = f"{self.base_path}/new"

        os.makedirs(self.tmp_path, exist_ok=True)
        os.makedirs(self.new_path, exist_ok=True)

    def send(self, message: object) -> None:
        logger.info("Sending a monitoring message via filesystem")

        unique_id = str(uuid.uuid4())

        tmp_filename = f"{self.tmp_path}/{unique_id}"
        new_filename = f"{self.new_path}/{unique_id}"
        buffer = (message, "NA")

        # this will write the message out then atomically
        # move it into new/, so that a partially written
        # file will never be observed in new/
        with open(tmp_filename, "wb") as f:
            f.write(serialize(buffer))
        os.rename(tmp_filename, new_filename)


import chronopy
# TODO: this should encapsulate chronopy state (eg ChronoLog handles) in some
# object rather than being global. but it doesn't. so don't chronopy.start()
# multiple times.
chronopy.start()

class HTEXRadio(MonitoringRadio):

    def __init__(self, monitoring_url: str, source_id: int, timeout: int = 10):
        """
        Parameters
        ----------

        monitoring_url : str
            URL of the form <scheme>://<IP>:<PORT>
        source_id : str
            String identifier of the source
        timeout : int
            timeout, default=10s
        """
        self.source_id = source_id
        logger.info("htex-based monitoring channel initialising")

    def send(self, message: object) -> None:
        """ Sends a message to the UDP receiver

        Parameter
        ---------

        message: object
            Arbitrary pickle-able object that is to be sent

        Returns:
            None
        """

        import parsl.executors.high_throughput.monitoring_info

        result_queue = parsl.executors.high_throughput.monitoring_info.result_queue

        # this message needs to go in the result queue tagged so that it is treated
        # i) as a monitoring message by the interchange, and then further more treated
        # as a RESOURCE_INFO message when received by monitoring (rather than a NODE_INFO
        # which is the implicit default for messages from the interchange)

        # for the interchange, the outer wrapper, this needs to be a dict:

        stringified = str(message)

        chronopy.send(stringified)

        return


class ResultsRadio(MonitoringRadio):
    def __init__(self, monitoring_url: str, source_id: int, timeout: int = 10):
        pass

    def send(self, message: object) -> None:
        global result_radio_queue
        result_radio_queue.append(message)
        # raise RuntimeError(f"BENC: appended {message} to {result_radio_queue}")


class UDPRadio(MonitoringRadio):

    def __init__(self, monitoring_url: str, source_id: int, timeout: int = 10):
        """
        Parameters
        ----------

        monitoring_url : str
            URL of the form <scheme>://<IP>:<PORT>
        source_id : str
            String identifier of the source
        timeout : int
            timeout, default=10s
        """
        self.monitoring_url = monitoring_url
        self.sock_timeout = timeout
        self.source_id = source_id
        try:
            self.scheme, self.ip, port = (x.strip('/') for x in monitoring_url.split(':'))
            self.port = int(port)
        except Exception:
            raise Exception("Failed to parse monitoring url: {}".format(monitoring_url))

        self.sock = socket.socket(socket.AF_INET,
                                  socket.SOCK_DGRAM,
                                  socket.IPPROTO_UDP)  # UDP
        self.sock.settimeout(self.sock_timeout)

    def send(self, message: object) -> None:
        """ Sends a message to the UDP receiver

        Parameter
        ---------

        message: object
            Arbitrary pickle-able object that is to be sent

        Returns:
            None
        """
        try:
            buffer = pickle.dumps(message)
        except Exception:
            logging.exception("Exception during pickling", exc_info=True)
            return

        try:
            self.sock.sendto(buffer, (self.ip, self.port))
        except socket.timeout:
            logging.error("Could not send message within timeout limit")
            return
        return
