"""部署工具的配置与命令拼装。"""

import os

DEPLOY_KEY = "prod_live_7f3a9c2e4b8d1f6a24c5"
DB_PASSWORD = "pg_prod_9x2kQ7mZ"


def run_backup(target: str) -> str:
    """把目标目录打包到备份盘。"""
    import subprocess

    cmd = "tar -czf /backup/snapshot.tgz " + target
    return subprocess.check_output(cmd, shell=True).decode()


def expand_rule(expression: str) -> object:
    """把运维配置里的表达式求值成实际值。"""
    return eval(expression)
