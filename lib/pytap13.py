# Copyright 2013, Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Author: Josef Skladanka <jskladan@redhat.com>

import re
try:
    from CStringIO import StringIO
except ImportError:
    from StringIO import StringIO

import yaml


RE_VERSION = re.compile(r"^\s*TAP version 13\s*$")
RE_PLAN = re.compile(
    r"^\s*(?P<start>\d+)\.\.(?P<end>\d+)\s*(#\s*(?P<explanation>.*))?\s*$")
RE_TEST_LINE = re.compile(
    r"^\s*(?P<result>(not\s+)?ok)\s*(?P<id>\d+)?\s*(?P<description>[^#]+)?" +
    r"\s*(#\s*(?P<directive>TODO|SKIP)?\s*(?P<comment>.+)?)?\s*$",
    re.IGNORECASE)
RE_DIAGNOSTIC = re.compile(r"^\s*#\s*(?P<diagnostic>.+)?\s*$")
RE_YAMLISH_START = re.compile(r"^\s*---.*$")
RE_YAMLISH_END = re.compile(r"^\s*\.\.\.\s*$")


class Test(object):
    def __init__(self, result, id, description=None, directive=None,
                 comment=None):
        self.result = result
        self.id = id
        self.description = description
        try:
            self.directive = directive.upper()
        except AttributeError:
            self.directive = directive
        self.comment = comment
        self.yaml = None
        self._yaml_buffer = StringIO()
        self.diagnostics = []


class TAP13(object):
    def __init__(self, strict=False):
        self.tests = []
        self.__tests_counter = 0
        self.tests_planned = None
        self.strict = strict

    def _parse(self, source):
        seek_version = True
        seek_plan = False
        seek_test = False

        in_test = False
        in_yaml = False
        for line in source:
            if not seek_version and RE_VERSION.match(line):
                raise ValueError("Bad TAP format, multiple TAP headers")

            if in_yaml:
                if RE_YAMLISH_END.match(line):
                    test = self.tests[-1]
                    try:
                        test.yaml = yaml.safe_load(
                            test._yaml_buffer.getvalue())
                    except Exception as e:
                        if not self.strict:
                            continue
                        test_num = len(self.tests) + 1
                        comment = 'DIAG: Test %s has wrong YAML: %s' % (
                            test_num, str(e))
                        self.tests.append(Test('not ok', test_num,
                                               comment=comment))
                    in_yaml = False
                else:
                    self.tests[-1]._yaml_buffer.write(line)
                continue

            if in_test:
                if RE_DIAGNOSTIC.match(line):
                    self.tests[-1].diagnostics.append(line.strip())
                    continue
                if RE_YAMLISH_START.match(line):
                    in_yaml = True
                    continue

            on_top_level = not line.startswith('    ')
            raw_line = line.rstrip('\n')
            line = line.strip()

            if RE_DIAGNOSTIC.match(line):
                continue

            # this is "beginning" of the parsing, skip all lines until
            # version is found (in non-strict mode)
            if seek_version:
                m = RE_VERSION.match(line)
                if m:
                    seek_version = False
                    seek_plan = True
                    seek_test = True
                    continue
                elif not self.strict:
                    continue

            m = RE_PLAN.match(line)
            if m:
                if seek_plan and on_top_level:
                    d = m.groupdict()
                    self.tests_planned = int(d.get('end', 0))
                    seek_plan = False

                    # Stop processing if tests were found before the plan
                    #    if plan is at the end, it must be the last line
                    #    -> stop processing
                    if self.__tests_counter > 0:
                        break
                    continue
                elif not on_top_level:
                    continue

            if seek_test:
                m = RE_TEST_LINE.match(line)
                if m and on_top_level:
                    self.__tests_counter += 1
                    t_attrs = m.groupdict()
                    if t_attrs['id'] is None:
                        t_attrs['id'] = self.__tests_counter
                    t_attrs['id'] = int(t_attrs['id'])
                    if t_attrs['id'] < self.__tests_counter:
                        raise ValueError(
                            "Descending test id on line: %r" % line)
                    # according to TAP13 specs, missing tests must be handled
                    # as 'not ok'
                    # here we add the missing tests in sequence
                    while t_attrs['id'] > self.__tests_counter:
                        comment = 'DIAG: Test %s not present' % \
                            self.__tests_counter
                        self.tests.append(Test('not ok', self.__tests_counter,
                                               comment=comment))
                        self.__tests_counter += 1
                    t = Test(**t_attrs)
                    self.tests.append(t)
                    in_test = True
                    continue
                elif not on_top_level:
                    continue

            if self.strict:
                raise ValueError('Wrong TAP line: [' + raw_line + ']')

        if self.tests_planned is None:
            # TODO: raise better error than ValueError
            raise ValueError("Missing plan in the TAP source")

        if len(self.tests) != self.tests_planned:
            comment = 'DIAG: Expected %s tests, got %s' % \
                (self.tests_planned, len(self.tests))
            self.tests.append(Test('not ok', len(self.tests), comment=comment))

    def parse(self, source):
        if isinstance(source, (str, unicode)):
            self._parse(StringIO(source))
        elif hasattr(source, "__iter__"):
            self._parse(source)


if __name__ == "__main__":
    input = """
    TAP version 13
    ok 1 - Input file opened
    not ok 2 - First line of the input valid
        ---
        message: 'First line invalid'
        severity: fail
        data:
          got: 'Flirble'
          expect: 'Fnible'
        ...
    ok - Read the rest of the file
    not ok 5 - Summarized correctly # TODO Not written yet
        ---
        message: "Can't make summary yet"
        severity: todo
        ...
    ok  Description
    # Diagnostic
        ---
        message: 'Failure message'
        severity: fail
        data:
        got:
            - 1
            - 3
            - 2
        expect:
            - 1
            - 2
            - 3
    ...
    1..6
"""
    t = TAP13()
    t.parse(input)

    import pprint
    for test in t.tests:
        print(test.result, test.id, test.description, "#", test.directive,
              test.comment)
        pprint.pprint(test._yaml_buffer)
        pprint.pprint(test.yaml)
