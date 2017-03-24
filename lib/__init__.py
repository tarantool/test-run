# Imports
# #######


import os
import sys
import shutil
import atexit
import traceback
from ast import literal_eval

from options              import Options
from lib.tarantool_server import TarantoolServer
from lib.unittest_server  import UnittestServer
from lib.test_suite       import TestSuite
from lib.parallel         import Supervisor

from lib.colorer import Colorer
color_stdout = Colorer()

# Public interface
##################


__all__ = ['options'] # TODO; needed?


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


class Worker:
    def report_keyboard_interrupt(self):
        color_stdout('[Worker "%s"] Caught keyboard interrupt; stopping...\n' \
            % self.name, schema='test_var')

    def wrap_output(self, output):
        return WorkerOutput(self.id, self.name, output)

    def done_marker(self):
        return WorkerDone(self.id, self.name)

    def wrap_result(self, task_id, short_status):
        return TaskResult(self.id, self.name, task_id, short_status)

    def __init__(self, suite, _id):
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
            Worker.task_done(task_queue)
            result_queue.put(self.wrap_result(task_id, short_status))

    def run_all(self, task_queue, result_queue):
        if not self.initialized:
            result_queue.put(self.done_marker())
            return
        try:
            self.run_loop(task_queue, result_queue)
        except (KeyboardInterrupt, Exception):
            # some task was in progress when the exception raised
            Worker.task_done(task_queue)
            # unblock task_queue is case it's JoinableQueue
            self.flush_all(task_queue, result_queue)
            self.suite.stop_server(self.server, self.inspector, silent=True)

        result_queue.put(self.done_marker())

    def flush_all(self, task_queue, result_queue):
        while True:
            task_id = task_queue.get()
            result_queue.put(self.wrap_result(task_id, 'not_run'))
            Worker.task_done(task_queue)
            # None is 'stop worker' marker
            if task_id is None:
                break


def find_suites():
    suite_names = options.args.suites
    if suite_names == []:
        for root, dirs, names in os.walk(os.getcwd(), followlinks=True):
            if "suite.ini" in names:
                suite_names.append(os.path.basename(root))

    if options.args.stress is None:
        suites = [TestSuite(suite_name, options.args) for suite_name in sorted(suite_names)]
    else:
        suite_names = [suite_name for suite_name in suite_names if suite_name.find(options.args.stress) != -1]
        suites = [Supervisor(suite_name, options.args) for suite_name in sorted(suite_names)]
    return suites


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


# Package (de)initialization
############################


def warn_unix_sockets():
    unix_socket_len_limit = 107
    max_unix_socket_rel = './var/??_replication/autobootstrap_guest3.control'
    max_unix_socket_abs = os.path.realpath(max_unix_socket_rel)
    if len(max_unix_socket_abs) > unix_socket_len_limit:
        color_stdout('WARGING: unix sockets can become longer than 107 symbols:\n',
                     schema='error')
        color_stdout('WARNING: for example: "%s" has length %d\n' % \
            (max_unix_socket_abs, len(max_unix_socket_abs)), schema='error')


def parse_tests_file(tests_file):
    reproduce = []
    try:
        if tests_file:
            with open(tests_file, 'r') as f:
                for line in f:
                    task_id = literal_eval(line)
                    reproduce.append(task_id)
    except IOError:
        color_stdout('Cannot read "%s" passed as --reproduce argument\n' %
            tests_file, schema='error')
        exit(1)
    return reproduce


def setenv():
    """Find where is tarantool dir by check_file"""
    check_file = 'src/fiber.h'
    path = os.path.abspath('../')
    while path != '/':
        if os.path.isfile('%s/%s' % (path, check_file)):
            os.putenv('TARANTOOL_SRC_DIR', path)
            break
        path = os.path.abspath(os.path.join(path, '../'))


def module_init():
    """ Called at import """
    options = Options()
    oldcwd = os.getcwd()
    # Change the current working directory to where all test
    # collections are supposed to reside
    # If script executed with (python test-run.py) dirname is ''
    # so we need to make it .
    path = os.path.dirname(sys.argv[0])
    if not path:
        path = '.'
    os.chdir(path)
    setenv()

    warn_unix_sockets()
    reproduce = parse_tests_file(options.args.reproduce)

    # always run with clean (non-existent) 'var' directory
    try:
        shutil.rmtree(options.args.vardir)
    except OSError:
        pass

    options.args.builddir = os.path.abspath(os.path.expanduser(options.args.builddir))
    os.environ["SOURCEDIR"] = os.path.dirname(os.path.abspath(path))
    os.environ["BUILDDIR"] = os.path.abspath(options.args.builddir)

    TarantoolServer.find_exe(options.args.builddir) # XXX: can raise
    UnittestServer.find_exe(options.args.builddir)

    return (options, oldcwd, reproduce)

@atexit.register
def module_del():
    """ Called before the module exit """
    if 'oldcwd' in globals().keys() and oldcwd:
        os.chdir(oldcwd)


# Globals
#########


options, oldcwd, reproduce = module_init()
