import logging
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app_config import ConfigError, load_config, notify_url, require_token

logger = logging.getLogger("mc_qq_auth")
NOTIFY_TIMEOUT = 3


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
            return response.status == 200
    except (OSError, HTTPException, ValueError) as exc:
        logger.warning("授权通知失败，将等待后端补偿检查：%s", type(exc).__name__)
        return False
