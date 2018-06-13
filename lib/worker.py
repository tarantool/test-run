import os
import signal
import traceback
import yaml
import copy
import functools
import collections

import lib
from lib.utils import safe_makedirs
from lib.test_suite import TestSuite

from lib.colorer import color_stdout, color_log
from lib.tarantool_server import TarantoolServer


# Utils
#######


def find_suites():
    suite_names = lib.Options().args.suites
    if suite_names == []:
        for root, dirs, names in os.walk(os.getcwd(), followlinks=True):
            if "suite.ini" in names:
                suite_names.append(os.path.basename(root))

    suites = [TestSuite(suite_name, lib.Options().args)
              for suite_name in sorted(suite_names)]
    return suites


def parse_reproduce_file(filepath):
    reproduce = []
    if not filepath:
        return reproduce
    try:
        with open(filepath, 'r') as f:
            for task_id in yaml.load(f):
                task_name, task_conf = task_id
                reproduce.append((task_name, task_conf))
    except IOError:
        color_stdout('Cannot read "%s" passed as --reproduce argument\n' %
                     filepath, schema='error')
        exit(1)
    return reproduce


def get_reproduce_file(worker_name):
    main_vardir = os.path.realpath(lib.Options().args.vardir)
    reproduce_dir = os.path.join(main_vardir, 'reproduce')
    return os.path.join(reproduce_dir, '%s.list.yaml' % worker_name)


def print_greetings():
    # print information about tarantool
    color_stdout('\n')
    TarantoolServer.print_exe()


# Get tasks and worker generators
#################################


def get_task_groups():
    """Scan directories where tests files expected to reside, create the list
    of tests and group it by suites. Create workers generator for each of these
    group.
    """
    suites = find_suites()
    res = collections.OrderedDict()
    for suite in suites:
        key = os.path.basename(suite.suite_path)
        gen_worker = functools.partial(Worker, suite)  # get _id as an arg
        task_ids = [task.id for task in suite.find_tests()]
        if task_ids:
            res[key] = {
                'gen_worker': gen_worker,
                'task_ids': task_ids,
                'is_parallel': suite.is_parallel(),
            }
    return res


def reproduce_task_groups(task_groups):
    """Filter provided task_groups down to the one certain group. Sort tests in
    this group as in the reproduce file.
    """
    found_keys = []
    reproduce = parse_reproduce_file(lib.Options().args.reproduce)
    if not reproduce:
        raise ValueError('[reproduce] Tests list cannot be empty')
    for i, task_id in enumerate(reproduce):
        for key, task_group in task_groups.items():
            if task_id in task_group['task_ids']:
                found_keys.append(key)
                break
        if len(found_keys) != i + 1:
            raise ValueError('[reproduce] Cannot find test "%s"' %
                             str(task_id))
    found_keys = list(set(found_keys))
    if len(found_keys) < 1:
        raise ValueError('[reproduce] Cannot find any suite for given tests')
    elif len(found_keys) > 1:
        raise ValueError(
            '[reproduce] Given tests contained by different suites')

    res_key = found_keys[0]
    res_task_group = copy.deepcopy(task_groups[key])
    res_task_group['task_ids'] = reproduce
    return {res_key: res_task_group}


# Worker results
################


class BaseWorkerMessage(object):
    """Base class for all objects passed via result queues. It holds worker_id
    (int) and worker_name (string). Used as a structure, i.e. w/o data fields
    incapsulation.
    """

    def __init__(self, worker_id, worker_name):
        super(BaseWorkerMessage, self).__init__()
        self.worker_id = worker_id
        self.worker_name = worker_name


class WorkerTaskResult(BaseWorkerMessage):
    """ Passed into the result queue when a task processed (done) by the
    worker. The short_status (string) field intended to give short note whether
    the task processed successfully or not, but with little more flexibility
    than binary True/False. The task_id (any hashable object) field hold ID of
    the processed task.
    """

    def __init__(self, worker_id, worker_name, task_id, short_status):
        super(WorkerTaskResult, self).__init__(worker_id, worker_name)
        self.short_status = short_status
        self.task_id = task_id


class WorkerOutput(BaseWorkerMessage):
    """The output passed by worker processes via color_stdout/color_log
    functions. The output wrapped into objects of this class by setting queue
    and wrapper in the Colorer class (see lib/colorer.py). Check
    LogOutputWatcher and OutputWatcher classes in listeners.py file to see how
    the output multiplexed by the main process.
    """

    def __init__(self, worker_id, worker_name, output, log_only):
        super(WorkerOutput, self).__init__(worker_id, worker_name)
        self.output = output
        self.log_only = log_only


class WorkerDone(BaseWorkerMessage):
    """Report the worker as done its work."""

    def __init__(self, worker_id, worker_name):
        super(WorkerDone, self).__init__(worker_id, worker_name)


# Worker
########


class VoluntaryStopException(Exception):
    pass


