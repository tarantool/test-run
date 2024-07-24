import os
import sys
import yaml
import shutil

from lib import Options
from lib.colorer import color_stdout
from lib.colorer import final_report
from lib.colorer import decolor
from lib.sampler import sampler
from lib.worker import WorkerCurrentTask
from lib.worker import WorkerDone
from lib.worker import WorkerFlakedTask
from lib.worker import WorkerOutput
from lib.worker import WorkerTaskResult
from lib.worker import get_reproduce_file
from lib.worker import get_luatest_logfile
from lib.utils import prefix_each_line
from lib.utils import safe_makedirs
from lib.utils import print_tail_n
from lib.utils import print_unidiff


class BaseWatcher(object):
    """Base class for all listeners intended to be called when some message
    arrive to a result queue from some worker.
    """
    def process_result(self, obj):
        raise ValueError('override me')

    def process_timeout(self, delta_seconds):
        """Called after delta_seconds time of inactivity."""
        # optionally override
        pass


class StatisticsWatcher(BaseWatcher):
    def __init__(self, get_logfile, total_tasks_cnt):
        self.stats = dict()
        self.field_size = 60
        self._sampler = sampler
        self.duration_stats = dict()
        self.failed_tasks = []
        self.flaked_tasks = []
        self.get_logfile = get_logfile
        self.long_tasks = set()
        self.total_tasks_cnt = total_tasks_cnt
        self.finished_tasks_cnt = 0

    def process_result(self, obj):
        if isinstance(obj, WorkerTaskResult):
            self.finished_tasks_cnt += 1
            if obj.is_long:
                self.long_tasks.add(obj.task_id)

            if obj.short_status not in self.stats:
                self.stats[obj.short_status] = 0
            self.stats[obj.short_status] += 1

            if obj.short_status == 'fail':
                self.failed_tasks.append((obj.task_id, obj.worker_name, False))

            self.duration_stats[obj.task_id] = obj.duration
            self.print_status_line()

        if isinstance(obj, WorkerFlakedTask):
            self.flaked_tasks.append((obj.task_id, obj.worker_name, False))
            self.print_status_line()

    def get_long_mark(self, task):
        return '(long)' if task in self.long_tasks else ''

    def prettify_task_name(self, task_id):
        return task_id[0] + ((':' + task_id[1]) if task_id[1] else '')

    # RSS.
    def print_rss_summary(self, stats_dir):
        if not self._sampler.is_enabled:
            return

        rss_summary = self._sampler.rss_summary
        top_rss = 10

        # Print to stdout RSS statistics for all failed tasks.
        if self.failed_tasks:
            final_report('Occupied memory in failed tests (RSS, Mb):\n', schema='info')
            for task in self.failed_tasks:
                task_id = task[0]
                if task_id in rss_summary:
                    final_report('* %6.1f %s %s\n' % (float(rss_summary[task_id]) / 1024,
                                 self.prettify_task_name(task_id).ljust(self.field_size),
                                 self.get_long_mark(task_id)),
                                 schema='info')
            final_report('\n')

        # Print to stdout RSS statistics for some number of most it used tasks.
        final_report('Top {} tests by occupied memory (RSS, Mb):\n'.format(
                     top_rss), schema='info')
        results_sorted = sorted(rss_summary.items(), key=lambda x: x[1], reverse=True)
        for task_id, rss in results_sorted[:top_rss]:
            final_report('* %6.1f %s %s\n' % (float(rss) / 1024,
                         self.prettify_task_name(task_id).ljust(self.field_size),
                         self.get_long_mark(task_id)), schema='info')
        final_report('\n')

        # Add two newlines at the end to split this paragraph from
        # dashes below. Otherwise GitHub's Markdown parser
        # interprets the paragraph as a header and renders it as a
        # header on the job summary page (in the Actions tab).
        final_report('(Tests quicker than {} seconds may be missed.)\n\n'.format(
                     self._sampler.sample_interval), schema='info')

        final_report('-' * 81, "\n", schema='separator')

        # Print RSS statistics to '<vardir>/statistics/rss.log' file.
        filepath = os.path.join(stats_dir, 'rss.log')
        fd = open(filepath, 'w')
        for task_id in rss_summary:
            fd.write("{} {}\n".format(self.prettify_task_name(task_id),
                                      rss_summary[task_id]))
        fd.close()

    # Durations.
    def print_duration(self, stats_dir):
        top_durations = 10

        # Print to stdout durations for all failed tasks.
        if self.failed_tasks:
            final_report('Duration of failed tests (seconds):\n',
                         schema='info')
            for task in self.failed_tasks:
                task_id = task[0]
                if task_id in self.duration_stats:
                    final_report('* %6.2f %s %s\n' % (self.duration_stats[task_id],
                                 self.prettify_task_name(task_id).ljust(self.field_size),
                                 self.get_long_mark(task_id)),
                                 schema='info')
            final_report('\n')

        # Print to stdout durations for some number of most long tasks.
        final_report('Top {} longest tests (seconds):\n'.format(top_durations),
                     schema='info')
        results_sorted = sorted(self.duration_stats.items(), key=lambda x: x[1], reverse=True)
        for task_id, duration in results_sorted[:top_durations]:
            final_report('* %6.2f %s %s\n' % (duration,
                         self.prettify_task_name(task_id).ljust(self.field_size),
                         self.get_long_mark(task_id)), schema='info')

        final_report('-' * 81, "\n", schema='separator')

        # Print duration statistics to '<vardir>/statistics/duration.log' file.
        filepath = os.path.join(stats_dir, 'duration.log')
        fd = open(filepath, 'w')
        for task_id in self.duration_stats:
            fd.write("{} {}\n".format(self.prettify_task_name(task_id),
                                      self.duration_stats[task_id]))
        fd.close()

    def print_tasks_info(self, tasks):
        for task_id, worker_name, show_reproduce_content in tasks:
            logfile = self.get_logfile(worker_name)
            task_id_str = yaml.safe_dump(task_id, default_flow_style=True)
            final_report('- %s' % task_id_str, schema='test_var')
            color_stdout('# logfile:        %s\n' % logfile)
            luatest_logfile = get_luatest_logfile(worker_name)
            if luatest_logfile:
                color_stdout('# luatest logfile: %s\n' % luatest_logfile)
            reproduce_file_path = get_reproduce_file(worker_name)
            color_stdout('# reproduce file: %s\n' % reproduce_file_path)
            if show_reproduce_content:
                color_stdout("---\n", schema='separator')
                print_tail_n(reproduce_file_path)
                color_stdout("...\n", schema='separator')

    def print_statistics(self):
        """Print statistics and results of testing."""
        # Prepare standalone subpath '<vardir>/statistics' for statistics files.
        stats_dir = os.path.join(Options().args.vardir, 'statistics')
        safe_makedirs(stats_dir)

        self.print_rss_summary(stats_dir)
        self.print_duration(stats_dir)

        if self.stats:
            final_report('Statistics:\n', schema='test_var')
        for short_status, cnt in self.stats.items():
            if short_status == 'pass' and self.flaked_tasks:
                final_report('* %s: %d (flaky: %d)\n' %
                             (short_status, cnt, len(self.flaked_tasks)),
                             schema='test_var')
            else:
                final_report('* %s: %d\n' % (short_status, cnt),
                             schema='test_var')

        if self.flaked_tasks:
            final_report('Flaked tasks:\n', schema='test_var')
            self.print_tasks_info(self.flaked_tasks)

        if not self.failed_tasks:
            return False, bool(self.flaked_tasks)

        final_report('Failed tasks:\n', schema='test_var')
        self.print_tasks_info(self.failed_tasks)

        return True, bool(self.flaked_tasks)

    def print_status_line(self):
        if not color_stdout.is_term:
            return

        lstats = ['{}: {}'.format(k, v) for k, v in self.stats.items()]
        report = '[{}/{}] [{}]'.format(
                self.finished_tasks_cnt,
                self.total_tasks_cnt,
                ', '.join(sorted(lstats)))
        if self.flaked_tasks:
            report += ' [flaky: {}]'.format(len(self.flaked_tasks))

        if self.stats.get('fail', 0) > 0:
            color_schema = 'bad_status'
        elif self.flaked_tasks:
            color_schema = 'tentative_status'
        else:
            color_schema = 'good_status'

        color_stdout(report, schema=color_schema)
        color_stdout('\b' * len(report))


