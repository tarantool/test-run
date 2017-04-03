#!/usr/bin/env python2


# TODOs:
# * Give each worker 1000 unique ports for TCP sockets and let find_port from
#   utils.py works only inside them.
# * Investigate new failing tests.


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


import os
import signal
import sys
import time

import subprocess
import multiprocessing

import lib
from lib.colorer import color_stdout
from lib.utils import signame
from lib.utils import format_process

from listeners import HangError
from dispatcher import Dispatcher


EXIT_SUCCESS = 0
EXIT_HANG = 1
EXIT_INTERRUPTED = 2
EXIT_FAILED_TEST = 3
EXIT_NOTDONE_TEST = 4
EXIT_UNKNOWN_ERROR = 50


def main_loop():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    jobs = lib.Options().args.jobs
    if jobs == 0:
        # faster result I got was with 2 * cpu_count
        jobs = 2 * multiprocessing.cpu_count()
    randomize = True

    task_groups = lib.worker.get_task_groups()
    if lib.Options().args.reproduce:
        task_groups = lib.worker.reproduce_task_groups(task_groups)
        jobs = 1
        randomize = False

    dispatcher = Dispatcher(task_groups, jobs, randomize)
    dispatcher.start()
    try:
        is_force = lib.Options().args.is_force
        dispatcher.wait()
        dispatcher.wait_processes()
        has_failed = dispatcher.statistics.print_statistics()
        has_undone = dispatcher.report_undone(verbose=is_force)
        if has_failed:
            return EXIT_FAILED_TEST
        if is_force and has_undone:
            return EXIT_NOTDONE_TEST
    except KeyboardInterrupt:
        dispatcher.statistics.print_statistics()
        dispatcher.report_undone(verbose=is_force)
        raise
    except HangError:
        dispatcher.statistics.print_statistics()
        dispatcher.report_undone(verbose=is_force)
        return EXIT_HANG
    return EXIT_SUCCESS


def main():
    res = EXIT_UNKNOWN_ERROR
    try:
        res = main_loop()
    except KeyboardInterrupt:
        color_stdout('\n[Main process] Caught keyboard interrupt\n',
                     schema='test_var')
        res = EXIT_INTERRUPTED
    return res


if __name__ == "__main__":
    exit(main())
