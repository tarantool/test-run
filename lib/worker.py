import os
import sys
import shutil
import atexit
import signal
import traceback

import lib
from lib.tarantool_server import TarantoolStartError
from lib.test_suite       import TestSuite
from lib.parallel         import Supervisor


from lib.colorer import Colorer
color_stdout = Colorer()


# Utils
#######


def find_suites():
    suite_names = lib.options.args.suites
    if suite_names == []:
        for root, dirs, names in os.walk(os.getcwd(), followlinks=True):
            if "suite.ini" in names:
                suite_names.append(os.path.basename(root))

    if lib.options.args.stress is None:
        suites = [TestSuite(suite_name, lib.options.args) for suite_name in sorted(suite_names)]
    else:
        suite_names = [suite_name for suite_name in suite_names if suite_name.find(lib.options.args.stress) != -1]
        suites = [Supervisor(suite_name, lib.options.args) for suite_name in sorted(suite_names)]
    return suites


# Get tasks and worker generators
#################################


def task_buckets():
    suites = find_suites()
    res = {}
    for suite in suites:
        key = os.path.basename(suite.suite_path)
        gen_worker = lambda _id, suite=suite: Worker(suite, _id)
        task_ids = [task.id for task in suite.find_tests()]
        if task_ids:
            res[key] = {
                'gen_worker': gen_worker,
                'task_ids': task_ids,
            }
    return res


# Worker results
################


class BaseWorkerResult(object):
    def __init__(self, worker_id, worker_name):
        super(BaseWorkerResult, self).__init__()
        self.worker_id = worker_id
        self.worker_name = worker_name


class TaskResult(BaseWorkerResult):
    def __init__(self, worker_id, worker_name, task_id, short_status):
        super(TaskResult, self).__init__(worker_id, worker_name)
        self.short_status = short_status
        self.task_id = task_id


class WorkerOutput(BaseWorkerResult):
    def __init__(self, worker_id, worker_name, output):
        super(WorkerOutput, self).__init__(worker_id, worker_name)
        self.output = output


class WorkerDone(BaseWorkerResult):
    def __init__(self, worker_id, worker_name):
        super(WorkerDone, self).__init__(worker_id, worker_name)


# Worker
########


class VoluntaryStopException(Exception):
    pass


class Worker:
    def report_keyboard_interrupt(self):
        color_stdout('\n[Worker "%s"] Caught keyboard interrupt; stopping...\n' \
            % self.name, schema='test_var')

    def wrap_output(self, output):
        return WorkerOutput(self.id, self.name, output)

    def done_marker(self):
        return WorkerDone(self.id, self.name)

    def wrap_result(self, task_id, short_status):
        return TaskResult(self.id, self.name, task_id, short_status)

    def sigterm_handler(self, signum, frame):
        self.sigterm_received = True

    def __init__(self, suite, _id):
        self.sigterm_received = False
        signal.signal(signal.SIGTERM, lambda x, y, z=self: \
            z.sigterm_handler(x, y))

        self.initialized = False
        self.id = _id
        self.suite = suite
        self.name = '%02d_%s' % (self.id, self.suite.suite_path)

        main_vardir = self.suite.ini['vardir']
        self.suite.ini['vardir'] = os.path.join(main_vardir, self.name)

        reproduce_dir = os.path.join(main_vardir, 'reproduce')
        if not os.path.isdir(reproduce_dir):
            os.makedirs(reproduce_dir)
        self.tests_file = os.path.join(reproduce_dir, '%s.tests.txt' % self.name)

        color_stdout.queue_msg_wrapper = \
            lambda output, w=self: w.wrap_output(output)

        try:
            self.server = suite.gen_server()
            self.inspector = suite.start_server(self.server)
            self.initialized = True
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
        except TarantoolStartError:
            color_stdout('Worker "%s" cannot start tarantool server; ignoring tasks...\n' \
                % self.name, schema='error')

    @staticmethod
    def task_done(task_queue):
        if 'task_done' in task_queue.__dict__.keys():
            task_queue.task_done()

    def find_task(self, task_id):
        for cur_task in self.suite.tests:
            if cur_task.id == task_id:
                return cur_task
        raise ValueError('Cannot find test: %s' % str(task_id))

    # TODO: timeout for task
    # Note: it's not exception safe
    def run_task(self, task_id):
        if not self.initialized:
            return self.done_marker()
        try:
            task = self.find_task(task_id)
            with open(self.tests_file, 'a') as f:
                f.write(repr(task.id) + '\n')
            short_status = self.suite.run_test(task, self.server, self.inspector)
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
            raise
        except Exception as e:
            color_stdout('Worker "%s" received the following error; stopping...\n' \
                % self.name, schema='error')
            color_stdout(traceback.format_exc() + '\n', schema='error')
            raise
        return short_status

    def run_loop(self, task_queue, result_queue):
        """ called from 'run_all' """
        while True:
            task_id = task_queue.get()
            # None is 'stop worker' marker
            if task_id is None:
                color_stdout('Worker "%s" exhaust task queue; stopping the server...\n' \
                    % self.name, schema='test_var')
                self.suite.stop_server(self.server, self.inspector)
                Worker.task_done(task_queue)
                break
            short_status = self.run_task(task_id)
            result_queue.put(self.wrap_result(task_id, short_status))
            if not lib.options.args.is_force and short_status == 'fail':
                color_stdout('Worker "%s" got failed test; stopping the server...\n' \
                    % self.name, schema='test_var')
                raise VoluntaryStopException()
            if self.sigterm_received:
                color_stdout('Worker "%s" got signal to terminate; stopping the server...\n' \
                    % self.name, schema='test_var')
                raise VoluntaryStopException()
            Worker.task_done(task_queue)

    def run_all(self, task_queue, result_queue):
        if not self.initialized:
            result_queue.put(self.done_marker())
            return

        try:
            self.run_loop(task_queue, result_queue)
        except (KeyboardInterrupt, Exception):
            self.stop(task_queue, result_queue)

        result_queue.put(self.done_marker())

    def stop(self, task_queue, result_queue):
        # some task was in progress when the exception raised
        Worker.task_done(task_queue)
        # unblock task_queue is case it's JoinableQueue
        self.flush_all_tasks(task_queue, result_queue)
        self.suite.stop_server(self.server, self.inspector, silent=True)

    def flush_all_tasks(self, task_queue, result_queue):
        while True:
            task_id = task_queue.get()
            result_queue.put(self.wrap_result(task_id, 'not_run'))
            Worker.task_done(task_queue)
            # None is 'stop worker' marker
            if task_id is None:
                break
