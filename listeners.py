import os
import re
import sys
import yaml

import lib
from lib.worker import WorkerOutput, WorkerDone, TaskResult
from lib.colorer import Colorer


color_stdout = Colorer()


class TaskResultListener(object):
    def process_result(self, obj):
        raise ValueError('override me')

    def process_timeout(self, delta_seconds):
        """ Called after delta_seconds time of inactivity """
        # optionally override
        pass


class StatisticsWatcher(TaskResultListener):
    def __init__(self, get_logfile):
        self.stats = dict()
        self.failed_tasks = []
        self.get_logfile = get_logfile

    def process_result(self, obj):
        if not isinstance(obj, TaskResult):
            return

        if obj.short_status not in self.stats:
            self.stats[obj.short_status] = 0
        self.stats[obj.short_status] += 1

        if obj.short_status == 'fail':
            self.failed_tasks.append((obj.task_id, obj.worker_name))

    def print_statistics(self):
        if self.stats:
            color_stdout('Statistics:\n', schema='test_var')
        for short_status, cnt in self.stats.items():
            color_stdout('* %s: %d\n' % (short_status, cnt), schema='test_var')

        if not self.failed_tasks:
            return

        color_stdout('Failed tasks:\n', schema='test_var')
        for task_id, worker_name in self.failed_tasks:
            # TODO: output path to reproduce file
            color_stdout('- %s# logfile: %s\n' % (yaml.safe_dump(task_id),
                         self.get_logfile(worker_name)), schema='test_var')


class LogOutputWatcher(TaskResultListener):
    def __init__(self):
        self.fds = dict()
        self.logdir = os.path.join(lib.Options().args.vardir, 'log')
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


class OutputWatcher(TaskResultListener):
    color_re = re.compile('\033' + r'\[\d(?:;\d\d)?m')

    def __init__(self):
        self.buffer = dict()

    @staticmethod
    def add_prefix(output, worker_name):
        prefix_max_len = len('[Worker "xx_replication-py"] ')
        prefix = ('[Worker "%s"] ' % worker_name).ljust(prefix_max_len)
        output = output.rstrip('\n')
        lines = [(line + '\n') for line in output.split('\n')]
        output = prefix + prefix.join(lines)
        return output

    @staticmethod
    def _write(output, worker_name):
        output = OutputWatcher.add_prefix(output, worker_name)
        sys.stdout.write(output)

    @staticmethod
    def _decolor(obj):
        return OutputWatcher.color_re.sub('', obj)

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            bufferized = self.buffer.get(obj.worker_id, '')
            if bufferized:
                OutputWatcher._write(bufferized, obj.worker_name)
            if obj.worker_id in self.buffer.keys():
                del self.buffer[obj.worker_id]
            return

        if not isinstance(obj, WorkerOutput) or obj.log_only:
            return

        bufferized = self.buffer.get(obj.worker_id, '')
        if OutputWatcher._decolor(obj.output).endswith('\n'):
            OutputWatcher._write(bufferized + obj.output, obj.worker_name)
            self.buffer[obj.worker_id] = ''
        else:
            self.buffer[obj.worker_id] = bufferized + obj.output

    def not_done_worker_ids(self):
        return self.buffer.keys()


class FailWatcher(TaskResultListener):
    def __init__(self, terminate_all_workers):
        self.terminate_all_workers = terminate_all_workers
        self.got_fail = False

    def process_result(self, obj):
        if not isinstance(obj, TaskResult):
            return

        if obj.short_status == 'fail':
            color_stdout('[Main process] Got failed test; '
                         'gently terminate all workers...\n',
                         schema='test_var')
            self.got_fail = True
            self.terminate_all_workers()


class HangError(Exception):
    pass


class HangWatcher(TaskResultListener):
    """ Terminate all workers if no output received 'no_output_times' time """

    def __init__(self, get_not_done_worker_ids, kill_all_workers, timeout):
        self.get_not_done_worker_ids = get_not_done_worker_ids
        self.kill_all_workers = kill_all_workers
        self.timeout = timeout
        self.inactivity = 0.0

    def process_result(self, obj):
        self.inactivity = 0.0

    def process_timeout(self, delta_seconds):
        self.inactivity += delta_seconds
        worker_ids = self.get_not_done_worker_ids()
        color_stdout("No output during %d seconds. List of workers don't"
                     " reported its done: %s; We will exit after %d seconds"
                     " w/o output.\n" % (self.inactivity, worker_ids,
                                         self.timeout), schema='test_var')
        if self.inactivity < self.timeout:
            return
        color_stdout('\n[Main process] No output from workers. '
                     'It seems that we hang. Send SIGKILL to workers; '
                     'exiting...\n',
                     schema='test_var')
        self.kill_all_workers()
        raise HangError()
