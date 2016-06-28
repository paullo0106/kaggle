#!/usr/bin/env python

import os
import glob
import time
import subprocess

import pymongo
import threading
import Queue

import numpy as np

from utils import create_folder, log, INFO, WARN
from facebook.facebook_utils import transform_to_submission_format, save_submission, get_mongo_location
from facebook.facebook_utils import MONGODB_URL, MONGODB_INDEX, MONGODB_VALUE, MONGODB_SCORE, MONGODB_BATCH_SIZE, FULL_SET

def get_full_queue(batch_num=200000):
    max_num = FULL_SET[1]
    batch_idx = max_num/batch_num

    queue = Queue.Queue()
    for idx in xrange(0, batch_idx+1):
        queue.put((idx*batch_num, min(max_num, (idx+1)*batch_num)))

    return queue

def merge_files(filepath_prefix, conf, m, final_submission_filename):
    filepath_final = "{}/final.csv.gz".format(filepath_prefix)
    filepath_final = "{}/{}.{}.{}.3.csv.gz".format(filepath_prefix, os.path.basename(conf).replace(".cfg", ""), m.lower(), "_".join(final_submission_filename))
    rc = subprocess.call("echo 'row_id,place_id' | gzip -9 > {}".format(filepath_final), shell=True)

    for f in glob.iglob("{}/*.csv".format(filepath_prefix)):
        rc = subprocess.call("tail -n +2 {} | gzip -9 >> {}".format(f, filepath_final), shell=True)
        log("Append {} to the end of {}".format(f, filepath_final), INFO)

class NormalizeThread(threading.Thread):
    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, verbose=None):
        threading.Thread.__init__(self, group=group, target=target, name=name, verbose=verbose)

        self.args = args

        for key, value in kwargs.items():
            setattr(self, key, value)

    def run(self):
        mongo = pymongo.MongoClient(MONGODB_URL)
        mm_database, mm_collection = "facebook_checkin_competition", "min_max"

        while True:
            database, collection = self.queue.get()
            log("Start to search the values for {} of {}".format(collection ,database), INFO)

            rmin, rmax = np.inf, -np.inf

            # Check the existing of min, max values for collections
            for r in mongo[mm_database][mm_collection].find({"database": database, "collection": collection}):
                rmin, rmax = r["min"], r["max"]

            # Not found the min/max records
            if rmin == np.inf:
                row_n = 0
                xx, x, n = 0.0, 0.0, 0
                for record in mongo[database][collection].find({}, {MONGODB_VALUE: 1}):
                    for info in record[MONGODB_VALUE]:
                        score = info[MONGODB_SCORE]

                        xx += score**2
                        x += score

                        v_min = min(rmin, score)
                        v_max = max(rmax, score)

                        if v_min != rmin:
                            rmin = v_min

                        if v_max != rmax:
                            rmax = v_max

                        n += 1

                    if row_n % 50000 == 0:
                        log("{}/{}/{}/{}/{} in {} of {}".format(n, x, xx, rmin, rmax, collection, database), INFO)
                    row_n += 1

                avg = x/n
                std = np.sqrt(xx/n - avg**2)

                mongo[mm_database][mm_collection].insert({"database": database, "collection": collection, "std": std, "avg": avg, "n": n, "min": rmin, "max": rmax})

            log("Get {}/{} from {} of {}".format(rmin, rmax, collection, database), INFO)

            self.queue.task_done()

        mongo.close()

class AggregatedThread(threading.Thread):
    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, verbose=None):
        threading.Thread.__init__(self, group=group, target=target, name=name, verbose=verbose)

        self.args = args

        for key, value in kwargs.items():
            setattr(self, key, value)

    def run(self):
        mongo = pymongo.MongoClient(MONGODB_URL)
        new_collection = self.collection + "_aggregation"

        idx_min, idx_max = None, None
        while True:
            if idx_min == None or idx_max == None:
                idx_min, idx_max = self.queue.get()
            log("Start to aggregate the values for {} of {}".format(idx_min, idx_max), INFO)

            try:
                pre_row_id = None
                pre_place_ids, pre_grids = [], []
                pool = []

                timestamp_start = time.time()
                for record in mongo[self.database][self.collection].find({MONGODB_INDEX: {"$gte": idx_min, "$lt": idx_max}}).sort([(MONGODB_INDEX, pymongo.ASCENDING)]).batch_size(MONGODB_BATCH_SIZE):
                    row_id = record[MONGODB_INDEX]

                    if pre_row_id != None and pre_row_id != row_id:
                        r = {MONGODB_INDEX: pre_row_id,
                             MONGODB_VALUE: pre_place_ids,
                            "grid": pre_grids}

                        pool.append(r)

                        pre_place_ids = []
                        pre_grids = []

                    pre_row_id = row_id
                    pre_place_ids.extend(record["place_ids"])
                    pre_grids.append(record["grid"])

                r = {MONGODB_INDEX: pre_row_id,
                     MONGODB_VALUE: pre_place_ids,
                     "grid": pre_grids}
                pool.append(r)
                timestamp_end = time.time()
                log("Cost {:4f} secends to query records".format(timestamp_end-timestamp_start), INFO)

                timestamp_start = time.time()
                mongo[self.database][new_collection].insert_many(pool)
                timestamp_end = time.time()
                log("Cost {:4f} secends to insert records".format(timestamp_end-timestamp_start), INFO)
            except pymongo.errors.CursorNotFound as e:
                log(e)
                time.sleep(60)

                continue
            else:
                idx_min, idx_max = None, None

            self.queue.task_done()

        mongo.close()

