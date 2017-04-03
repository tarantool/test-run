#!/usr/bin/env python2
"""Tarantool regression test suite front-end."""

__author__ = "Konstantin Osipov <kostja.osipov@gmail.com>"

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

import sys
import time

import lib
import lib.worker

from lib.colorer import color_stdout

#
# Run a collection of tests.
#

#######################################################################
# Program body
#######################################################################


def main_loop(failed_test_ids):
    for task_group in lib.worker.get_task_groups().values():
        task_ids = task_group['task_ids']
        if not task_ids:
            continue
        worker_id = 1
        worker = task_group['gen_worker'](worker_id)
        for task_id in task_ids:
            short_status = worker.run_task(task_id)
            if short_status == 'fail':
                failed_test_ids.append(task_id)
                if not lib.Options().args.is_force:
                    return


def main():
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')
    failed_test_ids = []

    try:
        main_loop(failed_test_ids)
    except KeyboardInterrupt:
        color_stdout('[Main loop] Caught keyboard interrupt\n', schema='test_var')
    except RuntimeError as e:
        color_stdout("\nFatal error: %s. Execution aborted.\n" % e, schema='error')
        if lib.Options().args.gdb:
            time.sleep(100)
        return (-1)

    if failed_test_ids and lib.Options().args.is_force:
        color_stdout("\n===== %d tests failed:\n" % len(failed_test_ids),
                     schema='error')
        for test_id in failed_test_ids:
             color_stdout("----- %s\n" % str(test_id), schema='info')

    return (-1 if failed_test_ids else 0)

if __name__ == "__main__":
    exit(main())
