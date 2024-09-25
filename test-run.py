#!/usr/bin/env python3
"""Tarantool regression test suite front-end."""

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

# How it works (briefly, simplified)
# ##################################
#
# * Get task groups; each task group correspond to a test suite; each task
#   group contains workers generator (factory) and task IDs (test_name +
#   conf_name).
# * Put task groups to Dispatcher, which:
#   * Create task (input) and result (output) queues for each task group.
#   * Create and run specified count of workers on these queues.
#   * Wait for results on the result queues and calls registered listeners.
#   * If some worker done its work, the Dispatcher will run the new one if
#     there are tasks.
# * Listeners received messages from workers and timeouts when no messages
#   received. Its:
#   * Count results statistics.
#   * Multiplex screen's output.
#   * Log output to per worker log files.
#   * Exit us when some test failed.
#   * Exit us when no output received from workers during some time.
# * When all workers reported it's done (or exceptional situation occured) the
#   main process kill all processes in the same process group as its own to
#   prevent 'orphan' worker or tarantool servers from flooding an OS.
# * Exit status is zero (success) when no errors detected and all requested
#   tests passed. Otherwise non-zero.


import multiprocessing
import os
import sys
import time

from lib import Options
from lib import saved_env
from lib.colorer import color_stdout
from lib.colorer import separator
from lib.colorer import test_line
from lib.utils import cpu_count
from lib.utils import find_tags
from lib.utils import shlex_quote
from lib.error import TestRunInitError
from lib.utils import print_tail_n
from lib.utils import PY3
from lib.worker import get_task_groups
from lib.worker import get_reproduce_file
from lib.worker import reproduce_task_groups
from lib.worker import print_greetings
from dispatcher import Dispatcher
from listeners import HangError

EXIT_SUCCESS = 0
EXIT_HANG = 1
EXIT_INTERRUPTED = 2
EXIT_FAILED_TEST = 3
EXIT_NOTDONE_TEST = 4
EXIT_INIT_ERROR = 5
EXIT_UNKNOWN_ERROR = 50


def main_loop_parallel():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    args = Options().args
    jobs = args.jobs
    if jobs < 1:
        # faster result I got was with 2 * cpu_count
        jobs = 2 * cpu_count()

    if jobs > 0:
        color_stdout("Running in parallel with %d workers\n\n" % jobs,
                     schema='tr_text')
    randomize = True

    color_stdout("Timeout options:\n", schema='tr_text')
    color_stdout('-' * 19, "\n",       schema='separator')
    color_stdout("SERVER_START_TIMEOUT:" . ljust(26) + "{}\n" .
                 format(args.server_start_timeout), schema='tr_text')
    color_stdout("REPLICATION_SYNC_TIMEOUT:" . ljust(26) + "{}\n" .
                 format(args.replication_sync_timeout), schema='tr_text')
    color_stdout("TEST_TIMEOUT:" . ljust(26) + "{}\n" .
                 format(args.test_timeout), schema='tr_text')
    color_stdout("NO_OUTPUT_TIMEOUT:" . ljust(26) + "{}\n" .
                 format(args.no_output_timeout), schema='tr_text')
    color_stdout("\n", schema='tr_text')

    task_groups = get_task_groups()
    if Options().args.reproduce:
        task_groups = reproduce_task_groups(task_groups)
        jobs = 1
        randomize = False

    dispatcher = Dispatcher(task_groups, jobs, randomize)
    dispatcher.start()

    print_greetings()

    color_stdout('\n')
    separator('=')
    color_stdout('WORKR ', schema='t_name')
    test_line('TEST', 'PARAMS')
    color_stdout('RESULT\n', schema='test_pass')
    separator('-')

    try:
        is_force = Options().args.is_force
        dispatcher.wait()
        dispatcher.wait_processes()
        separator('-')
        has_failed, has_flaked = dispatcher.statistics.print_statistics()
        has_undone = dispatcher.report_undone(
            verbose=bool(is_force or not has_failed))
        if any([has_failed, has_flaked]):
            dispatcher.artifacts.save_artifacts()
            if has_failed:
                return EXIT_FAILED_TEST
        if has_undone:
            return EXIT_NOTDONE_TEST
    except KeyboardInterrupt:
        separator('-')
        dispatcher.statistics.print_statistics()
        dispatcher.report_undone(verbose=False)
        raise
    except HangError:
        separator('-')
        dispatcher.statistics.print_statistics()
        dispatcher.report_undone(verbose=False)
        return EXIT_HANG
    return EXIT_SUCCESS


def main_parallel():
    res = EXIT_UNKNOWN_ERROR

    try:
        res = main_loop_parallel()
    except KeyboardInterrupt:
        color_stdout('\n[Main process] Caught keyboard interrupt\n',
                     schema='test_var')
        res = EXIT_INTERRUPTED
    return res


