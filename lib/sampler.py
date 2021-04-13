import os
import sys
import time

from lib.colorer import color_log
from lib.colorer import qa_notice
from lib.utils import format_process
from lib.utils import get_proc_stat_rss
from lib.utils import proc_stat_rss_supported


if sys.version_info[0] == 2:
    ProcessLookupError = OSError


# Don't inherit BaseWorkerMessage to bypass cyclic import.
class RegisterProcessMessage(object):
    """Ask the sampler in the main test-run process to register
       given process.
    """
    def __init__(self, worker_id, worker_name, pid, task_id, server_name):
        self.worker_id = worker_id
        self.worker_name = worker_name
        self.pid = pid
        self.task_id = task_id
        self.server_name = server_name


# Don't inherit BaseWatcher to bypass cyclic import.
class SamplerWatcher(object):
    def __init__(self, sampler):
        self._sampler = sampler
        self._last_sample = 0
        self._sample_interval = 0.1  # seconds
        self._warn_interval = self._sample_interval * 4

    def process_result(self, obj):
        if isinstance(obj, RegisterProcessMessage):
            self._sampler.register_process(
                obj.pid, obj.task_id, obj.server_name, obj.worker_id,
                obj.worker_name)
        self._wakeup()

    def process_timeout(self, delta_seconds):
        self._wakeup()

    @property
    def sample_interval(self):
        return self._sample_interval

    def _wakeup(self):
        """Invoke Sampler.sample() if enough time elapsed since
           the previous call.
        """
        now = time.time()
        delta = now - self._last_sample
        if self._last_sample > 0 and delta > self._warn_interval:
            template = 'Low sampling resolution. The expected interval\n' + \
                       'is {:.2f} seconds ({:.2f} seconds without warnings),\n' + \
                       'but the last sample was collected {:.2f} seconds ago.'
            qa_notice(template.format(self._sample_interval, self._warn_interval,
                                      delta))
        if delta > self._sample_interval:
            self._sampler._sample()
            self._last_sample = now


class Sampler:
    def __init__(self):
        # The instance is created in the test-run main process.

        # Field for an instance in a worker.
        self._worker_id = None
        self._worker_name = None
        self._queue = None

        # Field for an instance in the main process.
        self._watcher = SamplerWatcher(self)

        self._processes = dict()
        self._rss_summary = dict()

    def set_queue(self, queue, worker_id, worker_name):
        # Called from a worker process (_run_worker()).
        self._worker_id = worker_id
        self._worker_name = worker_name
        self._queue = queue
        self._watcher = None

    @property
    def rss_summary(self):
        """Task ID to maximum RSS mapping."""
        return self._rss_summary

    @property
    def sample_interval(self):
        return self._watcher.sample_interval

    @property
    def watcher(self):
        if not self._watcher:
            raise RuntimeError('sampler: watcher is available only in the ' +
                               'main test-run process')
        return self._watcher

    @property
    def is_enabled(self):
        return proc_stat_rss_supported()

    def register_process(self, pid, task_id, server_name, worker_id=None,
                         worker_name=None):
        """Register a process to sampling.

           Call it without worker_* arguments from a worker
           process.
        """
        if not self._queue:
            # In main test-run process.
            self._processes[pid] = {
                'task_id': task_id,
                'server_name': server_name,
                'worker_id': worker_id,
                'worker_name': worker_name,
            }
            self._log('register', pid)
            return

        # Pass to the main test-run process.
        self._queue.put(RegisterProcessMessage(
            self._worker_id, self._worker_name, pid, task_id, server_name))

    def unregister_process(self, pid):
        if self._queue:
            raise NotImplementedError('sampler: a process unregistration ' +
                                      'from a test-run worker is not ' +
                                      'implemented yet')
        if pid not in self._processes:
            return

        self._log('unregister', pid)
        del self._processes[pid]

    def _log(self, event, pid):
        # Those logs are not written due to gh-247.
        process_def = self._processes[pid]
        task_id = process_def['task_id']
        test_name = task_id[0] + ((':' + task_id[1]) if task_id[1] else '')
        worker_name = process_def['worker_name']
        server_name = process_def['server_name']
        color_log('DEBUG: sampler: {} {}\n'.format(
                  event, format_process(pid)), schema='info')
        color_log(' | worker: {}\n'.format(worker_name))
        color_log(' | test: {}\n'.format(test_name))
        color_log(' | server: {}\n'.format(str(server_name)))

    def _sample(self):
        tasks_rss = dict()
        for pid in list(self._processes.keys()):
            # Unregister processes that're gone.
            # Assume that PIDs are rarely reused.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                self.unregister_process(pid)
            else:
                self._sample_process(pid, tasks_rss)

        # Save current overall RSS value if it is bigger than saved.
        for task_id in tasks_rss:
            if self.rss_summary.get(task_id, 0) < tasks_rss[task_id]:
                self.rss_summary[task_id] = tasks_rss[task_id]

    def _sample_process(self, pid, tasks_rss):
        task_id = self._processes[pid]['task_id']
        # Count overall RSS per task.
        tasks_rss[task_id] = get_proc_stat_rss(pid) + tasks_rss.get(task_id, 0)


# The 'singleton' sampler instance: created in the main test-run
# process, but then work differently in the main process and
# workers.
sampler = Sampler()
