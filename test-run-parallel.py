#!/usr/bin/env python2

import re
import sys
import time
import select
import random
import multiprocessing
from multiprocessing.queues import SimpleQueue
import copy


import lib
from lib import WorkerOutput, WorkerDone, TaskResult
from lib.colorer import Colorer


color_stdout = Colorer()


# Helpers
#########


def terminate_all_workers(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()


def kill_all_workers(pids):
    import os
    import signal
    for pid in pids:
        os.kill(pid, signal.SIGKILL)


#########
#########


class TaskResultListener(object):
    def process_result(self, *args, **kwargs):
        raise ValueError('override me')

    def process_timeout(self, *args, **kwargs):
        # optionally override
        pass


class TaskStatistics(TaskResultListener):
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
            color_stdout('* %s on worker "%s"\n' % (str(task_id),
                worker_name), schema='test_var')


class TaskOutput(TaskResultListener):
    color_re = re.compile('\033' + r'\[\d(?:;\d\d)?m')

    def __init__(self):
        self.buffer = dict()

    @staticmethod
    def _write(output, worker_name):
        #prefix_max_len = len('[Worker "xx_replication-py"] ')
        #prefix = ('[Worker "%s"] ' % name).ljust(prefix_max_len)
        #output = output.rstrip('\n')
        #lines = [(line + '\n') for line in output.split('\n')]
        #output = prefix + prefix.join(lines)
        sys.stdout.write(output)

    @staticmethod
    def _decolor(obj):
        return TaskOutput.color_re.sub('', obj)

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            bufferized = self.buffer.get(obj.worker_id, '')
            if bufferized:
                TaskOutput._write(bufferized, obj.worker_name)
            if obj.worker_id in self.buffer.keys():
                del self.buffer[obj.worker_id]
            return

        if not isinstance(obj, WorkerOutput):
            return

        bufferized = self.buffer.get(obj.worker_id, '')
        if TaskOutput._decolor(obj.output).endswith('\n'):
            TaskOutput._write(bufferized + obj.output, obj.worker_name)
            self.buffer[obj.worker_id] = ''
        else:
            self.buffer[obj.worker_id] = bufferized + obj.output

    def process_timeout(self):
        color_stdout("No output during 2 seconds. List of workers don\'t reported its done: %s\n" % \
            str(self.buffer.keys()), schema='test_var')


class FailWatcher(TaskResultListener):
    def __init__(self, terminate_all_workers):
        self.terminate_all_workers = terminate_all_workers
        self.got_fail = False

    def process_result(self, obj):
        if not isinstance(obj, TaskResult):
            return

        if obj.short_status == 'fail':
            color_stdout('[Main process] Got failed test; gently terminate all workers...\n',
                schema='test_var')
            self.got_fail = True
            self.terminate_all_workers()


class HangError(Exception):
    pass


class HangWatcher(TaskResultListener):
    """ Terminate all workers if no output received 'no_output_times' time """

    def __init__(self, no_output_times, kill_all_workers):
        self.no_output_times = no_output_times
        self.kill_all_workers = kill_all_workers
        self.cur_times = 0

    def process_result(self, obj):
        self.cur_times = 0

    def process_timeout(self):
        self.cur_times += 1
        if self.cur_times < self.no_output_times:
            return
        color_stdout('\n[Main process] Not output from workers. ' \
                     'It seems that we hang. Send SIGKILL to workers; ' \
                     'exiting...\n',
                     schema='test_var')
        self.kill_all_workers()
        raise HangError()


def run_worker(gen_worker, task_queue, result_queue, worker_id):
    color_stdout.queue = result_queue
    worker = gen_worker(worker_id)
    worker.run_all(task_queue, result_queue)


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
            raise ValueError('[reproduce] Cannot find test "%s"' % str(task_id))
    found_bucket_ids = list(set(found_bucket_ids))
    if len(found_bucket_ids) < 1:
        raise ValueError('[reproduce] Cannot find any suite for given tests')
    elif len(found_bucket_ids) > 1:
        raise ValueError('[reproduce] Given tests contained by different suites')

    key = found_bucket_ids[0]
    bucket = copy.deepcopy(all_buckets[key])
    bucket['task_ids'] = lib.reproduce
    return { key: bucket }


class Manager:
    def __init__(self, buckets, max_workers_cnt):
        self.pids = []
        self.processes = []
        self.result_queues = []
        self.task_queues = []
        self.workers_cnt = 0
        self.worker_next_id = 1

        self.bucket_managers = dict()
        for key, bucket in buckets.items():
            bucket_manager = BucketManager(key, bucket)
            self.bucket_managers[key] = bucket_manager
            self.result_queues.append(bucket_manager.result_queue)
            self.task_queues.append(bucket_manager.task_queue)

        self.report_timeout = 2.0
        self.kill_after_report_cnt = 5

        self.statistics = None
        self.fail_watcher = None
        self.listeners = None
        self.init_listeners()

        self.max_workers_cnt = max_workers_cnt

    def init_listeners(self):
        self.statistics = TaskStatistics()
        self.listeners = [self.statistics, TaskOutput()]
        term_callback = lambda x=self.processes: terminate_all_workers(x)
        kill_callback = lambda x=self.pids: kill_all_workers(x)
        if not lib.options.args.is_force:
            self.fail_watcher = FailWatcher(term_callback)
            self.listeners.append(self.fail_watcher)
        hang_watcher = HangWatcher(self.kill_after_report_cnt, kill_callback)
        self.listeners.append(hang_watcher)

    def start(self):
        for _ in range(self.max_workers_cnt):
            self.add_worker()

    def find_nonempty_bucket_manager(self):
        bucket_managers_rnd = list(self.bucket_managers.values())
        random.shuffle(bucket_managers_rnd)
        for bucket_manager in bucket_managers_rnd:
            if not bucket_manager.done:
                return bucket_manager
        return None

    def get_bucket_manager(self, worker_id):
        for bucket_manager in self.bucket_managers.values():
            if worker_id in bucket_manager.worker_ids:
                return bucket_manager
        return None

    def add_worker(self):
        # don't add new workers if fail occured and --force not passed
        if self.fail_watcher and self.fail_watcher.got_fail:
            return
        bucket_manager = self.find_nonempty_bucket_manager()
        if not bucket_manager:
            return
        process = bucket_manager.add_worker(self.worker_next_id)
        self.processes.append(process)
        self.pids.append(process.pid)
        self.workers_cnt += 1
        self.worker_next_id += 1

    def del_worker(self, worker_id):
        bucket_manager = self.get_bucket_manager(worker_id)
        bucket_manager.del_worker(worker_id)
        self.workers_cnt -= 1

    def wait(self):
        while self.workers_cnt > 0:
            inputs = [q._reader for q in self.result_queues]
            ready_inputs, _, _ = select.select(inputs, [], [], self.report_timeout)

            if not ready_inputs:
                for listener in self.listeners:
                    listener.process_timeout()

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
                        break

            new_workers_cnt = self.max_workers_cnt - self.workers_cnt
            for _ in range(new_workers_cnt):
                self.add_worker()

    def wait_processes(self):
        for process in self.processes:
            process.join()
            self.processes.remove(process)


class BucketManager:
    def __init__(self, key, bucket):
        self.key = key
        self.gen_worker = bucket['gen_worker']
        self.task_ids = bucket['task_ids']
        random.shuffle(self.task_ids)
        self.result_queue = SimpleQueue()
        self.task_queue = SimpleQueue()
        for task_id in self.task_ids:
            self.task_queue.put(task_id)
        self.worker_ids = set()
        self.done = False

    def add_worker(self, worker_id):
        # Note: each of our workers can consume only one None, but it would
        # be good to prevent locking in case of 'bad' worker.
        self.task_queue.put(None)  # 'stop worker' marker

        # It's python-style closure; XXX: prettify
        entry = lambda gen_worker=self.gen_worker, \
                task_queue=self.task_queue, result_queue=self.result_queue, \
                worker_id=worker_id: \
            run_worker(gen_worker, task_queue, result_queue, worker_id)

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

def main_loop():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    jobs = lib.options.args.jobs
    if jobs == 0:
        # faster result I got was with 2 * cpu_count
        jobs = 2 * multiprocessing.cpu_count()

    buckets = lib.task_buckets()
    if lib.reproduce:
        buckets = reproduce_buckets(lib.reproduce, buckets)
        jobs = 1

    manager = Manager(buckets, jobs)
    manager.start()
    try:
        manager.wait()
    except KeyboardInterrupt:
        manager.statistics.print_statistics()
        raise
    except HangError:
        pass
    manager.statistics.print_statistics()
    manager.wait_processes()


def main():
    try:
        main_loop()
    except KeyboardInterrupt as e:
        color_stdout('\n[Main process] Caught keyboard interrupt;' \
            ' waiting for processes for doing its clean up\n', schema='test_var')


if __name__ == "__main__":
    exit(main())
