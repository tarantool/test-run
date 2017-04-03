import os
import signal
import time
import select
import random
import functools
import yaml

import multiprocessing
from multiprocessing.queues import SimpleQueue

import listeners
import lib
from lib.worker import WorkerTaskResult, WorkerDone
from lib.colorer import color_stdout


class Dispatcher:
    """Run specified count of worker processes ('max_workers_cnt' arg), pass
    task IDs (via 'task_queue'), receive results and output (via
    'result_queue') and pass it to listeners. Workers as well as tasks have
    types and certain task can be run only on worker of that type. To being
    abstract we get 'task_groups' argument contains worker generators (the
    callable working as factory of workers) and IDs of task that can be
    executed on such workers. The structure of this argument is the following:
    ```
    task_groups = {
        'some_key_1': {
            'gen_worker': gen_worker_1,
            'task_ids': task_ids_1,
        }
        ...
    }

    ```
    Usage (simplified and w/o exception catching):
    ```
    task_groups = ...
    dispatcher = Dispatcher(task_groups, max_workers_count=8, randomize=True)
    dispatcher.start()
    dispatcher.wait()
    dispatcher.statistics.print_statistics()
    dispatcher.wait_processes()
    ```
    """
    def __init__(self, task_groups, max_workers_cnt, randomize):
        self.pids = []
        self.processes = []
        self.result_queues = []
        self.task_queues = []
        self.workers_cnt = 0
        self.worker_next_id = 1

        tasks_cnt = 0
        self.task_queue_disps = dict()
        for key, task_group in task_groups.items():
            tasks_cnt += len(task_group['task_ids'])
            task_queue_disp = TaskQueueDispatcher(key, task_group, randomize)
            self.task_queue_disps[key] = task_queue_disp
            self.result_queues.append(task_queue_disp.result_queue)
            self.task_queues.append(task_queue_disp.task_queue)

        self.report_timeout = 2.0

        self.statistics = None
        self.fail_watcher = None
        self.listeners = None
        self.init_listeners()

        self.max_workers_cnt = min(max_workers_cnt, tasks_cnt)

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
        args = lib.Options().args
        watch_hang = args.no_output_timeout >= 0 and \
            not args.gdb and \
            not args.lldb and \
            not args.valgrind and \
            not args.long
        watch_fail = not lib.Options().args.is_force

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
            no_output_timeout = float(args.no_output_timeout or 10)
            hang_watcher = listeners.HangWatcher(
                output_watcher.not_done_worker_ids, self.kill_all_workers,
                no_output_timeout)
            self.listeners.append(hang_watcher)

    def start(self):
        for _ in range(self.max_workers_cnt):
            self.add_worker()

    def find_nonempty_task_queue_disp(self):
        """Find TaskQueueDispatcher that doesn't reported it's 'done' (don't
        want more workers created for working on its task queue).
        """
        task_queue_disps_rnd = list(
            self.task_queue_disps.values())
        if self.randomize:
            random.shuffle(task_queue_disps_rnd)
        for task_queue_disp in task_queue_disps_rnd:
            if not task_queue_disp.done:
                return task_queue_disp
        return None

    def get_task_queue_disp(self, worker_id):
        """Get TaskQueueDispatcher instance which contains certain worker by
        worker_id.
        """
        for task_queue_disp in self.task_queue_disps.values():
            if worker_id in task_queue_disp.worker_ids:
                return task_queue_disp
        return None

    def add_worker(self):
        # don't add new workers if fail occured and --force not passed
        if self.fail_watcher and self.fail_watcher.got_fail:
            return
        task_queue_disp = self.find_nonempty_task_queue_disp()
        if not task_queue_disp:
            return
        process = task_queue_disp.add_worker(self.worker_next_id)
        self.processes.append(process)
        self.pids.append(process.pid)
        self.pid_to_worker_id[process.pid] = self.worker_next_id
        self.workers_cnt += 1
        self.worker_next_id += 1

    def del_worker(self, worker_id):
        task_queue_disp = self.get_task_queue_disp(worker_id)
        task_queue_disp.del_worker(worker_id)
        self.workers_cnt -= 1

    def mark_task_done(self, worker_id, task_id):
        task_queue_disp = self.get_task_queue_disp(worker_id)
        task_queue_disp.mark_task_done(task_id)

    def undone_tasks(self):
        res = []
        for task_queue_disp in self.task_queue_disps.values():
            res.extend(task_queue_disp.undone_tasks())
        return res

    def report_undone(self, verbose):
        undone = self.undone_tasks()
        if not bool(undone):
            return False
        if verbose:
            color_stdout(
                'The following tasks were dispatched on some worker task '
                'queue, but were not reported as done (does not matters '
                'success or fail):\n', schema='test_var')
            for task_id in undone:
                color_stdout('- %s' % yaml.safe_dump(task_id))
        else:
            color_stdout("Count of didn't processed tasks: %d\n"
                         % len(undone), schema='test_var')
        return True

    def wait(self):
        """Wait all workers reported its done via result_queues. But in the
        case when some worker process terminated prematurely 'invoke_listeners'
        can add fake WorkerDone markers (see also 'check_for_dead_processes').
        """
        while self.workers_cnt > 0:
            try:
                inputs = [q._reader for q in self.result_queues]
                ready_inputs, _, _ = select.select(
                    inputs, [], [], self.report_timeout)
            except KeyboardInterrupt:
                self.flush_ready(inputs)
                raise

            objs = self.invoke_listeners(inputs, ready_inputs)
            for obj in objs:
                if isinstance(obj, WorkerTaskResult):
                    self.mark_task_done(obj.worker_id, obj.task_id)
                elif isinstance(obj, WorkerDone):
                    self.del_worker(obj.worker_id)

            new_workers_cnt = self.max_workers_cnt - self.workers_cnt
            for _ in range(new_workers_cnt):
                self.add_worker()

    def invoke_listeners(self, inputs, ready_inputs):
        """Returns received objects from result queue to allow Dispatcher
        update its structures.
        """
        # process timeout
        if not ready_inputs:
            for listener in self.listeners:
                listener.process_timeout(self.report_timeout)
            self.check_for_dead_processes()
            return []

        # collect received objects
        objs = []
        for ready_input in ready_inputs:
            result_queue = self.result_queues[inputs.index(ready_input)]
            while not result_queue.empty():
                objs.append(result_queue.get())

        # process received objects
        for obj in objs:
            for listener in self.listeners:
                listener.process_result(obj)

        return objs

    def flush_ready(self, inputs):
        """Write output from workers to stdout."""
        # leave only output listeners in self.listeners
        save_listeners = self.listeners
        new_listeners = []
        for listener in self.listeners:
            if isinstance(listener, (listeners.LogOutputWatcher,
                                     listeners.OutputWatcher)):
                listener.report_at_timeout = False
                new_listeners.append(listener)
        self.listeners = new_listeners
        # wait some time until processes in our group get its SIGINTs and give
        # us some last output
        time.sleep(0.1)
        # collect and process ready inputs
        ready_inputs, _, _ = select.select(inputs, [], [], 0)
        self.invoke_listeners(inputs, ready_inputs)

    def check_for_dead_processes(self):
        for pid in self.pids[:]:
            exited = False
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                exited = True
            if exited:
                worker_id = self.pid_to_worker_id[pid]
                task_queue_disp = self.get_task_queue_disp(worker_id)
                if not task_queue_disp:
                    continue
                result_queue = task_queue_disp.result_queue
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


