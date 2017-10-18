import os
import re
import sys
import time
import filecmp
import difflib
import traceback
import gevent
import pytap13
import pprint
import shutil

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from lib.utils import non_empty_valgrind_logs, print_tail_n

from lib.colorer import color_stdout


class TestExecutionError(OSError):
    """ When we know we fail, but already reported the fail in an another
    greenlet.
    """
    pass


class TestRunGreenlet(gevent.Greenlet):
    def __init__(self, green_callable, *args, **kwargs):
        self.callable = green_callable
        self.callable_args = args
        self.callable_kwargs = kwargs
        super(TestRunGreenlet, self).__init__()

    def _run(self, *args, **kwargs):
        self.callable(*self.callable_args, **self.callable_kwargs)

    def __repr__(self):
            return "<TestRunGreenlet at %s info='%s'>" % (hex(id(self)), getattr(self, "info", None))

class FilteredStream:
    """Helper class to filter .result file output"""
    def __init__(self, filename):
        #
        # always open the output stream in line-buffered mode,
        # to see partial results of a failed test
        #
        self.stream = open(filename, "w+", 1)
        self.filters = []
        self.inspector = None

    def write(self, fragment):
        """Apply all filters, then write result to the undelrying stream.
        Do line-oriented filtering: the fragment doesn't have to represent
        just one line."""
        fragment_stream = StringIO(fragment)
        skipped = False
        for line in fragment_stream:
            original_len = len(line.strip())
            for pattern, replacement in self.filters:
                line = re.sub(pattern, replacement, line)
                # don't write lines that are completely filtered out:
                skipped = original_len and not line.strip()
                if skipped:
                    break
            if not skipped:
                self.stream.write(line)

    def push_filter(self, pattern, replacement):
        self.filters.append([pattern, replacement])

    def pop_filter(self):
        self.filters.pop()

    def clear_all_filters(self):
        self.filters = []

    def close(self):
        self.clear_all_filters()
        self.stream.close()

    def flush(self):
        self.stream.flush()