class ArtifactsWatcher(BaseWatcher):
    """ArtifactsWatcher listener collects list of all workers with failed
    tests. After overall testing finishes it copies workers artifacts
    files from its running 'vardir' sub-directories to the common path
    '<vardir>/artifacts' to be able to collect these artifacts later.
    """
    def __init__(self, get_logfile):
        self.failed_workers = []
        self.get_logfile = get_logfile

    def process_result(self, obj):
        if isinstance(obj, WorkerTaskResult) and \
                obj.short_status == 'fail' and \
                obj.worker_name not in self.failed_workers:
            self.failed_workers.append(obj.worker_name)

        if isinstance(obj, WorkerFlakedTask) and \
                obj.worker_name not in self.failed_workers:
            self.failed_workers.append(obj.worker_name)

    def save_artifacts(self):
        if not self.failed_workers:
            return

        def copytree_ignore(path, filenames):
            ignored_filenames = []
            for filename in filenames:
                filepath = os.path.join(path, filename)
                if not (os.path.isfile(filepath) or os.path.isdir(filepath)):
                    ignored_filenames.append(filename)
            return ignored_filenames

        vardir = Options().args.vardir
        artifacts_dir = os.path.join(vardir, 'artifacts')
        artifacts_log_dir = os.path.join(artifacts_dir, 'log')
        artifacts_reproduce_dir = os.path.join(artifacts_dir, 'reproduce')
        safe_makedirs(artifacts_dir)
        safe_makedirs(artifacts_log_dir)
        safe_makedirs(artifacts_reproduce_dir)

        for worker_name in self.failed_workers:
            logfile = self.get_logfile(worker_name)
            reproduce_file_path = get_reproduce_file(worker_name)
            shutil.copy(logfile,
                        os.path.join(artifacts_log_dir,
                                     os.path.basename(logfile)))
            shutil.copy(reproduce_file_path,
                        os.path.join(artifacts_reproduce_dir,
                                     os.path.basename(reproduce_file_path)))
            shutil.copytree(os.path.join(vardir, worker_name),
                            os.path.join(artifacts_dir, worker_name),
                            ignore=copytree_ignore)
        shutil.copytree(os.path.join(vardir, 'statistics'),
                        os.path.join(artifacts_dir, 'statistics'))


