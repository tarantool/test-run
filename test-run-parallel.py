#!/usr/bin/env python2


# TODOs:
# ! * Count tests that are 'not_run' due to worker hang (compare received tasks
#     results w/ sent tasks).
#     * Non-zero exit code in this case (in the case when we have any 'fail' or
#       'not_run').
# ! * Do out-of-source build work?
# ? * Don't restart a server per test, fix admin connections (and maybe update
#     properly other fields) w/o server restarting; check 0b586f55.
#     * Add Server's function like setup_for_test_type().
#   * Found workers failed at the initialization (starting server) -- via
#     result_queue -- then print path to worker's log file and give non-zero
#     exit status.
#   * Make color_stdout and color_log functions in colorer.py. Give
#     comment/docstring about usage of color_log and intention to use it only
#     for regular, non-error output that appears every run and mostly not
#     needed for a user. Don't hide errors and backtraces (or any other details
#     of an exceptional circumstances) from the screen, because such details
#     especially useful with CI bots.
#   * Add '--no-kill-group' option to run test-run in a shell pipeline. With
#     this option test-run will kill only its direct childrens (workers).
#   * Investigate why tarantool can be don't killed by workers, but only by
#     main process by pgrp. Seems that default servers is affected.
#   * Investigate new failing tests.

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
from lib.colorer import Colorer
from lib.utils import signame

from listeners import HangError
from dispatcher import Dispatcher


color_stdout = Colorer()


EXIT_SUCCESS = 0
EXIT_HANG = 1
EXIT_INTERRUPTED = 2
EXIT_UNKNOWN_ERROR = 50


def kill_our_group():
    def pids_in_group(group_id=0):
        """ PIDs of processes the process group except my PID.
            Note: Unix only. """
        pids = []
        cmd = ['pgrep', '-g', str(group_id)]
        p = subprocess.Popen(args=cmd, stdout=subprocess.PIPE)
        for line in p.stdout:
            line = line.strip()
            if line:
                pids.append(int(line))
        pgrep_pid = p.pid
        my_pid = os.getpid()
        p.wait()
        if pgrep_pid in pids:
            pids.remove(pgrep_pid)
        if my_pid in pids:
            pids.remove(my_pid)
        return pids

    def remove_zombies(pids):
        """ Works only for childs; don't for all group's processes """
        if not pids:
            return
        color_stdout('Collecting zombies...\n', schema='test_var')
        for pid in pids:
            try:
                wpid, wstatus = os.waitpid(pid, os.WNOHANG)
                if wpid == pid and (os.WIFEXITED(wstatus) or
                                    os.WIFSIGNALED(wstatus)):
                    pids.remove(pid)
            except OSError:
                pass

    def process_str(pid):
        cmdline = 'unknown'
        try:
            with open('/proc/%d/cmdline' % pid, 'r') as f:
                cmdline = ' '.join(f.read().split('\0')).strip() or cmdline
        except (OSError, IOError):
            pass
        status = 'unknown'
        try:
            with open('/proc/%d/status' % pid, 'r') as f:
                for line in f:
                    key, value = line.split(':', 1)
                    if key == 'State':
                        status = value.strip()
        except (OSError, IOError):
            pass
        return 'process %d [%s; %s]' % (pid, status, cmdline)

    def kill_pids(pids, sig):
        for pid in pids:
            color_stdout('Killing %s by %s\n' % (process_str(pid),
                                                 signame(sig)))
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    for sig in [signal.SIGTERM, signal.SIGKILL]:
        time.sleep(0.1)
        pids = pids_in_group()
        remove_zombies(pids)
        if pids:
            color_stdout(
                '[Main process] Sending %s to processes in our process '
                'group...\n' % signame(sig), schema='test_var')
            kill_pids(pids, sig)


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
        dispatcher.wait()
    except KeyboardInterrupt:
        dispatcher.statistics.print_statistics()
        raise
    except HangError:
        return EXIT_HANG
    dispatcher.statistics.print_statistics()
    dispatcher.wait_processes()
    return EXIT_SUCCESS


def main():
    res = EXIT_UNKNOWN_ERROR
    try:
        res = main_loop()
    except KeyboardInterrupt:
        color_stdout('\n[Main process] Caught keyboard interrupt\n',
                     schema='test_var')
        res = EXIT_INTERRUPTED
    try:
        kill_our_group()
    except KeyboardInterrupt:
        color_stdout(
            '\n[Main process] Caught keyboard interrupt; killing processes '
            'in our process group possibly not done\n', schema='test_var')
        res = EXIT_INTERRUPTED
    return res


if __name__ == "__main__":
    exit(main())