class Test:
    """An individual test file. A test object can run itself
    and remembers completion state of the run.

    If file <test_name>.skipcond is exists it will be executed before
    test and if it sets self.skip to True value the test will be skipped.
    """
    rg = re.compile('\.test.*')

    def __init__(self, name, args, suite_ini, params={}, conf_name=None):
        """Initialize test properties: path to test file, path to
        temporary result file, path to the client program, test status."""
        self.name = name
        self.args = args
        self.suite_ini = suite_ini
        self.result = os.path.join(suite_ini['suite'],
                os.path.basename(self.rg.sub('.result', name)))
        self.skip_cond = os.path.join(suite_ini['suite'],
                os.path.basename(self.rg.sub('.skipcond', name)))
        self.tmp_result = os.path.join(self.suite_ini['vardir'],
                                       os.path.basename(self.result))
        self.reject = self.rg.sub('.reject', name)
        self.is_executed = False
        self.is_executed_ok = None
        self.is_equal_result = None
        self.is_valgrind_clean = True
        self.is_terminated = False
        self.run_params = params
        self.conf_name = conf_name

        # filled in execute() when a greenlet runs
        self.current_test_greenlet = None

        # prevent double/triple reporting
        self.is_crash_reported = False

    @property
    def id(self):
        return self.name, self.conf_name

    def passed(self):
        """Return true if this test was run successfully."""
        return self.is_executed and self.is_executed_ok and self.is_equal_result

    def execute(self, server):
        # Note: don't forget to set 'server.current_test = self' in
        # inherited classes. Crash reporting relying on that.
        server.current_test = self

    def run(self, server):
        """ Execute the test assuming it's a python program.  If the test
            aborts, print its output to stdout, and raise an exception. Else,
            comprare result and reject files.  If there is a difference, print
            it to stdout.

            Returns short status of the test as a string: 'skip', 'pass',
            'new', or 'fail'. There is also one possible value for
            short_status, 'disabled', but it returned in the caller,
            TestSuite.run_test().
        """

        # Note: test was created before certain worker become known, so we need
        # to update temporary result directory here as it depends on 'vardir'.
        self.tmp_result = os.path.join(self.suite_ini['vardir'],
                                       os.path.basename(self.result))

        diagnostics = "unknown"
        save_stdout = sys.stdout
        try:
            self.skip = False
            if os.path.exists(self.skip_cond):
                sys.stdout = FilteredStream(self.tmp_result)
                stdout_fileno = sys.stdout.stream.fileno()
                execfile(self.skip_cond, dict(locals(), **server.__dict__))
                sys.stdout.close()
                sys.stdout = save_stdout
            if not self.skip:
                sys.stdout = FilteredStream(self.tmp_result)
                stdout_fileno = sys.stdout.stream.fileno()
                self.execute(server)
                sys.stdout.flush()
            self.is_executed_ok = True
        except TestExecutionError:
            self.is_executed_ok = False
        except Exception as e:
            color_stdout('\nTest.run() received the following error:\n' +
                traceback.format_exc() + '\n', schema='error')
            diagnostics = str(e)
            if e.__class__.__name__ == 'TarantoolStartError':
                # worker should stop
                raise
        finally:
            if sys.stdout and sys.stdout != save_stdout:
                sys.stdout.close()
            sys.stdout = save_stdout
        self.is_executed = True
        sys.stdout.flush()

        is_tap = False
        if not self.skip:
            if self.is_executed_ok and os.path.isfile(self.result):
                self.is_equal_result = filecmp.cmp(self.result, self.tmp_result)
            elif self.is_executed_ok:
                is_tap, is_ok = self.check_tap_output()
                self.is_equal_result = is_ok
        else:
            self.is_equal_result = 1

        if self.args.valgrind:
            non_empty_logs = non_empty_valgrind_logs(
                server.current_valgrind_logs(for_test=True))
            self.is_valgrind_clean = not bool(non_empty_logs)

        short_status = None

        if self.skip:
            short_status = 'skip'
            color_stdout("[ skip ]\n", schema='test_skip')
            if os.path.exists(self.tmp_result):
                os.remove(self.tmp_result)
        elif self.is_executed_ok and self.is_equal_result and self.is_valgrind_clean:
            short_status = 'pass'
            color_stdout("[ pass ]\n", schema='test_pass')
            if os.path.exists(self.tmp_result):
                os.remove(self.tmp_result)
        elif (self.is_executed_ok and not self.is_equal_result and not
              os.path.isfile(self.result)) and not is_tap:
            shutil.copy(self.tmp_result, self.result)
            short_status = 'new'
            color_stdout("[ new ]\n", schema='test_new')
        else:
            shutil.copy(self.tmp_result, self.reject)
            short_status = 'fail'
            color_stdout("[ fail ]\n", schema='test_fail')

            where = ""
            if not self.is_crash_reported and not self.is_executed_ok:
                self.print_diagnostics(self.reject, "Test failed! Last 15 lines of the result file:\n")
                server.print_log(15)
                where = ": test execution aborted, reason '{0}'".format(diagnostics)
            elif not self.is_crash_reported and not self.is_equal_result:
                self.print_unidiff()
                server.print_log(15)
                where = ": wrong test output"
            elif not self.is_crash_reported and not self.is_valgrind_clean:
                os.remove(self.reject)
                for log_file in non_empty_logs:
                    self.print_diagnostics(log_file,
                            "Test failed! Last 10 lines of {}:\n".format(
                                log_file))
                where = ": there were warnings in the valgrind log file(s)"
        return short_status

    def print_diagnostics(self, logfile, message):
        """Print 10 lines of client program output leading to test
        failure. Used to diagnose a failure of the client program"""

        color_stdout(message, schema='error')
        print_tail_n(logfile, 10)

    def print_unidiff(self):
        """Print a unified diff between .test and .result files. Used
        to establish the cause of a failure when .test differs
        from .result."""

        color_stdout("\nTest failed! Result content mismatch:\n", schema='error')
        with open(self.result, "r") as result:
            with open(self.reject, "r") as reject:
                result_time = time.ctime(os.stat(self.result).st_mtime)
                reject_time = time.ctime(os.stat(self.reject).st_mtime)
                diff = difflib.unified_diff(result.readlines(),
                                            reject.readlines(),
                                            self.result,
                                            self.reject,
                                            result_time,
                                            reject_time)

                color_stdout.writeout_unidiff(diff)

    def tap_parse_print_yaml(self, yml):
        if 'expected' in yml and 'got' in yml:
            color_stdout('Expected: %s\n' % yml['expected'], schema='error')
            color_stdout('Got:      %s\n' % yml['got'],      schema='error')
            del yml['expected']
            del yml['got']
        if 'trace' in yml:
            color_stdout('Traceback:\n', schema='error')
            for fr in yml['trace']:
                fname = fr.get('name', '')
                if fname:
                    fname = " function '%s'" % fname
                line = '[%-4s]%s at <%s:%d>\n' % (
                        fr['what'], fname, fr['filename'], fr['line']
                )
                color_stdout(line, schema='error')
            del yml['trace']
        if 'filename' in yml:
            del yml['filename']
        if 'line' in yml:
            del yml['line']
        yaml_str = pprint.pformat(yml)
        color_stdout('\n', schema='error')
        if len(yml):
            for line in yaml_str.splitlines():
                color_stdout(line + '\n', schema='error')
            color_stdout('\n', schema='error')

    def check_tap_output(self):
        """ Returns is_tap, is_ok """
        if not os.path.isfile(self.tmp_result):
            color_strout('\nCannot find %s\n' % self.tmp_result, schema='error')
            self.is_crash_reported = True
            return False
        with open(self.tmp_result, 'r') as f:
            content = f.read()
        tap = pytap13.TAP13()
        try:
            tap.parse(content)
        except ValueError as e:
            color_stdout('\nTAP13 parse failed: %s\n' % str(e), schema='error')
            self.is_crash_reported = True
            return False, False
        is_ok = True
        for test_case in tap.tests:
            if test_case.result == 'ok':
                continue
            if is_ok:
                color_stdout('\n')
            color_stdout('%s %s %s # %s %s\n' % (
                test_case.result,
                test_case.id or '',
                test_case.description or '-',
                test_case.directive or '',
                test_case.comment or ''), schema='error')
            if test_case.yaml:
                self.tap_parse_print_yaml(test_case.yaml)
            is_ok = False
        if not is_ok:
            color_stdout('Rejected result file: %s\n' % self.reject, schema='test_var')
            self.is_crash_reported = True
        return True, is_ok