def main_loop_consistent(failed_test_ids):
    # find and prepare all tasks/groups, print information
    task_groups = get_task_groups().items()
    print_greetings()

    for name, task_group in task_groups:
        # print information about current test suite
        color_stdout('\n')
        separator('=')
        test_line('TEST', 'PARAMS')
        color_stdout("RESULT\n", schema='test_pass')
        separator('-')

        task_ids = task_group['task_ids']
        show_reproduce_content = task_group['show_reproduce_content']
        if not task_ids:
            continue
        worker_id = 1
        worker = task_group['gen_worker'](worker_id)
        for task_id in task_ids:
            # The 'run_task' method returns a tuple of two items:
            # (short_status, duration). So taking the first
            # item of this tuple for failure check.
            short_status = worker.run_task(task_id)[0]
            if short_status == 'fail':
                reproduce_file_path = \
                    get_reproduce_file(worker.name)
                color_stdout('Reproduce file %s\n' %
                             reproduce_file_path, schema='error')
                if show_reproduce_content:
                    color_stdout("---\n", schema='separator')
                    print_tail_n(reproduce_file_path)
                    color_stdout("...\n", schema='separator')
                failed_test_ids.append(task_id)
                if not Options().args.is_force:
                    worker.stop_server(cleanup=False)
                    return

        separator('-')

        worker.stop_server(silent=False)
        color_stdout()


def main_consistent():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')
    failed_test_ids = []

    try:
        main_loop_consistent(failed_test_ids)
    except KeyboardInterrupt:
        color_stdout('[Main loop] Caught keyboard interrupt\n',
                     schema='test_var')
    except RuntimeError as e:
        color_stdout("\nFatal error: %s. Execution aborted.\n" % e,
                     schema='error')
        if Options().args.gdb:
            time.sleep(100)
        return -1

    if failed_test_ids and Options().args.is_force:
        color_stdout("\n===== %d tests failed:\n" % len(failed_test_ids),
                     schema='error')
        for test_id in failed_test_ids:
            color_stdout("----- %s\n" % str(test_id), schema='info')

    return (-1 if failed_test_ids else 0)


def show_tags():
    # Collect tests in the same way as when we run them.
    collected_tags = set()
    for name, task_group in get_task_groups().items():
        for task_id in task_group['task_ids']:
            test_name, _ = task_id
            for tag in find_tags(test_name):
                collected_tags.add(tag)

    for tag in sorted(collected_tags):
        color_stdout(tag + '\n')


def show_env():
    """ Print new values of changed environment variables.

        The format is suitable for sourcing in a shell.
    """
    original_env = saved_env()
    for k, v in os.environ.items():
        # Don't change PWD.
        #
        # test-run changes current working directory and set PWD
        # environment variable. If we'll just export PWD (and
        # don't change a current directory), it will be very
        # misleading. Moreover, changing the directory by test-run
        # is more like an implementation detail. It would be good
        # to get rid from this approach in a future.
        if k == 'PWD':
            continue

        # Don't print unchanged environment variables.
        #
        # It would be harmless, but if we filter them out, the
        # output is nicely short.
        if original_env.get(k) == v:
            continue

        color_stdout('export {}={}\n'.format(shlex_quote(k), shlex_quote(v)))

    # test-run doesn't call `del os.environ['FOO']` anywhere, so
    # all changed variables are present in `os.environ`. We don't
    # need an extra traverse over `original_env` as it would be in
    # the general case of comparing two dictionaries.


if __name__ == "__main__":
    # In Python 3 start method 'spawn' in multiprocessing module becomes
    # default on Mac OS.
    #
    # The 'spawn' method causes re-execution of some code, which is already
    # executed in the main process. At least it is seen on the
    # lib/__init__.py code, which removes the 'var' directory. Some other
    # code may have side effects too, it requires investigation.
    #
    # The method also requires object serialization that doesn't work when
    # objects use lambdas, whose for example used in class TestSuite
    # (lib/test_suite.py).
    #
    # The latter problem is easy to fix, but the former looks more
    # fundamental. So we stick to the 'fork' method now.
    if PY3:
        multiprocessing.set_start_method('fork')

    # test-run assumes that text file streams are UTF-8 (as
    # contrary to ASCII) on Python 3. It is necessary to process
    # non ASCII symbols in test files, result files and so on.
    #
    # Default text file stream encoding depends on a system
    # locale with exception for the POSIX locale (C locale): in
    # this case UTF-8 is used (see PEP-0540). Sadly, this
    # behaviour is in effect since Python 3.7.
    #
    # We want to achieve the same behaviour on lower Python
    # versions, at least on 3.6.8, which is provided by CentOS 7
    # and CentOS 8.
    #
    # So we hack the open() builtin.
    #
    # https://stackoverflow.com/a/53347548/1598057
    if PY3 and sys.version_info[0:2] < (3, 7):
        std_open = __builtins__.open

        def open_as_utf8(*args, **kwargs):
            if len(args) >= 2:
                mode = args[1]
            else:
                mode = kwargs.get('mode', '')
            if 'b' not in mode:
                kwargs.setdefault('encoding', 'utf-8')
            return std_open(*args, **kwargs)

        __builtins__.open = open_as_utf8

    status = 0

    if Options().args.show_tags:
        show_tags()
        exit(status)

    if Options().args.show_env:
        show_env()
        exit(status)

    try:
        force_parallel = bool(Options().args.reproduce)
        if not force_parallel and Options().args.jobs == -1:
            status = main_consistent()
        else:
            status = main_parallel()
    except TestRunInitError as e:
        color_stdout(str(e), '\n', schema='error')
        status = EXIT_INIT_ERROR

    exit(status)
