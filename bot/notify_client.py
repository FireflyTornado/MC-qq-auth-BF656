import json
import logging
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app_config import ConfigError, load_config, notify_url, require_token

logger = logging.getLogger("mc_qq_auth")
NOTIFY_TIMEOUT = 3
MAX_NOTIFY_RESPONSE = 4096


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Send the internal credential directly to the configured endpoint only.
opener = build_opener(ProxyHandler({}), _NoRedirect())


def notify_auth_updated() -> bool:
    """通知 FastAPI 授权已更新，失败不影响数据库事务。"""
    try:
        config = load_config()
        token = require_token(config, "MC_NOTIFY_TOKEN")
        url = notify_url(config)
    except ConfigError as exc:
        logger.warning("内部通知配置无效：%s", exc)
        return False

    try:
        request = Request(url, data=b"", method="POST", headers={"Authorization": f"Bearer {token}"})
        with opener.open(request, timeout=NOTIFY_TIMEOUT) as response:
            if response.status != 200:
                logger.warning("授权通知返回 HTTP %d，将等待后端补偿检查", response.status)
                return False
            body = response.read(MAX_NOTIFY_RESPONSE + 1)
            result = json.loads(body) if len(body) <= MAX_NOTIFY_RESPONSE else None
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or type(result.get("version")) is not int
                or result["version"] < 0
                or type(result.get("delivered")) is not int
                or result["delivered"] < 0
            ):
                logger.warning("授权通知未收到成功确认，将等待后端补偿检查")
                return False
            return True
    except (OSError, HTTPException, ValueError) as exc:
        logger.warning("授权通知失败，将等待后端补偿检查：%s", type(exc).__name__)
        return False