class LogOutputWatcher(BaseWatcher):
    def __init__(self):
        self.fds = dict()
        self.logdir = os.path.join(Options().args.vardir, 'log')
        try:
            os.makedirs(self.logdir)
        except OSError:
            pass

    def get_logfile(self, worker_name):
        filename = '%s.log' % worker_name
        filepath = os.path.join(self.logdir, filename)
        return os.path.realpath(filepath)

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            self.fds[obj.worker_id].close()
            del self.fds[obj.worker_id]

        if not isinstance(obj, WorkerOutput):
            return

        if obj.worker_id not in self.fds.keys():
            filepath = self.get_logfile(obj.worker_name)
            self.fds[obj.worker_id] = open(filepath, 'w')
        fd = self.fds[obj.worker_id]

        # Mark chunks without newlines at the end.
        #
        # In OutputWatcher() such chunks are bufferized and
        # written to the terminal when a newline arrives (to
        # don't mix partial lines from different workers).
        #
        # Here we want to dump output without any buffering,
        # because it may help with debugging a problem. So
        # we just mark such chunks and write them out.
        output = obj.output
        if not decolor(output).endswith('\n'):
            output = output + ' <no newline>\n'

        # Prefix each line with a timestamp.
        prefix = '[{}] '.format(obj.timestamp)
        output = prefix_each_line(prefix, output)

        fd.write(output)
        fd.flush()

    def __del__(self):
        for fd in self.fds.values():
            try:
                fd.close()
            except IOError:
                pass


