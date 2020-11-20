#!/usr/bin/env python2
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
from lib.colorer import color_stdout
from lib.utils import print_tail_n
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
EXIT_UNKNOWN_ERROR = 50


def main_loop_parallel():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    args = Options().args
    jobs = args.jobs
    if jobs < 1:
        # faster result I got was with 2 * cpu_count
        jobs = 2 * multiprocessing.cpu_count()

    if jobs > 0:
        color_stdout("Running in parallel with %d workers\n\n" % jobs,
                     schema='tr_text')
    randomize = True

    color_stdout("Timeout options:\n", schema='tr_text')
    color_stdout('-' * 19, "\n",       schema='separator')
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

    color_stdout("\n", '=' * 86, "\n", schema='separator')
    color_stdout("WORKR".ljust(6),     schema='t_name')
    color_stdout("TEST".ljust(48),     schema='t_name')
    color_stdout("PARAMS".ljust(16),   schema='test_var')
    color_stdout("RESULT\n",           schema='test_pass')
    color_stdout('-' * 81, "\n",       schema='separator')

    try:
        is_force = Options().args.is_force
        dispatcher.wait()
        dispatcher.wait_processes()
        color_stdout('-' * 81, "\n", schema='separator')
        has_failed = dispatcher.statistics.print_statistics()
        has_undone = dispatcher.report_undone(
            verbose=bool(is_force or not has_failed))
        if has_failed:
            dispatcher.artifacts.save_artifacts()
            return EXIT_FAILED_TEST
        if has_undone:
            return EXIT_NOTDONE_TEST
    except KeyboardInterrupt:
        color_stdout('-' * 81, "\n", schema='separator')
        dispatcher.statistics.print_statistics()
        dispatcher.report_undone(verbose=False)
        raise
    except HangError:
        color_stdout('-' * 81, "\n", schema='separator')
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
        color_stdout("\n", '=' * 80, "\n", schema='separator')
        color_stdout("TEST".ljust(48),     schema='t_name')
        color_stdout("PARAMS".ljust(16),   schema='test_var')
        color_stdout("RESULT\n",           schema='test_pass')
        color_stdout('-' * 75, "\n",       schema='separator')

        task_ids = task_group['task_ids']
        show_reproduce_content = task_group['show_reproduce_content']
        if not task_ids:
            continue
        worker_id = 1
        worker = task_group['gen_worker'](worker_id)
        for task_id in task_ids:
            short_status = worker.run_task(task_id)
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

        color_stdout('-' * 75, "\n", schema='separator')

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


if __name__ == "__main__":
    # don't sure why, but it values 1 or 2 gives 1.5x speedup for parallel
    # test-run (and almost doesn't affect consistent test-run)
    os.environ['OMP_NUM_THREADS'] = '2'

    status = 0

    force_parallel = bool(Options().args.reproduce)
    if not force_parallel and Options().args.jobs == -1:
        status = main_consistent()
    else:
        status = main_parallel()

    exit(status)
