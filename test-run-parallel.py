#!/usr/bin/env python2


# TODOs:
# * Save output for failed tests and give it at the end.
#   * Will eliminated by prettified output?
#   * Print log files at the end?
# * Limit workers count by tests count at max.
# * Prettify output: extract build lines into log file like var/*/worker.log.
#   * Shows in on the screen only when the option '--debug' passed (separate
#     schema in Colorer).
# * Log file for inspector (useful for debugging).
# * Document how workers-task-buckets interacts and works; and possible
#   non-obvious code parts.
#   * Comment each Worker's results_queue classes.
#   * Describe how we wait workers, when exits, how select results/output from
#     workers, how and what doing listeners.
# * Can we remove globals in lib/__init__.py?
# * Raise in tarantool_connection.py in addition to unix sockets warning in
#   __init__.py?
# * Do out-of-source build work?
# * Extract parts of this file into workers_managers.py and listeners.py.
# * Count tests that are 'not_run' due to worker hang (compare received tasks
#   results w/ sent tasks).
#   * Non-zero exit code in this case (in the case when we have any 'fail' or
#     'not_run').


import os
import signal
import re
import sys
import time
import select
import random
import copy

import subprocess
import multiprocessing
from multiprocessing.queues import SimpleQueue

import lib
from lib.worker import WorkerOutput, WorkerDone, TaskResult
from lib.colorer import Colorer
from lib.utils import signame


color_stdout = Colorer()


class TaskResultListener(object):
    def process_result(self, obj):
        raise ValueError('override me')

    def process_timeout(self, delta_seconds):
        """ Called after delta_seconds time of inactivity """
        # optionally override
        pass


class StatisticsWatcher(TaskResultListener):
    def __init__(self):
        self.stats = dict()
        self.failed_tasks = []

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
            color_stdout('* %s on worker "%s"\n' % (str(task_id), worker_name),
                         schema='test_var')


class LogOutputWatcher(TaskResultListener):
    def __init__(self):
        self.fds = dict()
        self.logdir = os.path.join(lib.options.args.vardir, 'log')
        try:
            os.makedirs(self.logdir)
        except OSError:
            pass

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            self.fds[obj.worker_id].close()
            del self.fds[obj.worker_id]

        if not isinstance(obj, WorkerOutput):
            return

        if obj.worker_id not in self.fds.keys():
            filename = '%s.log' % obj.worker_name
            filepath = os.path.join(self.logdir, filename)
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

        if not isinstance(obj, WorkerOutput):
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