class OutputWatcher(BaseWatcher):
    def __init__(self):
        self.buffer = dict()

    @staticmethod
    def add_prefix(output, worker_id):
        prefix_max_len = len('[xxx] ')
        prefix = ('[%03d] ' % worker_id).ljust(prefix_max_len)
        return prefix_each_line(prefix, output)

    @staticmethod
    def _write(output, worker_id):
        output = OutputWatcher.add_prefix(output, worker_id)
        sys.stdout.write(output)

    def process_result(self, obj):
        if isinstance(obj, WorkerDone):
            bufferized = self.buffer.get(obj.worker_id, '')
            if bufferized:
                OutputWatcher._write(bufferized, obj.worker_id)
            if obj.worker_id in self.buffer.keys():
                del self.buffer[obj.worker_id]
            return

        # Skip irrelevant events.
        if not isinstance(obj, WorkerOutput):
            return

        # Skip color_log() events if --debug is not passed.
        if obj.log_only and not Options().args.debug:
            return

        # Prepend color_log() messages with a timestamp.
        if obj.log_only:
            prefix = '[{}] '.format(obj.timestamp)
            obj.output = prefix_each_line(prefix, obj.output)

        bufferized = self.buffer.get(obj.worker_id, '')
        if decolor(obj.output).endswith('\n'):
            OutputWatcher._write(bufferized + obj.output, obj.worker_id)
            self.buffer[obj.worker_id] = ''
        else:
            self.buffer[obj.worker_id] = bufferized + obj.output

    def not_done_worker_ids(self):
        return self.buffer.keys()


class FailWatcher(BaseWatcher):
    def __init__(self, terminate_all_workers):
        self.terminate_all_workers = terminate_all_workers
        self.got_fail = False

    def process_result(self, obj):
        if not isinstance(obj, WorkerTaskResult):
            return

        if obj.short_status == 'fail':
            color_stdout('[Main process] Got failed test; '
                         'gently terminate all workers...\n',
                         schema='test_var')
            self.got_fail = True
            self.terminate_all_workers()


class HangError(Exception):
    pass


class HangWatcher(BaseWatcher):
    """Terminate all workers if no output received 'no_output_times' time."""
    def __init__(self, get_not_done_worker_ids, kill_all_workers,
                 warn_timeout, kill_timeout):
        self.get_not_done_worker_ids = get_not_done_worker_ids
        self.kill_all_workers = kill_all_workers
        self.warn_timeout = warn_timeout
        self.kill_timeout = kill_timeout
        self.warned_seconds_ago = 0.0
        self.inactivity = 0.0
        self.worker_current_task = dict()

    def process_result(self, obj):
        # Track tasks in progress.
        if isinstance(obj, WorkerCurrentTask):
            self.worker_current_task[obj.worker_id] = obj

        # Skip irrelevant events.
        if not isinstance(obj, WorkerOutput):
            return

        # Skip color_log() events if --debug is not passed.
        if obj.log_only and not Options().args.debug:
            return

        self.warned_seconds_ago = 0.0
        self.inactivity = 0.0

    def process_timeout(self, delta_seconds):
        self.warned_seconds_ago += delta_seconds
        self.inactivity += delta_seconds
        worker_ids = self.get_not_done_worker_ids()

        if self.warned_seconds_ago < self.warn_timeout:
            return

        is_warning = self.inactivity < self.kill_timeout
        color_schema = 'test_var' if is_warning else 'error'

        color_stdout(
            "No output during {0.inactivity:.0f} seconds. "
            "Will abort after {0.kill_timeout:.0f} seconds without output. "
            "List of workers not reporting the status:\n".format(self),
            schema=color_schema)

        hung_tasks = [task for worker_id, task
                      in self.worker_current_task.items()
                      if worker_id in worker_ids]
        for task in hung_tasks:
            result_file = task.task_tmp_result
            result_file_summary = '(no result file {})'.format(result_file)
            if os.path.exists(result_file):
                with open(result_file, 'r', encoding='utf-8', errors='replace') as f:
                    lines = sum(1 for _ in f)
                    result_file_summary = 'at {}:{}'.format(result_file,
                                                            lines)
            color_stdout('- {} [{}, {}] {}\n'.format(
                task.worker_name, task.task_name, task.task_param,
                result_file_summary), schema=color_schema)

        self.warned_seconds_ago = 0.0

        if is_warning:
            return

        for task in hung_tasks:
            color_stdout("Test hung! Result content mismatch:\n",
                         schema='error')
            print_unidiff(task.task_result, task.task_tmp_result)
        color_stdout('\n[Main process] No output from workers. '
                     'It seems that we hang. Send SIGKILL to workers; '
                     'exiting...\n', schema='error')
        self.kill_all_workers()

        raise HangError()
