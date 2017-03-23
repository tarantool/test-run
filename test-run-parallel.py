#!/usr/bin/env python2

import sys
import time
import select
import multiprocessing
from multiprocessing.queues import SimpleQueue
import lib
from lib.colorer import Colorer


color_stdout = Colorer()


def run_worker(gen_worker, task_queue, result_queue, worker_id):
    color_stdout.queue = result_queue
    worker = gen_worker(worker_id)
    worker.run_all(task_queue, result_queue)


def main_loop():
    processes = []
    task_queues = []
    result_queues = []

    color_stdout("Started {0}\n".format(" ".join(sys.argv)), schema='tr_text')
    worker_next_id = 1
    for basket in lib.task_baskets().values():
        tasks = basket['tasks']
        if not tasks:
            continue
        result_queue = SimpleQueue()
        result_queues.append(result_queue)
        task_queue = SimpleQueue()
        task_queues.append(task_queue)
        for task in tasks:
            task_queue.put((task.name, task.conf_name)) # XXX: task.id
        task_queue.put((None, None))  # 'stop worker' marker
        # It's python-style closure; XXX: prettify
        entry = lambda gen_worker=basket['gen_worker'], \
                task_queue=task_queue, result_queue=result_queue, \
                worker_next_id=worker_next_id: \
            run_worker(gen_worker, task_queue, result_queue, worker_next_id)
        worker_next_id += 1

        process = multiprocessing.Process(target=entry)
        process.start()
        processes.append(process)

    if not processes:
        return

    stats = dict()
    inputs = [q._reader for q in result_queues]
    workers_cnt = len(inputs)
    while workers_cnt > 0:
        ready_inputs, _, _ = select.select(inputs, [], [])
        for ready_input in ready_inputs:
            idx = inputs.index(ready_input) # XXX: tmp
            result_queue = result_queues[inputs.index(ready_input)]
            obj = result_queue.get()
            if obj is None:
                workers_cnt -= 1
                break
            elif isinstance(obj, bool):
                if not idx in stats.keys():
                    stats[idx] = {
                        'pass': 0,
                        'othr': 0,
                    }
                if obj:
                    stats[idx]['pass'] += 1
                else:
                    stats[idx]['othr'] += 1
            elif isinstance(obj, str):
                sys.stdout.write(obj)

    color_stdout('Statistics: %s\n' % str(stats))

    for process in processes:
        process.join()
        processes.remove(process)


def main():
    try:
        main_loop()
    except KeyboardInterrupt as e:
        color_stdout('\n[Main process] Caught keyboard interrupt;' \
            ' waiting for processes for doing its clean up\n', schema='test_var')


if __name__ == "__main__":
    exit(main())