class TaskQueueDispatcher:
    """Incapsulate data structures necessary for dispatching workers working on
    the one task queue.
    """
    def __init__(self, key, task_group, randomize):
        self.key = key
        self.gen_worker = task_group['gen_worker']
        self.task_ids = task_group['task_ids']
        self.randomize = randomize
        if self.randomize:
            random.shuffle(self.task_ids)
        self.result_queue = SimpleQueue()
        self.task_queue = SimpleQueue()
        for task_id in self.task_ids:
            self.task_queue.put(task_id)
        self.worker_ids = set()
        self.done = False
        self.done_task_ids = set()

    def _run_worker(self, worker_id):
        """Entry function for worker processes."""
        color_stdout.queue = self.result_queue
        worker = self.gen_worker(worker_id)
        worker.run_all(self.task_queue, self.result_queue)

    def add_worker(self, worker_id):
        # Note: each of our workers should consume only one None, but for the
        # case of abnormal circumstances we listen for processes termination
        # (method 'check_for_dead_processes') and for time w/o output from
        # workers (class 'HangWatcher').
        self.task_queue.put(None)  # 'stop worker' marker

        entry = functools.partial(self._run_worker, worker_id)

        self.worker_ids.add(worker_id)
        process = multiprocessing.Process(target=entry)
        process.start()
        return process

    def del_worker(self, worker_id):
        # TODO: join on process, remove pid from pids
        self.worker_ids.remove(worker_id)
        # mark task queue as done when the first worker done to prevent cycling
        # with add-del workers
        self.done = True

    def mark_task_done(self, task_id):
        self.done_task_ids.add(task_id)

    def undone_tasks(self):
        # keeps an original order
        res = []
        for task_id in self.task_ids:
            if task_id not in self.done_task_ids:
                res.append(task_id)
        return res
