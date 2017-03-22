#!/usr/bin/env python2

import sys
import time
import multiprocessing

import lib

from lib.colorer import Colorer
color_stdout = Colorer()


def run_worker(gen_worker, task_queue, worker_id):
    worker = gen_worker(worker_id)
    worker.run_all(task_queue)


def main_loop(processes, task_queues):
    """ 'processes' and 'task_queues' passed as arguments to allow access to it
        from a caller in case of exception will raised.
    """
    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')
    worker_next_id = 1
    for basket in lib.task_baskets().values():
        tasks = basket['tasks']
        if not tasks:
            continue
        task_queue = multiprocessing.JoinableQueue()
        task_queues.append(task_queue)
        for task in tasks:
            task_queue.put(task.name)
        task_queue.put(None)  # 'stop worker' marker
        # It's python-style closure; XXX: prettify
        entry = lambda gen_worker=basket['gen_worker'], \
            task_queue=task_queue, worker_next_id=worker_next_id: \
            run_worker(gen_worker, task_queue, worker_next_id)
        worker_next_id += 1
        process = multiprocessing.Process(target=entry)
        process.start()
        processes.append(process)
        task_queue.close()

    for task_queue in task_queues:
        task_queue.join()
        task_queues.remove(task_queue)
    for process in processes:
        process.join()
        processes.remove(process)


def main():
    processes = []
    task_queues = []

    try:
        main_loop(processes, task_queues)
    except KeyboardInterrupt as e:
        color_stdout('\n[Main process] Caught keyboard interrupt;' \
            ' waiting for processes for doing its clean up\n', schema='test_var')
    finally:
        #for process in processes:
        #    if process.is_alive():
        #        process.terminate()
        #    processes.remove(process)
        pass


if __name__ == "__main__":
    exit(main())
