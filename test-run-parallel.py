#!/usr/bin/env python2

import re
import sys
import time
import select
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

    def process_result(self, obj):
        if not isinstance(obj, TaskResult):
            return

        if obj.short_status == 'fail':
            color_stdout('[Main process] Got failed test; gently terminate all workers...\n',
                schema='test_var')
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


def start_workers(processes, task_queues, result_queues, buckets,
        workers_per_suite):
    pids = []
    worker_next_id = 1
    for bucket in buckets.values():
        task_ids = bucket['task_ids']
        if not task_ids:
            continue
        result_queue = SimpleQueue()
        result_queues.append(result_queue)
        task_queue = SimpleQueue()
        task_queues.append(task_queue)
        for task_id in task_ids:
            task_queue.put(task_id)

        for _ in range(workers_per_suite):
            # Note: each of our workers can consume only one None, but it would
            # be good to prevent locking in case of 'bad' worker.
            task_queue.put(None)  # 'stop worker' marker

            # It's python-style closure; XXX: prettify
            entry = lambda gen_worker=bucket['gen_worker'], \
                    task_queue=task_queue, result_queue=result_queue, \
                    worker_next_id=worker_next_id: \
                run_worker(gen_worker, task_queue, result_queue, worker_next_id)
            worker_next_id += 1

            process = multiprocessing.Process(target=entry)
            process.start()
            pids.append(process.pid)
            processes.append(process)
    return pids


def wait_result_queues(processes, task_queues, result_queues, pids):
    report_timeout = 2.0
    inputs = [q._reader for q in result_queues]
    workers_cnt = len(processes)
    statistics = TaskStatistics()
    listeners = [statistics, TaskOutput()]
    term_callback = lambda x=processes: terminate_all_workers(x)
    kill_callback = lambda x=pids: kill_all_workers(x)
    if not lib.options.args.is_force:
        fail_watcher = FailWatcher(term_callback)
        listeners.append(fail_watcher)
    hang_watcher = HangWatcher(5, kill_callback)
    listeners.append(hang_watcher)
    while workers_cnt > 0:
        ready_inputs, _, _ = select.select(inputs, [], [], report_timeout)
        if not ready_inputs:
            for listener in listeners:
                try:
                    listener.process_timeout()
                except HangError:
                    return statistics
        for ready_input in ready_inputs:
            result_queue = result_queues[inputs.index(ready_input)]
            objs = []
            while not result_queue.empty():
                objs.append(result_queue.get())
            for obj in objs:
                for listener in listeners:
                    listener.process_result(obj)
                if isinstance(obj, WorkerDone):
                    workers_cnt -= 1
                    break
    return statistics


def main_loop():
    processes = []
    task_queues = []
    result_queues = []

    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    buckets = lib.task_buckets()
    workers_per_suite = 2
    if lib.reproduce:
        buckets = reproduce_buckets(lib.reproduce, buckets)
        workers_per_suite = 1
    pids = start_workers(processes, task_queues, result_queues, buckets,
        workers_per_suite)

    if not processes:
        return

    statistics = wait_result_queues(processes, task_queues, result_queues,
                                    pids)
    statistics.print_statistics()

    for process in processes:
        process.join()
        processes.remove(process)


def main():
    try:
        main_loop()
    except KeyboardInterrupt as e:
        color_stdout('\n[Main process] Caught keyboard interrupt;' \
            ' waiting for processes for doing its clean up\n', schema='test_var')
        # TODO: print statistics


if __name__ == "__main__":
    exit(main())
