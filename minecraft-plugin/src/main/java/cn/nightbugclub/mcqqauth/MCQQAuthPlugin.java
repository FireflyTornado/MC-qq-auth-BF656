
package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.configuration.InvalidConfigurationException;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.plugin.IllegalPluginAccessException;
import org.bukkit.plugin.java.JavaPlugin;
import org.eclipse.jetty.util.ssl.SslContextFactory;

import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

public final class MCQQAuthPlugin extends JavaPlugin {

    private volatile AuthorizationService authorization;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        YamlConfiguration config = readConfig();
        Server server = getServer();
        AuthorizationListener listener = new AuthorizationListener(
                this::view, server::getOnlinePlayers, this::scheduleOnMainThread,
                config.getString("messages.unauthorized"),
                config.getString("messages.not-ready"),
                config.getString("messages.revoked"));
        server.getPluginManager().registerEvents(listener, this);
        PluginSettings settings;
        try {
            settings = PluginSettings.read(config);
        } catch (IllegalArgumentException e) {
            getLogger().severe("授权配置无效，新登录已拒绝。 " + e.getMessage());
            return;
        }
        authorization = new AuthorizationService(settings, new SslContextFactory.Client(), getLogger(),
                listener::scheduleOnlineCheck);
        authorization.start();
    }

    private YamlConfiguration readConfig() {
        YamlConfiguration defaults = YamlConfiguration.loadConfiguration(
                new InputStreamReader(getResource("config.yml"), StandardCharsets.UTF_8));
        YamlConfiguration config = new YamlConfiguration();
        try {
            config.load(new File(getDataFolder(), "config.yml"));
        } catch (IOException | InvalidConfigurationException e) {
            // A YAML parse exception can include lines of the private config.
            getLogger().severe("无法读取插件 config.yml，请检查文件权限和 YAML 格式。");
            return defaults;
        }
        config.setDefaults(defaults);
        return config;
    }

    @Override
    public void onDisable() {
        AuthorizationService service = authorization;
        authorization = null;
        if (service != null) {
            service.close();
        }
    }

    private AuthorizationService.View view() {
        AuthorizationService service = authorization;
        return service == null ? AuthorizationService.View.OFFLINE : service.view();
    }

    private void scheduleOnMainThread(Runnable task) {
        try {
            getServer().getScheduler().runTask(this, task);
        } catch (IllegalPluginAccessException e) {
            if (isEnabled()) {
                throw e;
            }
        }
    }

    @Override
    public boolean onCommand(
            CommandSender sender,
            Command command,
            String label,
            String[] args
    ) {
        if (command.getName().equalsIgnoreCase("mcqqauth")) {
            sender.sendMessage("MCQQAuth v" + getDescription().getVersion());
            AuthorizationService.View current = view();
            sender.sendMessage(current.ready() ? "授权服务已就绪" : "授权服务未就绪，新登录已拒绝");
            if (current.snapshot() != null) {
                sender.sendMessage("最近确认的授权版本 " + current.snapshot().version()
                        + "，名单人数 " + current.snapshot().activeNames().size());
            }
            return true;
        }

        return false;
    }
}