class WorkersManager:
    def __init__(self, buckets, max_workers_cnt, randomize):
        self.pids = []
        self.processes = []
        self.result_queues = []
        self.task_queues = []
        self.workers_cnt = 0
        self.worker_next_id = 1

        self.workers_bucket_managers = dict()
        for key, bucket in buckets.items():
            workers_bucket_manager = WorkersBucketManager(
                key, bucket, randomize)
            self.workers_bucket_managers[key] = workers_bucket_manager
            self.result_queues.append(workers_bucket_manager.result_queue)
            self.task_queues.append(workers_bucket_manager.task_queue)

        self.report_timeout = 2.0

        self.statistics = None
        self.fail_watcher = None
        self.listeners = None
        self.init_listeners()

        self.max_workers_cnt = max_workers_cnt

        self.pid_to_worker_id = dict()

        self.randomize = randomize

    def terminate_all_workers(self):
        for process in self.processes:
            if process.is_alive():
                process.terminate()

    def kill_all_workers(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def init_listeners(self):
        watch_hang = lib.options.args.no_output_timeout >= 0 and \
            not lib.options.args.gdb and \
            not lib.options.args.lldb and \
            not lib.options.args.valgrind and \
            not lib.options.args.long
        watch_fail = not lib.options.args.is_force

        self.statistics = StatisticsWatcher()
        output_watcher = OutputWatcher()
        log_output_watcher = LogOutputWatcher()
        self.listeners = [self.statistics, log_output_watcher, output_watcher]
        if watch_fail:
            self.fail_watcher = FailWatcher(self.terminate_all_workers)
            self.listeners.append(self.fail_watcher)
        if watch_hang:
            no_output_timeout = float(lib.options.args.no_output_timeout or 10)
            hang_watcher = HangWatcher(
                output_watcher.not_done_worker_ids, self.kill_all_workers,
                no_output_timeout)
            self.listeners.append(hang_watcher)

    def start(self):
        for _ in range(self.max_workers_cnt):
            self.add_worker()

    def find_nonempty_bucket_manager(self):
        workers_bucket_managers_rnd = list(
            self.workers_bucket_managers.values())
        if self.randomize:
            random.shuffle(workers_bucket_managers_rnd)
        for workers_bucket_manager in workers_bucket_managers_rnd:
            if not workers_bucket_manager.done:
                return workers_bucket_manager
        return None

    def get_workers_bucket_manager(self, worker_id):
        for workers_bucket_manager in self.workers_bucket_managers.values():
            if worker_id in workers_bucket_manager.worker_ids:
                return workers_bucket_manager
        return None

    def add_worker(self):
        # don't add new workers if fail occured and --force not passed
        if self.fail_watcher and self.fail_watcher.got_fail:
            return
        workers_bucket_manager = self.find_nonempty_bucket_manager()
        if not workers_bucket_manager:
            return
        process = workers_bucket_manager.add_worker(self.worker_next_id)
        self.processes.append(process)
        self.pids.append(process.pid)
        self.pid_to_worker_id[process.pid] = self.worker_next_id
        self.workers_cnt += 1
        self.worker_next_id += 1

    def del_worker(self, worker_id):
        workers_bucket_manager = self.get_workers_bucket_manager(worker_id)
        workers_bucket_manager.del_worker(worker_id)
        self.workers_cnt -= 1

    def wait(self):
        while self.workers_cnt > 0:
            try:
                inputs = [q._reader for q in self.result_queues]
                ready_inputs, _, _ = select.select(
                    inputs, [], [], self.report_timeout)
            except KeyboardInterrupt:
                # write output from workers to stdout
                new_listeners = []
                for listener in self.listeners:
                    if isinstance(listener, (LogOutputWatcher, OutputWatcher)):
                        listener.report_at_timeout = False
                        new_listeners.append(listener)
                self.listeners = new_listeners
                # TODO: wait for all workers even after SIGINT hit us?
                time.sleep(0.1)
                ready_inputs, _, _ = select.select(inputs, [], [], 0)
                self.invoke_listeners(inputs, ready_inputs)
                raise

            self.invoke_listeners(inputs, ready_inputs)

            new_workers_cnt = self.max_workers_cnt - self.workers_cnt
            for _ in range(new_workers_cnt):
                self.add_worker()

    def invoke_listeners(self, inputs, ready_inputs):
        if not ready_inputs:
            for listener in self.listeners:
                listener.process_timeout(self.report_timeout)
            self.check_for_dead_processes()

        for ready_input in ready_inputs:
            result_queue = self.result_queues[inputs.index(ready_input)]
            objs = []
            while not result_queue.empty():
                objs.append(result_queue.get())
            for obj in objs:
                for listener in self.listeners:
                    listener.process_result(obj)
                if isinstance(obj, WorkerDone):
                    self.del_worker(obj.worker_id)

    def check_for_dead_processes(self):
        for pid in self.pids[:]:
            exited = False
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                exited = True
            if exited:
                worker_id = self.pid_to_worker_id[pid]
                workers_bucket_manager = \
                    self.get_workers_bucket_manager(worker_id)
                if not workers_bucket_manager:
                    continue
                result_queue = workers_bucket_manager.result_queue
                result_queue.put(WorkerDone(worker_id, 'unknown'))
                color_stdout(
                    "[Main process] Worker %d don't reported work "
                    "done using results queue, but the corresponding "
                    "process seems dead. Sending fake WorkerDone marker.\n"
                        % worker_id, schema='test_var')
                self.pids.remove(pid)  # XXX: sync it w/ self.processes

    def wait_processes(self):
        for process in self.processes:
            process.join()
        self.processes = []


class WorkersBucketManager:
    def __init__(self, key, bucket, randomize):
        self.key = key
        self.gen_worker = bucket['gen_worker']
        self.task_ids = bucket['task_ids']
        self.randomize = randomize
        if self.randomize:
            random.shuffle(self.task_ids)
        self.result_queue = SimpleQueue()
        self.task_queue = SimpleQueue()
        for task_id in self.task_ids:
            self.task_queue.put(task_id)
        self.worker_ids = set()
        self.done = False

    def _run_worker(self, worker_id):
        """ for running in child process """
        color_stdout.queue = self.result_queue
        worker = self.gen_worker(worker_id)
        worker.run_all(self.task_queue, self.result_queue)

    def add_worker(self, worker_id):
        # Note: each of our workers can consume only one None, but it would
        # be good to prevent locking in case of 'bad' worker.
        self.task_queue.put(None)  # 'stop worker' marker

        entry = lambda x=self, worker_id=worker_id: x._run_worker(worker_id)

        self.worker_ids.add(worker_id)
        process = multiprocessing.Process(target=entry)
        process.start()
        return process

    def del_worker(self, worker_id):
        # TODO: join on process, remove pid from pids
        self.worker_ids.remove(worker_id)
        # mark bucket as done when the first worker done to prevent cycling
        # with add-del workers
        self.done = True


def reproduce_buckets(reproduce, all_buckets):
    # check test list and find a bucket
    found_bucket_ids = []
    if not lib.reproduce:
        raise ValueError('[reproduce] Tests list cannot be empty')
    for i, task_id in enumerate(lib.reproduce):
        for bucket_id, bucket in all_buckets.items():
            if task_id in bucket['task_ids']:
                found_bucket_ids.append(bucket_id)
                break
        if len(found_bucket_ids) != i + 1:
            raise ValueError('[reproduce] Cannot find test "%s"' %
                             str(task_id))
    found_bucket_ids = list(set(found_bucket_ids))
    if len(found_bucket_ids) < 1:
        raise ValueError('[reproduce] Cannot find any suite for given tests')
    elif len(found_bucket_ids) > 1:
        raise ValueError(
            '[reproduce] Given tests contained by different suites')

    key = found_bucket_ids[0]
    bucket = copy.deepcopy(all_buckets[key])
    bucket['task_ids'] = lib.reproduce
    return {key: bucket}


def main_loop():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    jobs = lib.options.args.jobs
    if jobs == 0:
        # faster result I got was with 2 * cpu_count
        jobs = 2 * multiprocessing.cpu_count()
    randomize = True

    buckets = lib.worker.task_buckets()
    if lib.reproduce:
        buckets = reproduce_buckets(lib.reproduce, buckets)
        jobs = 1
        randomize = False

    workers_manager = WorkersManager(buckets, jobs, randomize)
    workers_manager.start()
    try:
        workers_manager.wait()
    except KeyboardInterrupt:
        workers_manager.statistics.print_statistics()
        raise
    except HangError:
        pass
    workers_manager.statistics.print_statistics()
    workers_manager.wait_processes()


def kill_our_group():
    def pids_in_group(group_id=0):
        """ PIDs of processes the process group except my PID.
            Note: Unix only. """
        pids = []
        cmd = ['pgrep', '-g', str(group_id)]
        p = subprocess.Popen(args=cmd, stdout=subprocess.PIPE)
        for line in p.stdout:
            line = line.strip()
            if line:
                pids.append(int(line))
        pgrep_pid = p.pid
        my_pid = os.getpid()
        p.wait()
        if pgrep_pid in pids:
            pids.remove(pgrep_pid)
        if my_pid in pids:
            pids.remove(my_pid)
        return pids

    def remove_zombies(pids):
        """ Works only for childs; don't for all group's processes """
        if not pids:
            return
        color_stdout('Collecting zombies...\n', schema='test_var')
        for pid in pids:
            try:
                wpid, wstatus = os.waitpid(pid, os.WNOHANG)
                if wpid == pid and (os.WIFEXITED(wstatus)
                        or os.WIFSIGNALED(wstatus)):
                    pids.remove(pid)
            except OSError:
                pass

    def process_str(pid):
        cmdline = 'unknown'
        try:
            with open('/proc/%d/cmdline' % pid, 'r') as f:
                cmdline = ' '.join(f.read().split('\0')).strip() or cmdline
        except (OSError, IOError):
            pass
        status = 'unknown'
        try:
            with open('/proc/%d/status' % pid, 'r') as f:
                for line in f:
                    key, value = line.split(':', 1)
                    if key == 'State':
                       status = value.strip()
        except (OSError, IOError):
            pass
        return 'process %d [%s; %s]' % (pid, status, cmdline)

    def kill_pids(pids, sig):
        for pid in pids:
            color_stdout('Killing %s by %s\n' % (process_str(pid),
                signame(sig)))
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    for sig in [signal.SIGTERM, signal.SIGKILL]:
        time.sleep(0.1)
        pids = pids_in_group()
        remove_zombies(pids)
        if pids:
            color_stdout(
                '[Main process] Sending %s to processes in our process '
                'group...\n' % signame(sig), schema='test_var')
            kill_pids(pids, sig)


def main():
    try:
        main_loop()
    except KeyboardInterrupt as e:
        color_stdout('\n[Main process] Caught keyboard interrupt\n',
                     schema='test_var')
    kill_our_group()


if __name__ == "__main__":
    exit(main())
