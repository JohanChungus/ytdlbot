#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - database.py
# 12/7/21 16:57
#

__author__ = "Benny <benny.think@gmail.com>"

import base64
import contextlib
import datetime
import logging
import os
import re
import sqlite3
import subprocess
import time
from io import BytesIO

import fakeredis
import pymysql
import redis
import requests
from beautifultable import BeautifulTable
from influxdb import InfluxDBClient

from config import MYSQL_HOST, MYSQL_PASS, MYSQL_USER, REDIS
from utils import sizeof_fmt

init_con = sqlite3.connect(":memory:", check_same_thread=False)


class FakeMySQL:
    @staticmethod
    def cursor() -> "Cursor":
        return Cursor()

    def commit(self):
        pass

    def close(self):
        pass


class Cursor:
    def __init__(self):
        self.con = init_con
        self.cur = self.con.cursor()

    def execute(self, *args, **kwargs):
        sql = self.sub(args[0])
        new_args = (sql,) + args[1:]
        return self.cur.execute(*new_args, **kwargs)

    def fetchall(self):
        return self.cur.fetchall()

    def fetchone(self):
        return self.cur.fetchone()

    @staticmethod
    def sub(sql):
        sql = re.sub(r"CHARSET.*|charset.*", "", sql, re.IGNORECASE)
        sql = sql.replace("%s", "?")
        return sql


class Redis:
    def __init__(self):
        if REDIS:
            self.r = redis.StrictRedis(host=REDIS, db=0, decode_responses=True)
        else:
            self.r = fakeredis.FakeStrictRedis(host=REDIS, db=0, decode_responses=True)

        db_banner = "=" * 20 + "DB data" + "=" * 20
        quota_banner = "=" * 20 + "Quota" + "=" * 20
        metrics_banner = "=" * 20 + "Metrics" + "=" * 20
        usage_banner = "=" * 20 + "Usage" + "=" * 20
        vnstat_banner = "=" * 20 + "vnstat" + "=" * 20
        self.final_text = f"""
{db_banner}
%s


{vnstat_banner}
%s


{quota_banner}
%s


{metrics_banner}
%s


{usage_banner}
%s
        """
        super().__init__()

    def __del__(self):
        self.r.close()

    def update_metrics(self, metrics):
        logging.info(f"Setting metrics: {metrics}")
        all_ = f"all_{metrics}"
        today = f"today_{metrics}"
        self.r.hincrby("metrics", all_)
        self.r.hincrby("metrics", today)

    @staticmethod
    def generate_table(header, all_data: "list"):
        table = BeautifulTable()
        for data in all_data:
            table.rows.append(data)
        table.columns.header = header
        table.rows.header = [str(i) for i in range(1, len(all_data) + 1)]
        return table

    def show_usage(self):
        db = MySQL()
        db.cur.execute("select user_id,payment_amount,old_user,token from payment")
        data = db.cur.fetchall()
        fd = []
        for item in data:
            fd.append([item[0], item[1], item[2], item[3]])
        db_text = self.generate_table(["ID", "pay amount", "old user", "token"], fd)

        fd = []
        hash_keys = self.r.hgetall("metrics")
        for key, value in hash_keys.items():
            if re.findall(r"^today|all", key):
                fd.append([key, value])
        fd.sort(key=lambda x: x[0])
        metrics_text = self.generate_table(["name", "count"], fd)

        fd = []
        for key, value in hash_keys.items():
            if re.findall(r"\d+", key):
                fd.append([key, value])
        fd.sort(key=lambda x: int(x[-1]), reverse=True)
        usage_text = self.generate_table(["UserID", "count"], fd)

        fd = []
        for key in self.r.keys("*"):
            if re.findall(r"^\d+$", key):
                value = self.r.get(key)
                date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.r.ttl(key) + time.time()))
                fd.append([key, value, sizeof_fmt(int(value)), date])
        fd.sort(key=lambda x: int(x[1]))
        quota_text = self.generate_table(["UserID", "bytes", "human readable", "refresh time"], fd)

        # vnstat
        if os.uname().sysname == "Darwin":
            cmd = "/opt/homebrew/bin/vnstat -i en0".split()
        else:
            cmd = "/usr/bin/vnstat -i eth0".split()
        vnstat_text = subprocess.check_output(cmd).decode("u8")
        return self.final_text % (db_text, vnstat_text, quota_text, metrics_text, usage_text)

    def reset_today(self):
        pairs = self.r.hgetall("metrics")
        for k in pairs:
            if k.startswith("today"):
                self.r.hdel("metrics", k)

    def user_count(self, user_id):
        self.r.hincrby("metrics", user_id)

    def generate_file(self):
        text = self.show_usage()
        file = BytesIO()
        file.write(text.encode("u8"))
        date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        file.name = f"{date}.txt"
        return file

    def add_send_cache(self, unique, file_id):
        self.r.hset("cache", unique, file_id)

    def get_send_cache(self, unique) -> "str":
        return self.r.hget("cache", unique)

    def del_send_cache(self, unique):
        return self.r.hdel("cache", unique)


