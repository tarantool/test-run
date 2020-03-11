from __future__ import absolute_import

import os
import re
import sys
import yaml
import six

from lib import Options
from lib.colorer import color_stdout
from lib.worker import WorkerCurrentTask
from lib.worker import WorkerDone
from lib.worker import WorkerOutput
from lib.worker import WorkerTaskResult
from lib.worker import get_reproduce_file
from lib.utils import prefix_each_line
from lib.utils import print_tail_n
from lib.utils import print_unidiff


class BaseWatcher(object):
    """Base class for all listeners intended to be called when some message
    arrive to a result queue from some worker.
    """
    def process_result(self, obj):
        raise ValueError('override me')

    def process_timeout(self, delta_seconds):
        """Called after delta_seconds time of inactivity."""
        # optionally override
        pass


class StatisticsWatcher(BaseWatcher):
    def __init__(self, get_logfile):
        self.stats = dict()
        self.failed_tasks = []
        self.get_logfile = get_logfile

    def process_result(self, obj):
        if not isinstance(obj, WorkerTaskResult):
            return

        if obj.short_status not in self.stats:
            self.stats[obj.short_status] = 0
        self.stats[obj.short_status] += 1

        if obj.short_status == 'fail':
            self.failed_tasks.append((obj.task_id,
                                      obj.worker_name,
                                      obj.show_reproduce_content))

    def print_statistics(self):
        """Returns are there failed tasks."""
        if self.stats:
            color_stdout('Statistics:\n', schema='test_var')
        for short_status, cnt in six.iteritems(self.stats):
            color_stdout('* %s: %d\n' % (short_status, cnt), schema='test_var')

        if not self.failed_tasks:
            return False

        color_stdout('Failed tasks:\n', schema='test_var')
        for task_id, worker_name, show_reproduce_content in self.failed_tasks:
            logfile = self.get_logfile(worker_name)
            color_stdout('- %s' % yaml.safe_dump(task_id), schema='test_var')
            color_stdout('# logfile:        %s\n' % logfile)
            reproduce_file_path = get_reproduce_file(worker_name)
            color_stdout('# reproduce file: %s\n' % reproduce_file_path)
            if show_reproduce_content:
                color_stdout("---\n", schema='separator')
                print_tail_n(reproduce_file_path)
                color_stdout("...\n", schema='separator')

        return True


class LogOutputWatcher(BaseWatcher):
    def __init__(self):
        self.fds = dict()
        self.logdir = os.path.join(Options().args.vardir, 'log')
        try:
            os.makedirs(self.logdir)
        except OSError:
            pass

    def get_logfile(self, worker_name):
        filename = '%s.log' % worker_name
        filepath = os.path.join(self.logdir, filename)
        return os.path.realpath(filepath)

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            self.fds[obj.worker_id].close()
            del self.fds[obj.worker_id]

        if not isinstance(obj, WorkerOutput):
            return

        if obj.worker_id not in self.fds.keys():
            filepath = self.get_logfile(obj.worker_name)
            self.fds[obj.worker_id] = open(filepath, 'w')
        fd = self.fds[obj.worker_id]
        fd.write(obj.output)
        fd.flush()

    def __del__(self):
        for fd in self.fds.values():
            try:
                fd.close()
            except IOError:
                pass


class OutputWatcher(BaseWatcher):
    color_re = re.compile('\033' + r'\[\d(?:;\d\d)?m')

    def __init__(self):
        self.buffer = dict()

    @staticmethod
    def add_prefix(output, worker_id):
        prefix_max_len = len('[xxx] ')
        prefix = ('[%03d] ' % worker_id).ljust(prefix_max_len)
        return prefix_each_line(prefix, output)

    @staticmethod
    def _write(output, worker_id):
        output = OutputWatcher.add_prefix(output, worker_id)
        sys.stdout.write(output)

    @staticmethod
    def _decolor(obj):
        return OutputWatcher.color_re.sub('', obj)

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            bufferized = self.buffer.get(obj.worker_id, '')
            if bufferized:
                OutputWatcher._write(bufferized, obj.worker_id)
            if obj.worker_id in self.buffer.keys():
                del self.buffer[obj.worker_id]
            return

        if not isinstance(obj, WorkerOutput) or obj.log_only:
            return

        bufferized = self.buffer.get(obj.worker_id, '')
        if OutputWatcher._decolor(obj.output).endswith('\n'):
            OutputWatcher._write(bufferized + obj.output, obj.worker_id)
            self.buffer[obj.worker_id] = ''
        else:
            self.buffer[obj.worker_id] = bufferized + obj.output

    def not_done_worker_ids(self):
        return list(self.buffer.keys())


class FailWatcher(BaseWatcher):
    def __init__(self, terminate_all_workers):
        self.terminate_all_workers = terminate_all_workers
        self.got_fail = False

    def process_result(self, obj):
        if not isinstance(obj, WorkerTaskResult):
            return

        if obj.short_status == 'fail':
            color_stdout('[Main process] Got failed test; '
                         'gently terminate all workers...\n',
                         schema='test_var')
            self.got_fail = True
            self.terminate_all_workers()


class HangError(Exception):
    pass


class HangWatcher(BaseWatcher):
    """Terminate all workers if no output received 'no_output_times' time."""
    def __init__(self, get_not_done_worker_ids, kill_all_workers,
                 warn_timeout, kill_timeout):
        self.get_not_done_worker_ids = get_not_done_worker_ids
        self.kill_all_workers = kill_all_workers
        self.warn_timeout = warn_timeout
        self.kill_timeout = kill_timeout
        self.warned_seconds_ago = 0.0
        self.inactivity = 0.0
        self.worker_current_task = dict()

    def process_result(self, obj):
        self.warned_seconds_ago = 0.0
        self.inactivity = 0.0

        if isinstance(obj, WorkerCurrentTask):
            self.worker_current_task[obj.worker_id] = obj

    def process_timeout(self, delta_seconds):
        self.warned_seconds_ago += delta_seconds
        self.inactivity += delta_seconds
        worker_ids = self.get_not_done_worker_ids()

        if self.warned_seconds_ago < self.warn_timeout:
            return

        is_warning = self.inactivity < self.kill_timeout

        color_stdout(
            "No output during {0.inactivity:.0f} seconds. "
            "Will abort after {0.kill_timeout:.0f} seconds without output. "
            "List of workers not reporting the status:\n".format(self),
            schema=('test_var' if is_warning else 'error'))

        hung_tasks = [task for worker_id, task
                      in six.iteritems(self.worker_current_task)
                      if worker_id in worker_ids]
        for task in hung_tasks:
            with open(task.task_tmp_result, 'r') as f:
                lines = sum(1 for _ in f)
            color_stdout("- {0} [{1}, {2}] at {3}:{4}\n".format(
                task.worker_name, task.task_name, task.task_param,
                task.task_tmp_result, lines),
                schema=('test_var' if is_warning else 'error'))

        self.warned_seconds_ago = 0.0

        if is_warning:
            return

        for task in hung_tasks:
            color_stdout("Test hung! Result content mismatch:\n",
                         schema='error')
            print_unidiff(task.task_result, task.task_tmp_result)
        color_stdout('\n[Main process] No output from workers. '
                     'It seems that we hang. Send SIGKILL to workers; '
                     'exiting...\n', schema='error')
        self.kill_all_workers()

        raise HangError()
