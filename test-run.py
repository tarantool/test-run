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
from lib.tarantool_server import TarantoolStartError

from lib.colorer import Colorer
color_stdout = Colorer()

#
# Run a collection of tests.
#

#######################################################################
# Program body
#######################################################################

def main():
    options = lib.options
    failed_tests = []

    try:
        color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')
        for basket in lib.task_baskets().values():
            tasks = basket['tasks']
            if not tasks:
                continue
            worker_id = 1
            worker = basket['gen_worker'](worker_id)
            for task in tasks:
                worker.run_task(task)

# XXX: collect failed_tests
#        suites = lib.find_suites()
#        if options.args.stress is None:
#            for suite in suites:
#                failed_tests.extend(suite.run_all())
#        else:
#            for suite in suites:
#                suite.run_all()
    except KeyboardInterrupt:
        color_stdout('[Main loop] Caught keyboard interrupt\n', schema='test_var')
        return (-1)
    except TarantoolStartError:
        # fail silently, we already reported it to stdout
        return (-1)
    except RuntimeError as e:
        raise # XXX: remove it
        color_stdout("\nFatal error: %s. Execution aborted.\n" % e, schema='error')
        if options.args.gdb:
            time.sleep(100)
        return (-1)

    if failed_tests and options.args.is_force:
        color_stdout("\n===== %d tests failed:\n" % len(failed_tests), schema='error')
        for test in failed_tests:
             color_stdout("----- %s\n" % test, schema='info')

    return (-1 if failed_tests else 0)

if __name__ == "__main__":
    exit(main())
