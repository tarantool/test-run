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


def main():
    options = lib.options
    processes = []
    task_queues = []

    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')
    worker_next_id = 1
    for basket in lib.task_baskets().values():
        tasks = basket['tasks']
        if not tasks:
            continue
        task_queue = multiprocessing.JoinableQueue()
        task_queues.append(task_queue)
        import os
        for task in tasks:
            task_queue.put(task.name)
        task_queue.put(None)  # 'stop worker' mark
        # It's python-style closure; XXX: prettify
        entry = lambda gen_worker=basket['gen_worker'], \
            task_queue=task_queue, worker_next_id=worker_next_id: \
            run_worker(gen_worker, task_queue, worker_next_id)
        worker_next_id += 1
        process = multiprocessing.Process(target=entry)
        process.start()
        processes.append(process)
    # TODO: timeouts for workers (processes)
    for i, task_queue in enumerate(task_queues):
        task_queue.join()
        del task_queues[i]
    for i, process in enumerate(processes):
        process.join()
        del processes[i]
    return 0


if __name__ == "__main__":
    exit(main())
