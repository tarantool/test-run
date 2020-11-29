import os
import re
import sys
from lib.singleton import Singleton


# Use it to print messages on the screen and to the worker's log.
color_stdout = None  # = Colorer(); below the class definition


def color_log(*args, **kwargs):
    """ Print the message only to log file, not on the screen. The intention is
    use this function only for regular, non-error output that appears every run
    and mostly not needed for a user (but useful when investigating occured
    problem). Don't hide errors and backtraces (or any other details of an
    exceptional circumstances) from the screen, because such details especially
    useful with CI bots.
    """
    kwargs['log_only'] = True
    color_stdout(*args, **kwargs)


def qa_notice(*args, **kwargs):
    """ Print a notice for an QA engineer at the terminal.

        Example::

            * [QA Notice]
            *
            * Attempt to stop already stopped server 'foo'
            *
    """
    # Import from the function to avoid recursive import.
    from lib.utils import prefix_each_line

    # Use 'info' color by default (yellow).
    if 'schema' not in kwargs:
        kwargs = dict(kwargs, schema='info')

    # Join all positional arguments (like color_stdout() do) and
    # decorate with a header and asterisks.
    data = ''.join([str(msg) for msg in args])
    data = prefix_each_line('* ', data)
    data = '\n* [QA Notice]\n*\n{}*\n'.format(data)

    # Write out.
    color_stdout(data, **kwargs)


class CSchema(object):
    objects = {}

    def __init__(self):
        self.main_objects = {
            'diff_mark': {},
            'diff_in':   {},
            'diff_out':  {},
            'test_pass': {},
            'test_fail': {},
            'test_new':  {},
            'test_skip': {},
            'test_disa': {},
            'error':     {},
            'lerror':    {},
            'tail':      {},
            'ts_text':   {},
            'path':      {},
            'info':      {},
            'separator': {},
            't_name':    {},
            'serv_text': {},
            'version':   {},
            'tr_text':   {},
            'log':       {},
        }
        self.main_objects.update(self.objects)


class SchemaAscetic(CSchema):
    objects = {
        'diff_mark': {'fgcolor': 'magenta'},
        'diff_in':   {'fgcolor': 'green'},
        'diff_out':  {'fgcolor': 'red'},
        'test_pass': {'fgcolor': 'green'},
        'test_fail': {'fgcolor': 'red'},
        'test_new':  {'fgcolor': 'lblue'},
        'test_skip': {'fgcolor': 'grey'},
        'test_disa': {'fgcolor': 'grey'},
        'error':     {'fgcolor': 'red'},
        'info':      {'fgcolor': 'yellow'},
        'test_var':  {'fgcolor': 'yellow'},
        'test-run command':  {'fgcolor': 'green'},
        'tarantool command': {'fgcolor': 'blue'},
    }


class SchemaPretty(CSchema):
    objects = {
        'diff_mark': {'fgcolor': 'magenta'},
        'diff_in':   {'fgcolor': 'blue'},
        'diff_out':  {'fgcolor': 'red'},
        'test_pass': {'fgcolor': 'green'},
        'test_fail': {'fgcolor': 'red'},
        'test_new':  {'fgcolor': 'lblue'},
        'test_skip': {'fgcolor': 'grey'},
        'test_disa': {'fgcolor': 'grey'},
        'error':     {'fgcolor': 'red'},
        'lerror':    {'fgcolor': 'lred'},
        'tail':      {'fgcolor': 'lblue'},
        'ts_text':   {'fgcolor': 'lmagenta'},
        'path':      {'fgcolor': 'green',  'bold': True},
        'info':      {'fgcolor': 'yellow', 'bold': True},
        'separator': {'fgcolor': 'blue'},
        't_name':    {'fgcolor': 'lblue'},
        'serv_text': {'fgcolor': 'lmagenta'},
        'version':   {'fgcolor': 'yellow', 'bold': True},
        'tr_text':   {'fgcolor': 'green'},
        'log':       {'fgcolor': 'grey'},
        'test_var':  {'fgcolor': 'yellow'},
        'test-run command':  {'fgcolor': 'green'},
        'tarantool command': {'fgcolor': 'blue'},
    }


