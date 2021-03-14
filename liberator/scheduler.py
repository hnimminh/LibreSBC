import time
import traceback
from threading import Thread
from datetime import date, datetime
import json

import redis
import schedule
import redisdl

from configuration import (CLUSTER_MEMBERS, NODENAME, CLUSTERNAME,
                    REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD, SCAN_COUNT, RACTION_TIMEOUT,
                    LIBREDBDIR)
from utilities import logger

REDIS_CONNECTION_POOL = redis.BlockingConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, decode_responses=True, max_connections=10, timeout=5)


def consistentchoice(members):
    return members[date.today().day % len(members)]


def threaded(func, *params):
    th = Thread(target=func, args=params)
    th.start()


def requeue_stuck_cdr():
    try:
        if NODENAME == consistentchoice(CLUSTER_MEMBERS):
            rdbconn = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, decode_responses=True)
            hourago = int(time.time()) - 3600
            stuck_uuids = rdbconn.zrangebyscore('cdr:inprogress', '-inf', hourago)
            
            pipe = rdbconn.pipeline()
            for uuid in stuck_uuids:
                pipe.exists(f'cdr:detail:{uuid}')
            existings = pipe.execute()

            requeue_uuids = []
            for uuid, existing in zip(stuck_uuids, existings):
                if existing:
                    pipe.rpush('cdr:queue:new', uuid)
                    requeue_uuids.append(uuid)
            pipe.execute()
            if stuck_uuids:
                logger(f"module=liberator, space=scheduler, action=requeue_stuck_cdr, stuck_uuids={stuck_uuids}, requeue_uuids={requeue_uuids}")
    except Exception as e:
        logger(f"module=liberator, space=scheduler, action=requeue_stuck_cdr, exception={e}, tracings={traceback.format_exc()}")


def dbbackup(state=str()):
    try:
        if NODENAME == consistentchoice(CLUSTER_MEMBERS):
            rdbfile = f"{LIBREDBDIR}/{CLUSTERNAME.lower()}-{datetime.now().strftime('%Y%m%d')}{state}.libredb.json"
            with open(rdbfile, 'w') as dbf:
                redisdl.dump(dbf, host=REDIS_HOST, port=REDIS_PORT, db=0, password=REDIS_PASSWORD, pretty=True)
            logger(f"module=liberator, space=scheduler, action=dbbackup, libredb={rdbfile}")
            #txt = redisdl.dumps(host=REDIS_HOST, port=REDIS_PORT, db=0, password=REDIS_PASSWORD,)
            #redisdl.loads(txt, host=REDIS_HOST, port=REDIS_PORT, db=1, password=REDIS_PASSWORD,)
            #with open(rdbfile) as dbf:
            #    redisdl.load(dbf, host=REDIS_HOST, port=REDIS_PORT, db=11, password=REDIS_PASSWORD, use_expireat=True)
    except Exception as e:
        logger(f"module=liberator, space=scheduler, action=dbbackup, exception={e}, tracings={traceback.format_exc()}")


class Scheduler(Thread):
    def __init__(self):
        self.stop = False
        Thread.__init__(self)
        self.setName('Scheduler')

    def run(self):
        try:
            logger(f"module=liberator, space=scheduler, action=start_scheduler_thread")
            # execute startup time only
            threaded(dbbackup, '.startup')
            # schedule job
            schedule.every().hour.at(':32').do(threaded, requeue_stuck_cdr)
            schedule.every().day.at('03:37').do(threaded, dbbackup)
            while not self.stop:
                # run pending job
                schedule.run_pending()
                time.sleep(30)
        except Exception as e:
            logger(f"module=liberator, space=scheduler, class=Scheduler, action=run, exception={e}, tracings={traceback.format_exc()}")
            time.sleep(2)
        finally: pass
