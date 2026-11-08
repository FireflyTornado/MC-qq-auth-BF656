from contextlib import chdir

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

from app_config import (
    BOT_DIR,
    ENV_FILE,
    ConfigError,
    load_config,
    onebot_ws_urls,
    require_token,
    target_group_id,
)


def init_bot() -> None:
    config = load_config()
    group_id = target_group_id(config)
    ws_urls = onebot_ws_urls(config)
    access_token = None
    if config.get("ONEBOT_ACCESS_TOKEN"):
        access_token = require_token(config, "ONEBOT_ACCESS_TOKEN")

    # NoneBot resolves plugin module names and its own Env relative to cwd.
    with chdir(BOT_DIR):
        # NoneBot can log the full config at DEBUG/TRACE during initialization.
        nonebot.logger.disable("nonebot")
        try:
            nonebot.init(
                _env_file=(ENV_FILE,),
                target_group_id=group_id,
                onebot_v11_ws_urls=ws_urls,
                onebot_v11_access_token=access_token,
            )
            driver = nonebot.get_driver()
            if isinstance(driver.config.log_level, str):
                nonebot.logger.level(driver.config.log_level)
            driver.register_adapter(Adapter)
        except (AttributeError, ImportError, KeyError, RuntimeError, ValueError):
            raise ConfigError("NoneBot 初始化失败，请检查 DRIVER、LOG_LEVEL 和 OneBot 配置") from None
        finally:
            nonebot.logger.enable("nonebot")

        nonebot.load_plugins("plugins")


init_bot()

if __name__ == "__main__":
    nonebot.run()
