# Imports
# #######


import os
import sys
import shutil
import atexit
import traceback

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


class Worker:
    def report_keyboard_interrupt(self):
        color_stdout('[Worker "%s"] Caught keyboard interrupt; stopping...\n' \
            % self.name, schema='test_var')

    def __init__(self, suite, _id):
        self.initialized = False
        self.id = _id
        self.suite = suite
        self.name = '%02d_%s' % (self.id, self.suite.suite_path)
        self.suite.ini['vardir'] += '/' + self.name
        try:
            self.server = suite.gen_server()
            self.inspector = suite.start_server(self.server)
            self.initialized = True
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()

    # TODO: timeout for task
    # Note: it's not exception safe
    def run_task(self, task):
        if not self.initialized:
            return
        try:
            res = self.suite.run_test(task, self.server, self.inspector)
        except KeyboardInterrupt:
            self.report_keyboard_interrupt()
            raise
        except Exception as e:
            color_stdout('Worker "%s" received the following error; stopping...\n' \
                % self.name, schema='error')
            color_stdout(traceback.format_exc() + '\n', schema='error')
            raise
            # XXX: there are errors after which we can continue? Or its
            #      processed down by the call stack?
        # TODO: add res to output queue

    def run_loop(self, task_queue):
        """ called from 'run_all' """
        while True:
            task_name = task_queue.get()
            # None is 'stop worker' marker
            if task_name is None:
                color_stdout('Worker "%s" exhaust task queue; stopping the server...\n' \
                    % self.name, schema='test_var')
                self.suite.stop_server(self.server, self.inspector)
                task_queue.task_done()
                break
            # find task by name
            # XXX: should we abstract it somehow? don't access certain field
            for cur_task in self.suite.tests:
                if cur_task.name == task_name:
                    task = cur_task
                    break
            res = self.run_task(task)
            # TODO: add res to output queue
            task_queue.task_done()

    def run_all(self, task_queue):
        if not self.initialized:
            return
        try:
            self.run_loop(task_queue)
        except (KeyboardInterrupt, Exception):
            # some task were in progress when the exception raised
            task_queue.task_done()
            self.flush_all(task_queue)  # unblock task_queue
            self.suite.stop_server(self.server, self.inspector, silent=True)

    def flush_all(self, task_queue):
        # TODO: add 'not run' status to output queue for flushed tests
        while True:
            task_name = task_queue.get()
            task_queue.task_done()
            # None is 'stop worker' marker
            if task_name is None:
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


def task_baskets():
    suites = find_suites()
    res = {}
    for suite in suites:
        key = os.path.basename(suite.suite_path)
        gen_worker = lambda _id, suite=suite: Worker(suite, _id)
        tasks = suite.find_tests()
        if tasks:
            res[key] = {
                'gen_worker': gen_worker,
                'tasks': tasks,
            }
    return res


# Package (de)initialization
############################


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

    return (options, oldcwd)

@atexit.register
def module_del():
    """ Called before exit """
    os.chdir(oldcwd)


# Globals
#########


options, oldcwd = module_init()
