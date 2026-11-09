
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
    private PluginSettings.Mode mode = PluginSettings.Mode.ENFORCE;
    private ObserveNotice observeNotice;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        YamlConfiguration config = readConfig();
        Server server = getServer();
        IllegalArgumentException modeError = null;
        try {
            mode = PluginSettings.readMode(config);
        } catch (IllegalArgumentException e) {
            mode = PluginSettings.Mode.ENFORCE;
            modeError = e;
        }
        Runnable onSynchronized;
        if (mode == PluginSettings.Mode.OBSERVE) {
            ObserveNotice notice = new ObserveNotice(this, this::view, NoticeSettings.read(config), System::nanoTime);
            observeNotice = notice;
            server.getPluginManager().registerEvents(notice, this);
            onSynchronized = () -> scheduleOnMainThread(notice::onSynchronized);
        } else {
            AuthorizationListener listener = new AuthorizationListener(
                    this::view, server::getOnlinePlayers, this::scheduleOnMainThread,
                    config.getString("messages.unauthorized"),
                    config.getString("messages.not-ready"),
                    config.getString("messages.revoked"));
            server.getPluginManager().registerEvents(listener, this);
            onSynchronized = listener::scheduleOnlineCheck;
        }
        if (modeError != null) {
            getLogger().severe("授权配置无效，新登录已拒绝。 " + modeError.getMessage());
            return;
        }
        PluginSettings settings;
        try {
            settings = PluginSettings.read(config);
        } catch (IllegalArgumentException e) {
            String behavior = mode == PluginSettings.Mode.OBSERVE ? "继续观察。" : "新登录已拒绝。";
            getLogger().severe("授权配置无效，" + behavior + " " + e.getMessage());
            return;
        }
        authorization = new AuthorizationService(settings, new SslContextFactory.Client(), getLogger(),
                onSynchronized);
        authorization.start();
    }

    private YamlConfiguration readConfig() {
        YamlConfiguration defaults = YamlConfiguration.loadConfiguration(
                new InputStreamReader(getResource("config.yml"), StandardCharsets.UTF_8));
        YamlConfiguration config = new YamlConfiguration() {
            @Override
            public void set(String path, Object value) {
                // Bukkit drops YAML nulls by default. Preserve a blank mode as an invalid value.
                super.set(path, "mode".equals(path) && value == null ? "" : value);
            }
        };
        try {
            config.load(new File(getDataFolder(), "config.yml"));
        } catch (IOException | InvalidConfigurationException e) {
            // A YAML parse exception can include lines of the private config.
            getLogger().severe("无法读取插件 config.yml，请检查文件权限和 YAML 格式。");
            config = new YamlConfiguration();
        }
        config.setDefaults(defaults);
        return config;
    }

    @Override
    public void onDisable() {
        if (observeNotice != null) {
            observeNotice.close();
            observeNotice = null;
        }
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
            sender.sendMessage("当前模式：" + mode);
            AuthorizationService.View current = view();
            sender.sendMessage("授权服务：" + (current.ready() ? "已就绪" : "未就绪"));
            if (!current.ready()) {
                sender.sendMessage(mode == PluginSettings.Mode.OBSERVE
                        ? "当前行为：允许正常登录，仅观察" : "当前行为：拒绝新玩家登录");
            }
            if (current.snapshot() != null) {
                sender.sendMessage((current.ready() ? "授权版本：" : "最近确认的授权版本：")
                        + current.snapshot().version());
                sender.sendMessage((current.ready() ? "有效授权游戏ID：" : "最近确认的名单人数：")
                        + current.snapshot().activeNames().size());
            }
            sender.sendMessage("入服公告：" + (observeNotice != null && observeNotice.enabled() ? "已启用" : "已关闭"));
            return true;
        }

        return false;
    }
}
