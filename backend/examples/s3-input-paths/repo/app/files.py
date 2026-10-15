"""上传目录的读取与用户资料加载。"""

import os
import pickle
import requests
import yaml

BASE_DIR = "/srv/uploads"


def read_upload(name: str) -> str:
    """按文件名读取上传目录里的文件。"""
    target = os.path.join(BASE_DIR, name)
    with open(target, encoding="utf-8") as handle:
        return handle.read()


def load_profile(blob: bytes) -> dict:
    """反序列化用户提交的会话快照。"""
    return pickle.loads(blob)


def load_settings(text: str) -> dict:
    """解析用户上传的 YAML 配置。"""
    return yaml.load(text)


def fetch_avatar(url: str) -> bytes:
    """按用户给出的地址抓取头像。"""
    response = requests.get(url, timeout=5)
    return response.content
