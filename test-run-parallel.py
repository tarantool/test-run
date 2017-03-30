#!/usr/bin/env python2


# TODOs:
# * Limit workers count by tests count at max.
# * Log file for inspector (useful for debugging).
#   * Just use color_log in it?
# * Document how workers-task-buckets interacts and works; and possible
#   non-obvious code parts.
#   * Comment each Worker's results_queue classes.
#   * Describe how we wait workers, when exits, how select results/output from
#     workers, how and what doing listeners.
# * Can we remove globals in lib/__init__.py?
#   * Options as a singleton.
#   * Don't need chdir before exit?
# * Raise in tarantool_connection.py in addition to unix sockets warning in
#   __init__.py?
# * Do out-of-source build work?
# * Count tests that are 'not_run' due to worker hang (compare received tasks
#   results w/ sent tasks).
#   * Non-zero exit code in this case (in the case when we have any 'fail' or
#     'not_run').

import os
import signal
import sys
import time
import copy

import subprocess
import multiprocessing

import lib
from lib.colorer import Colorer
from lib.utils import signame

from listeners import HangError
from workers_manager import WorkersManager


color_stdout = Colorer()


def reproduce_buckets(reproduce, all_buckets):
    # check test list and find a bucket
    found_bucket_ids = []
    if not lib.reproduce:
        raise ValueError('[reproduce] Tests list cannot be empty')
    for i, task_id in enumerate(lib.reproduce):
        for bucket_id, bucket in all_buckets.items():
            if task_id in bucket['task_ids']:
                found_bucket_ids.append(bucket_id)
                break
        if len(found_bucket_ids) != i + 1:
            raise ValueError('[reproduce] Cannot find test "%s"' %
                             str(task_id))
    found_bucket_ids = list(set(found_bucket_ids))
    if len(found_bucket_ids) < 1:
        raise ValueError('[reproduce] Cannot find any suite for given tests')
    elif len(found_bucket_ids) > 1:
        raise ValueError(
            '[reproduce] Given tests contained by different suites')

    key = found_bucket_ids[0]
    bucket = copy.deepcopy(all_buckets[key])
    bucket['task_ids'] = lib.reproduce
    return {key: bucket}


def main_loop():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')

    jobs = lib.options.args.jobs
    if jobs == 0:
        # faster result I got was with 2 * cpu_count
        jobs = 2 * multiprocessing.cpu_count()
    randomize = True

    buckets = lib.worker.task_buckets()
    if lib.reproduce:
        buckets = reproduce_buckets(lib.reproduce, buckets)
        jobs = 1
        randomize = False

    workers_manager = WorkersManager(buckets, jobs, randomize)
    workers_manager.start()
    try:
        workers_manager.wait()
    except KeyboardInterrupt:
        workers_manager.statistics.print_statistics()
        raise
    except HangError:
        pass
    workers_manager.statistics.print_statistics()
    workers_manager.wait_processes()


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


def main():
    try:
        main_loop()
    except KeyboardInterrupt as e:
        color_stdout('\n[Main process] Caught keyboard interrupt\n',
                     schema='test_var')
    kill_our_group()


if __name__ == "__main__":
    exit(main())
