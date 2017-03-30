import os
import signal
import time
import select
import random
import functools

import multiprocessing
from multiprocessing.queues import SimpleQueue

import listeners
import lib
from lib.worker import WorkerDone
from lib.colorer import Colorer


color_stdout = Colorer()


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

        log_output_watcher = listeners.LogOutputWatcher()
        self.statistics = listeners.StatisticsWatcher(
            log_output_watcher.get_logfile)
        output_watcher = listeners.OutputWatcher()
        self.listeners = [self.statistics, log_output_watcher, output_watcher]
        if watch_fail:
            self.fail_watcher = listeners.FailWatcher(
                self.terminate_all_workers)
            self.listeners.append(self.fail_watcher)
        if watch_hang:
            no_output_timeout = float(lib.options.args.no_output_timeout or 10)
            hang_watcher = listeners.HangWatcher(
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
                    if isinstance(listener, (listeners.LogOutputWatcher,
                                             listeners.OutputWatcher)):
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

        entry = functools.partial(self._run_worker, worker_id)

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
