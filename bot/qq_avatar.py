import re
from collections import OrderedDict
from contextlib import closing
from http.client import HTTPSConnection
from io import BytesIO
from threading import Lock
from time import monotonic

from nonebot import logger
from PIL import Image, ImageOps


AVATAR_HOST = "q1.qlogo.cn"
AVATAR_TIMEOUT = 2
MAX_AVATAR_BYTES = 128 * 1024
MAX_AVATAR_SIDE = 512
AVATAR_TTL = 30 * 60
FAILURE_TTL = 60
MAX_CACHED_AVATARS = 8
_cache: OrderedDict[str, tuple[float, Image.Image | None]] = OrderedDict()
_cache_lock = Lock()


def is_qq_id(value: str) -> bool:
    return re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def _download_avatar(qq_id: str) -> Image.Image | None:
    deadline = monotonic() + AVATAR_TIMEOUT
    # Direct HTTPS to a fixed host. HTTP redirects and environment proxies are not used.
    with closing(HTTPSConnection(AVATAR_HOST, timeout=AVATAR_TIMEOUT)) as connection:
        connection.request("GET", f"/g?b=qq&nk={qq_id}&s=100")
        with connection.getresponse() as response:
            if response.status != 200:
                return None
            data = bytearray()
            while len(data) <= MAX_AVATAR_BYTES and monotonic() < deadline:
                chunk = response.read1(min(8192, MAX_AVATAR_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            else:
                return None
    with Image.open(BytesIO(data), formats=("JPEG", "PNG", "WEBP", "GIF")) as image:
        if max(image.size) > MAX_AVATAR_SIDE:
            return None
        return ImageOps.fit(image.convert("RGB"), (144, 144), method=Image.Resampling.LANCZOS)


def get_avatar(qq_id: str) -> Image.Image | None:
    if not is_qq_id(qq_id):
        return None
    with _cache_lock:
        cached = _cache.get(qq_id)
        if cached is not None and cached[0] > monotonic():
            _cache.move_to_end(qq_id)
            return cached[1]
    try:
        avatar = _download_avatar(qq_id)
    except Exception as exc:
        # Transport and image decoding are both optional parts of the status card.
        logger.debug("[MC-QQ-AUTH] QQ 头像不可用（{}）", type(exc).__name__)
        avatar = None
    with _cache_lock:
        ttl = AVATAR_TTL if avatar is not None else FAILURE_TTL
        _cache[qq_id] = (monotonic() + ttl, avatar)
        _cache.move_to_end(qq_id)
        if len(_cache) > MAX_CACHED_AVATARS:
            _cache.popitem(last=False)
    return avatar