class Worker:
    def report_keyboard_interrupt(self):
        color_stdout('\n[Worker "%s"] Caught keyboard interrupt; stopping...\n'
                     % self.name, schema='test_var')

    def wrap_output(self, output, log_only):
        return WorkerOutput(self.id, self.name, output, log_only)

    def done_marker(self):
        return WorkerDone(self.id, self.name)

    def wrap_result(self, task_id, short_status):
        return WorkerTaskResult(self.id, self.name, task_id, short_status)

    def sigterm_handler(self, signum, frame):
        self.sigterm_received = True

    def __init__(self, suite, _id):
        self.sigterm_received = False
        signal.signal(signal.SIGTERM, lambda x, y, z=self:
        z.sigterm_handler(x, y))

        self.initialized = False
        self.server = None
        self.inspector = None

        self.id = _id
        self.suite = suite
        self.name = '%03d_%s' % (self.id, self.suite.suite_path)

        main_vardir = self.suite.ini['vardir']
        self.suite.ini['vardir'] = os.path.join(main_vardir, self.name)

        self.reproduce_file = get_reproduce_file(self.name)
        safe_makedirs(os.path.dirname(self.reproduce_file))

        color_stdout.queue_msg_wrapper = self.wrap_output

        self.last_task_done = True
        self.last_task_id = -1

        try:
            self.server = suite.gen_server()
            self.inspector = suite.start_server(self.server)
            self.initialized = True
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
            self.stop_server(cleanup=False)
        except Exception as e:
            color_stdout('Worker "%s" cannot start tarantool server; '
                         'the tasks will be ignored...\n' % self.name,
                         schema='error')
            color_stdout("The raised exception is '%s' of type '%s'.\n"
                         % (str(e), str(type(e))), schema='error')
            color_stdout('Worker "%s" received the following error:\n'
                         % self.name + traceback.format_exc() + '\n',
                         schema='error')
            self.stop_server(cleanup=False)

    def stop_server(self, rais=True, cleanup=True, silent=True):
        try:
            self.suite.stop_server(self.server, self.inspector, silent=silent,
                                   cleanup=cleanup)
        except (KeyboardInterrupt, Exception):
            if rais:
                raise

    # XXX: What if KeyboardInterrupt raised inside task_queue.get() and 'stop
    #      worker' marker readed from the queue, but not returned to us?
    def task_get(self, task_queue):
        self.last_task_done = False
        self.last_task_id = task_queue.get()
        return self.last_task_id

    @staticmethod
    def is_joinable(task_queue):
        return 'task_done' in task_queue.__dict__.keys()

    def task_done(self, task_queue):
        if Worker.is_joinable(task_queue):
            task_queue.task_done()
        self.last_task_done = True

    def find_task(self, task_id):
        for cur_task in self.suite.tests:
            if cur_task.id == task_id:
                return cur_task
        raise ValueError('Cannot find test: %s' % str(task_id))

    # Note: it's not exception safe
    def run_task(self, task_id):
        if not self.initialized:
            return self.done_marker()
        try:
            task = self.find_task(task_id)
            with open(self.reproduce_file, 'a') as f:
                f.write('- ' + yaml.safe_dump(task.id))
            short_status = self.suite.run_test(
                task, self.server, self.inspector)
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
            raise
        except Exception as e:
            color_stdout(
                'Worker "%s" received the following error; stopping...\n'
                % self.name + traceback.format_exc() + '\n', schema='error')
            raise
        return short_status

    def run_loop(self, task_queue, result_queue):
        """ called from 'run_all' """
        while True:
            task_id = self.task_get(task_queue)
            # None is 'stop worker' marker
            if task_id is None:
                color_log('Worker "%s" exhausted task queue; '
                          'stopping the server...\n' % self.name,
                          schema='test_var')
                self.stop_worker(task_queue, result_queue)
                break
            short_status = self.run_task(task_id)
            result_queue.put(self.wrap_result(task_id, short_status))
            if not lib.Options().args.is_force and short_status == 'fail':
                color_stdout(
                    'Worker "%s" got failed test; stopping the server...\n'
                    % self.name, schema='test_var')
                raise VoluntaryStopException()
            if self.sigterm_received:
                color_stdout('Worker "%s" got signal to terminate; '
                             'stopping the server...\n' % self.name,
                             schema='test_var')
                raise VoluntaryStopException()
            self.task_done(task_queue)

    def run_all(self, task_queue, result_queue):
        if not self.initialized:
            self.flush_all_tasks(task_queue, result_queue)
            result_queue.put(self.done_marker())
            return

        try:
            self.run_loop(task_queue, result_queue)
        except (KeyboardInterrupt, Exception):
            self.stop_worker(task_queue, result_queue, cleanup=False)

        result_queue.put(self.done_marker())

    def stop_worker(self, task_queue, result_queue, cleanup=True):
        try:
            if not self.last_task_done:
                self.task_done(task_queue)
            self.flush_all_tasks(task_queue, result_queue)
            self.stop_server(cleanup=cleanup)
        except (KeyboardInterrupt, Exception):
            pass

    def flush_all_tasks(self, task_queue, result_queue):
        """ A queue flusing is necessary only for joinable queue (when runner
            controlling workers with using join() on task queues), so doesn't
            used in the current test-run implementation.
        """
        if not Worker.is_joinable(task_queue):
            return

        # None is 'stop worker' marker
        while self.last_task_id is not None:
            task_id = self.task_get(task_queue)
            result_queue.put(self.wrap_result(task_id, 'not_run'))
            self.task_done(task_queue)
