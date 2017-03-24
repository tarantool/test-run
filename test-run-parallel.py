#!/usr/bin/env python2

import re
import sys
import time
import select
import multiprocessing
from multiprocessing.queues import SimpleQueue
import copy


import lib
from lib import WorkerOutput, WorkerDone
from lib.colorer import Colorer


color_stdout = Colorer()


class TaskResultListener(object):
    def process_result(self, *args, **kwargs):
        raise ValueError('override me')


class TaskStatistics(TaskResultListener):
    def __init__(self):
        self.stats = dict()

    # XXX: remove worker_name
    def process_result(self, worker_name, obj):
        if not isinstance(obj, bool):
            return

        if not worker_name in self.stats.keys():
            self.stats[worker_name] = {
                'pass': 0,
                'othr': 0,
            }
        if obj:
            self.stats[worker_name]['pass'] += 1
        else:
            self.stats[worker_name]['othr'] += 1

    def print_statistics(self):
        color_stdout('Statistics: %s\n' % str(self.stats), schema='test_var')


class TaskOutput(TaskResultListener):
    color_re = re.compile('\033' + r'\[\d(?:;\d\d)?m')

    def __init__(self):
        self.buffer = dict()

    @staticmethod
    def _write(name, output):
        #prefix_max_len = len('[Worker "xx_replication-py"] ')
        #prefix = ('[Worker "%s"] ' % name).ljust(prefix_max_len)
        #output = output.rstrip('\n')
        #lines = [(line + '\n') for line in output.split('\n')]
        #output = prefix + prefix.join(lines)
        sys.stdout.write(output)

    @staticmethod
    def _decolor(obj):
        return TaskOutput.color_re.sub('', obj)

    # XXX: remove worker_name arg
    def process_result(self, worker_name, obj):
        if isinstance(obj, WorkerDone):
            bufferized = self.buffer.get(obj.worker_id, '')
            if bufferized:
                TaskOutput._write(obj.worker_name, bufferized)
            return

        if not isinstance(obj, WorkerOutput):
            return

        bufferized = self.buffer.get(obj.worker_id, '')
        if TaskOutput._decolor(obj.output).endswith('\n'):
            TaskOutput._write(obj.worker_name, bufferized + obj.output)
            self.buffer[obj.worker_id] = ''
        else:
            self.buffer[obj.worker_id] = bufferized + obj.output


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


def start_workers(processes, task_queues, result_queues, buckets):
    workers_per_suite = 2
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
            processes.append(process)


def wait_result_queues(processes, task_queues, result_queues):
    inputs = [q._reader for q in result_queues]
    workers_cnt = len(processes)
    statistics = TaskStatistics()
    listeners = [statistics, TaskOutput()]
    while workers_cnt > 0:
        ready_inputs, _, _ = select.select(inputs, [], [])
        for ready_input in ready_inputs:
            result_queue = result_queues[inputs.index(ready_input)]
            objs = []
            while not result_queue.empty():
                objs.append(result_queue.get())
            for obj in objs:
                worker_name = inputs.index(ready_input) # XXX: tmp
                for listener in listeners:
                    listener.process_result(worker_name, obj)
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
    if lib.reproduce:
        buckets = reproduce_buckets(lib.reproduce, buckets)
        # TODO: when several workers will able to work on one task queue we
        #       need to limit workers count to 1 when reproducing
    start_workers(processes, task_queues, result_queues, buckets)

    if not processes:
        return

    statistics = wait_result_queues(processes, task_queues, result_queues)
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


if __name__ == "__main__":
    exit(main())