class Colorer(object):
    """
    Colorer/Styler based on VT220+ specifications (Not full). Based on:
    1. ftp://ftp.cs.utk.edu/pub/shuford/terminal/dec_vt220_codes.txt
    2. http://invisible-island.net/xterm/ctlseqs/ctlseqs.html
    """
    __metaclass__ = Singleton
    fgcolor = {
        "black":     '0;30',
        "red":       '0;31',
        "green":     '0;32',
        "brown":     '0;33',
        "blue":      '0;34',
        "magenta":   '0;35',
        "cyan":      '0;36',
        "grey":      '0;37',
        "lgrey":     '1;30',
        "lred":      '1;31',
        "lgreen":    '1;32',
        "yellow":    '1;33',
        "lblue":     '1;34',
        "lmagenta":  '1;35',
        "lcyan":     '1;36',
        "white":     '1;37',
    }
    bgcolor = {
        "black":     '0;40',
        "red":       '0;41',
        "green":     '0;42',
        "brown":     '0;43',
        "blue":      '0;44',
        "magenta":   '0;45',
        "cyan":      '0;46',
        "grey":      '0;47',
        "lgrey":     '1;40',
        "lred":      '1;41',
        "lgreen":    '1;42',
        "yellow":    '1;43',
        "lblue":     '1;44',
        "lmagenta":  '1;45',
        "lcyan":     '1;46',
        "white":     '1;47',
    }
    attributes = {
        "bold":       '1',
        "underline":  '4',
        "blinking":   '5',
        "negative":   '7',
        "invisible":  '8',
    }
    begin = "\033["
    end = "m"
    disable = begin+'0'+end
    color_re = re.compile('\033' + r'\[\d(?:;\d\d)?m')

    def __init__(self):
        # These two fields can be filled later. It's for passing output from
        # workers via result queue. When worker initializes, it set these
        # fields and just use Colorer as before having multiplexed output.
        self.queue_msg_wrapper = None
        self.queue = None

        self.stdout = sys.stdout
        self.is_term = self.stdout.isatty()
        self.colors = None
        if self.is_term:
            try:
                p = os.popen('tput colors 2>/dev/null')
                self.colors = int(p.read())
            except:  # noqa: E722
                pass
            finally:
                p.close()
        schema = os.getenv('TT_SCHEMA', 'ascetic')
        if schema == 'ascetic':
            self.schema = SchemaAscetic()
        elif schema == 'pretty':
            self.schema = SchemaPretty()
        else:
            self.schema = CSchema()
        self.schema = self.schema.main_objects

    def set_stdout(self):
        sys.stdout = self

    def ret_stdout(self):
        sys.stdout = self.stdout

    def _write(self, obj, log_only):
        if self.queue:
            if self.queue_msg_wrapper:
                obj = self.queue_msg_wrapper(obj, log_only)
            self.queue.put(obj)
        elif not log_only:
            self.stdout.write(obj)

    def _flush(self):
        if not self.queue:
            self.stdout.flush()

    def write(self, *args, **kwargs):
        flags = []
        if 'schema' in kwargs:
            kwargs.update(self.schema[kwargs['schema']])
        for i in self.attributes:
            if i in kwargs and kwargs[i] is True:
                flags.append(self.attributes[i])
        flags.append(self.fgcolor[kwargs['fgcolor']]) \
            if 'fgcolor' in kwargs else None
        flags.append(self.bgcolor[kwargs['bgcolor']]) \
            if 'bgcolor' in kwargs else None

        data = ''
        if self.is_term and flags:
            data += self.begin + (';'.join(flags)) + self.end
        for i in args:
            data += str(i)
        if self.is_term:
            # write 'color disable' before newline to better work with parallel
            # processes writing signle stdout/stderr
            if data.endswith('\n'):
                data = data[:-1] + self.disable + '\n'
            else:
                data += self.disable
        if data:
            self._write(data, kwargs.get('log_only', False))
        self._flush()

    def __call__(self, *args, **kwargs):
        self.write(*args, **kwargs)

    def writeout_unidiff(self, diff):
        for i in diff:

            if not i.endswith('\n'):
                i += "\n\\ No newline\n"

            if i.startswith('+'):
                self.write(i, schema='diff_in')
            elif i.startswith('-'):
                self.write(i, schema='diff_out')
            elif i.startswith('@'):
                self.write(i, schema='diff_mark')
            else:
                self.write(i)

    def flush(self):
        return self.stdout.flush()

    def fileno(self):
        return self.stdout.fileno()

    def isatty(self):
        return self.is_term

    def decolor(self, data):
        return self.color_re.sub('', data)


# Globals
#########


color_stdout = Colorer()


def decolor(data):
    return color_stdout.decolor(data)