class WeightedThread(threading.Thread):
    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, verbose=None):
        threading.Thread.__init__(self, group=group, target=target, name=name, verbose=verbose)

        self.args = args

        for key, value in kwargs.items():
            setattr(self, key, value)

    def weighted(self, score, weight):
        return score*weight

    def run(self):
        batch_size = 5000
        mongo = pymongo.MongoClient(MONGODB_URL)

        def scoring(results, row_id, pre_place_ids, avg, std, min_std, adjust, weight, eps):
            results.setdefault(row_id, {})

            if self.mode == "simple":
                for place_ids in pre_place_ids:
                    for place_id in place_ids:
                        results[row_id].setdefault(place_id["place_id"], 0)
                        results[row_id][place_id["place_id"]] += place_id["score"]
            elif self.mode == "weight":
                for place_ids in pre_place_ids:
                    pre_place_id, pre_count = None, 0
                    for p in sorted(place_ids):     # sort by key
                        place_id, s = p["place_id"], p["score"]

                        if pre_place_id and pre_place_id != place_id:
                            results[pre_place_id] /= pre_count
                            pre_count = 0

                        results[row_id].setdefault(place_id, 0)

                        score = self.weighted((s-avg)/std+min_std+eps, weight)*adjust
                        results[row_id][place_id] += score


                        pre_place_id = place_id
                        pre_count += 1

                        #if row_id in [50088, 89377, 440138]:
                        #    log("{} {} {} {} {} {}".format(row_id, place_id, adjust, score, s, results[row_id][place_id]))

                    results[row_id][pre_place_id] /= pre_count
            else:
                raise NotImplementError

        size = 3
        idx_min, idx_max = None, None
        eps = 0.0001
        mm_database, mm_collection = "facebook_checkin_competition", "min_max"
        while True:
            if idx_min == None or idx_max == None:
                idx_min, idx_max = self.queue.get()

            filepath_output = "{}/{}.{}.csv".format(self.filepath_prefix, size, idx_min)
            if os.path.exists(filepath_output):
                log("The {} is done so skipping it".format(filepath_output), INFO)
                idx_min, idx_max = None, None
            else:
                try:
                    results = {}
                    for database, collection, weight, adjust in self.locations:
                        avg, std, min_std = 0, 0, 0
                        for r in mongo[mm_database][mm_collection].find({"database": database, "collection": collection}):
                            avg = r["avg"]
                            std = r["std"]
                            min_std = (r["min"] - r["avg"])/r["std"]*-1+0.0001

                        pre_row_id = None
                        pre_place_ids = []

                        timestamp_start = time.time()
                        for record in mongo[database][collection].find({MONGODB_INDEX: {"$gte": idx_min, "$lt": idx_max}}).sort([(MONGODB_INDEX, pymongo.ASCENDING)]).batch_size(MONGODB_BATCH_SIZE):
                            row_id = record[MONGODB_INDEX]

                            if pre_row_id != None and pre_row_id != row_id:
                                scoring(results, pre_row_id, pre_place_ids, avg, std, min_std, adjust, weight, eps)
                                pre_place_ids = []

                            pre_row_id = row_id
                            pre_place_ids.append(record["place_ids"])

                        if pre_row_id:
                            scoring(results, pre_row_id, pre_place_ids, avg, std, min_std, adjust, weight, eps)

                        timestamp_end = time.time()
                        log("Cost {:4f} secends to finish this job({} - {}) from {} of {} with {}".format((timestamp_end-timestamp_start), idx_min, idx_max, collection, database, weight), INFO)

                    csv = transform_to_submission_format(results, size)
                    save_submission(filepath_output, csv, size, is_full=[idx_min, idx_max])
                except pymongo.errors.CursorNotFound as e:
                    log(e, WARN)
                    time.sleep(60)

                    continue
                else:
                    idx_min, idx_max = None, None

            self.queue.task_done()

        mongo.close()