import pytest
import psutil
from pathlib import Path
from gevent import subprocess
from lib import Options
from lib.tarantool_server import TarantoolLog


@pytest.yield_fixture(scope="session", autouse=True)
def clean_all_subprocesses() -> None:
    """Kill remained subprocess. Raise an exception of not killed procs."""
    current_process = psutil.Process()
    children = current_process.children(recursive=True)
    yield
    not_terminated_processes = []
    for child in children:
        if psutil.pid_exists(child.pid):
            not_terminated_processes.append(child)
            child.terminate()
    if not_terminated_processes:
        raise Exception(
            "Next processes were not terminated: {}\n".format(
                not_terminated_processes))


@pytest.fixture
def tarantool_log(log_file: Path) -> TarantoolLog:
    tarantool_log = TarantoolLog(log_file)
    return tarantool_log


@pytest.yield_fixture
def log_file() -> Path:
    dir_path = Path(__file__).resolve().parent
    with open(dir_path / 'outfile.txt', 'w') as file:
        ret = subprocess.Popen(
            ['../test-run.py',
             'hang.test.lua'], cwd=dir_path, stdout=file)
        ret.wait(timeout=Options().args.no_output_timeout + 10)
    yield Path(dir_path / 'outfile.txt')
    ret.kill()