class MySQL:
    vip_sql = """
    CREATE TABLE if not exists `payment`
    (
        `user_id`        bigint NOT NULL,
        `payment_amount` float        DEFAULT NULL,
        `payment_id`     varchar(256) DEFAULT NULL,
        `old_user`       tinyint(1)   DEFAULT NULL,
        `token`          int          DEFAULT NULL,
        UNIQUE KEY `payment_id` (`payment_id`)
    ) CHARSET = utf8mb4
                """

    settings_sql = """
    create table if not exists  settings
    (
        user_id    bigint          not null,
        resolution varchar(128) null,
        method     varchar(64)  null,
        mode varchar(32) default 'Celery' null,
        constraint settings_pk
            primary key (user_id)
    );
            """

    channel_sql = """
    create table if not exists channel
    (
        link              varchar(256) null,
        title             varchar(256) null,
        description       text         null,
        channel_id        varchar(256),
        playlist          varchar(256) null,
        latest_video varchar(256) null,
        constraint channel_pk
            primary key (channel_id)
    ) CHARSET=utf8mb4;
    """

    subscribe_sql = """
    create table if not exists subscribe
    (
        user_id    bigint       null,
        channel_id varchar(256) null,
        is_valid boolean default 1 null
    ) CHARSET=utf8mb4;
    """

    def __init__(self):
        if MYSQL_HOST:
            self.con = pymysql.connect(
                host=MYSQL_HOST, user=MYSQL_USER, passwd=MYSQL_PASS, db="ytdl", charset="utf8mb4"
            )
        else:
            self.con = FakeMySQL()

        self.cur = self.con.cursor()
        self.init_db()
        super().__init__()

    def init_db(self):
        self.cur.execute(self.vip_sql)
        self.cur.execute(self.settings_sql)
        self.cur.execute(self.channel_sql)
        self.cur.execute(self.subscribe_sql)
        self.con.commit()

    def __del__(self):
        self.con.close()

    def get_user_settings(self, user_id: "str") -> "tuple":
        cur = self.con.cursor()
        cur.execute("SELECT * FROM settings WHERE user_id = %s", (user_id,))
        data = cur.fetchone()
        if data is None:
            return 100, "high", "video", "Celery"
        return data

    def set_user_settings(self, user_id: int, field: "str", value: "str"):
        cur = self.con.cursor()
        cur.execute("SELECT * FROM settings WHERE user_id = %s", (user_id,))
        data = cur.fetchone()
        if data is None:
            resolution = method = ""
            if field == "resolution":
                method = "video"
                resolution = value
            if field == "method":
                method = value
                resolution = "high"
            cur.execute("INSERT INTO settings VALUES (%s,%s,%s,%s)", (user_id, resolution, method, "Celery"))
        else:
            cur.execute(f"UPDATE settings SET {field} =%s WHERE user_id = %s", (value, user_id))
        self.con.commit()


class InfluxDB:
    def __init__(self):
        self.client = InfluxDBClient(host=os.getenv("INFLUX_HOST", "192.168.7.233"), database="celery")
        self.data = None

    def __del__(self):
        self.client.close()

    @staticmethod
    def get_worker_data():
        username = os.getenv("FLOWER_USERNAME", "benny")
        password = os.getenv("FLOWER_PASSWORD", "123456abc")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        r = requests.get("https://celery.dmesg.app/dashboard?json=1", headers=headers)
        if r.status_code != 200:
            return dict(data=[])
        return r.json()

    def extract_dashboard_data(self):
        self.data = self.get_worker_data()
        json_body = []
        for worker in self.data["data"]:
            load1, load5, load15 = worker["loadavg"]
            t = {
                "measurement": "tasks",
                "tags": {
                    "hostname": worker["hostname"],
                },
                "time": datetime.datetime.utcnow(),
                "fields": {
                    "task-received": worker.get("task-received", 0),
                    "task-started": worker.get("task-started", 0),
                    "task-succeeded": worker.get("task-succeeded", 0),
                    "task-failed": worker.get("task-failed", 0),
                    "active": worker.get("active", 0),
                    "status": worker.get("status", False),
                    "load1": load1,
                    "load5": load5,
                    "load15": load15,
                },
            }
            json_body.append(t)
        return json_body

    def __fill_worker_data(self):
        json_body = self.extract_dashboard_data()
        self.client.write_points(json_body)

    def __fill_overall_data(self):
        active = sum([i["active"] for i in self.data["data"]])
        json_body = [{"measurement": "active", "time": datetime.datetime.utcnow(), "fields": {"active": active}}]
        self.client.write_points(json_body)

    def __fill_redis_metrics(self):
        json_body = [{"measurement": "metrics", "time": datetime.datetime.utcnow(), "fields": {}}]
        r = Redis().r
        hash_keys = r.hgetall("metrics")
        for key, value in hash_keys.items():
            if re.findall(r"^today", key):
                json_body[0]["fields"][key] = int(value)

        self.client.write_points(json_body)

    def collect_data(self):
        if os.getenv("INFLUX_HOST") is None:
            return

        with contextlib.suppress(Exception):
            self.data = self.get_worker_data()
            self.__fill_worker_data()
            self.__fill_overall_data()
            self.__fill_redis_metrics()
            logging.debug("InfluxDB data was collected.")
