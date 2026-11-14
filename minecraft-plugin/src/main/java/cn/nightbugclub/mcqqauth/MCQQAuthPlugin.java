
package cn.nightbugclub.mcqqauth;

import cn.nightbugclub.mcqqauth.PluginSettings.Mode;
import org.bukkit.Server;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.ConsoleCommandSender;
import org.bukkit.configuration.InvalidConfigurationException;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.plugin.IllegalPluginAccessException;
import org.bukkit.plugin.java.JavaPlugin;
import org.eclipse.jetty.util.ssl.SslContextFactory;
import org.yaml.snakeyaml.error.YAMLException;

import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;

public final class MCQQAuthPlugin extends JavaPlugin {

    private volatile AuthorizationService authorization;
    private SudoChannel sudoChannel;
    private SudoCommands sudoCommands;
    private AuthorizationModeController modes;
    private AuthorizationListener listener;
    private ObserveNotice observeNotice;
    private boolean changingMode;

    @Override
    public void onEnable() {
        Server server = getServer();
        YamlConfiguration config;
        Mode initialMode = Mode.ENFORCE;
        boolean validMode = false;
        try {
            saveDefaultConfig();
            config = readConfig();
            initialMode = PluginSettings.readMode(config);
            validMode = true;
        } catch (IOException | InvalidConfigurationException | YAMLException | SecurityException e) {
            // Configuration exceptions can include lines of the private config.
            getLogger().severe("无法读取插件 config.yml，请检查文件权限和 YAML 格式。新登录已拒绝。");
            config = new YamlConfiguration();
        } catch (IllegalArgumentException e) {
            getLogger().severe("授权配置无效，新登录已拒绝。 " + e.getMessage());
            config = new YamlConfiguration();
        }
        config.setDefaults(YamlConfiguration.loadConfiguration(
                new InputStreamReader(getResource("config.yml"), StandardCharsets.UTF_8)));
        AuthorizationModeController controller = new AuthorizationModeController(initialMode);
        modes = controller;
        AuthorizationListener gate = new AuthorizationListener(controller, this::view, server::getOnlinePlayers,
                config.getString("messages.unauthorized"),
                config.getString("messages.not-ready"),
                config.getString("messages.revoked"));
        listener = gate;
        ObserveNotice notice = new ObserveNotice(this, controller, this::view, NoticeSettings.read(config),
                System::nanoTime);
        observeNotice = notice;
        server.getPluginManager().registerEvents(gate, this);
        server.getPluginManager().registerEvents(notice, this);
        notice.listenForAuthMeLogin();
        if (!validMode) {
            return;
        }
        PluginSettings settings;
        try {
            settings = PluginSettings.read(config);
        } catch (IllegalArgumentException e) {
            String behavior = initialMode == Mode.OBSERVE ? "继续观察。" : "新登录已拒绝。";
            getLogger().severe("授权配置无效，" + behavior + " " + e.getMessage());
            return;
        }
        authorization = new AuthorizationService(settings, new SslContextFactory.Client(), getLogger(),
                () -> controller.scheduleSync(this::scheduleOnMainThread, policy -> {
                    if (policy.mode == Mode.OBSERVE) {
                        notice.onSynchronized();
                    } else {
                        gate.checkOnlinePlayers(policy);
                    }
                }));
        authorization.start();
        startSudo(config, settings.token);
    }

    private void startSudo(YamlConfiguration config, String authToken) {
        try {
            createSudoAllowlist();
        } catch (IOException | SecurityException e) {
            getLogger().warning("无法创建 " + SudoCommands.ALLOWLIST_FILE + "，请检查插件目录权限。");
        }
        try {
            SudoChannel.Settings sudo = SudoChannel.Settings.read(config, authToken);
            if (sudo != null) {
                Server server = getServer();
                sudoCommands = new SudoCommands(server, Set.of());
                try {
                    sudoCommands.reloadAllowlist(sudoAllowlistFile());
                } catch (IOException | SecurityException | IllegalArgumentException e) {
                    getLogger().warning(sudoReloadFailure(e));
                }
                sudoChannel = new SudoChannel(sudo, sudoCommands,
                        task -> server.getScheduler().runTask(this, task), getLogger());
                sudoChannel.start();
            }
        } catch (IllegalArgumentException e) {
            getLogger().warning("/sudo 已关闭：" + e.getMessage());
        }
    }

    private Path sudoAllowlistFile() {
        return getDataFolder().toPath().resolve(SudoCommands.ALLOWLIST_FILE);
    }

    private void createSudoAllowlist() throws IOException {
        Path allowlist = sudoAllowlistFile();
        if (Files.notExists(allowlist)) {
            try (var template = MCQQAuthPlugin.class.getResourceAsStream("/" + SudoCommands.ALLOWLIST_FILE)) {
                if (template == null) {
                    throw new IOException("Missing sudo allowlist template");
                }
                Files.copy(template, allowlist);
            } catch (FileAlreadyExistsException ignored) {
                // The administrator created the file while the plugin was starting.
            }
        }
    }

    private YamlConfiguration readConfig() throws IOException, InvalidConfigurationException {
        return ModeConfig.load(Files.readString(configFile(), StandardCharsets.UTF_8));
    }

    private Path configFile() {
        return getDataFolder().toPath().resolve("config.yml");
    }

    @Override
    public void onDisable() {
        sudoCommands = null;
        if (sudoChannel != null) {
            sudoChannel.close();
            sudoChannel = null;
        }
        if (modes != null) {
            modes.close();
            listener.clearExemptions();
        }
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
        if (!command.getName().equalsIgnoreCase("mcqqauth")) {
            return false;
        }
        if (!sender.hasPermission("mcqqauth.admin")) {
            sender.sendMessage("[MCQQAuth] 你没有执行此指令的权限。");
            return true;
        }
        if (args.length > 0 && args[0].equalsIgnoreCase("sudo")) {
            if (!(sender instanceof ConsoleCommandSender) || sender != getServer().getConsoleSender()) {
                sender.sendMessage("[MCQQAuth] 此指令仅限 Minecraft 服务端控制台使用。");
            } else if (args.length != 2 || !args[1].equalsIgnoreCase("reload")) {
                sender.sendMessage("[MCQQAuth] 用法：mcqqauth sudo reload");
            } else if (getServer().isPrimaryThread()) {
                reloadSudo(sender);
            } else {
                scheduleOnMainThread(() -> reloadSudo(sender));
            }
            return true;
        }
        Mode mode = modes.mode();
        if (args.length == 0) {
            sender.sendMessage("MCQQAuth v" + getDescription().getVersion());
            sender.sendMessage("当前模式：" + mode);
            AuthorizationService.View current = view();
            sender.sendMessage("授权服务：" + (current.ready() ? "已就绪" : "未就绪"));
            if (!current.ready()) {
                sender.sendMessage(mode == Mode.OBSERVE
                        ? "当前行为：允许正常登录，仅观察" : "当前行为：拒绝新玩家登录");
            }
            if (current.snapshot() != null) {
                sender.sendMessage((current.ready() ? "授权版本：" : "最近确认的授权版本：")
                        + current.snapshot().version());
                sender.sendMessage((current.ready() ? "有效授权游戏ID：" : "最近确认的名单人数：")
                        + current.snapshot().activeNames().size());
            } else {
                sender.sendMessage("最近确认的授权版本：无");
                sender.sendMessage("最近确认的名单人数：0");
            }
            sender.sendMessage("入服公告：" + (mode == Mode.OBSERVE && observeNotice.enabled() ? "已启用" : "已关闭"));
            return true;
        }
        if (args[0].equalsIgnoreCase("mode")) {
            if (args.length == 1) {
                sender.sendMessage("[MCQQAuth] 当前模式：" + mode);
                String policy = mode == Mode.OBSERVE ? "观察授权状态，不执行登录拦截与在线撤权。"
                        : view().ready() ? "验证新登录并执行在线撤权，切换时获得豁免的会话除外。"
                        : "授权服务未就绪，拒绝新登录。";
                sender.sendMessage("[MCQQAuth] 当前策略：" + policy);
                return true;
            }
            Mode requested = args[1].equalsIgnoreCase("observe") ? Mode.OBSERVE
                    : args[1].equalsIgnoreCase("enforce") ? Mode.ENFORCE : null;
            boolean kick = args.length == 3 && "--kick-online".equals(args[2]) && requested == Mode.ENFORCE;
            if (requested != null && (args.length == 2 || kick)) {
                if (getServer().isPrimaryThread()) {
                    changeMode(sender, requested, kick);
                } else {
                    scheduleOnMainThread(() -> changeMode(sender, requested, kick));
                }
                return true;
            }
        }
        sender.sendMessage("[MCQQAuth] 用法：/mcqqauth mode [observe|enforce [--kick-online]]");
        return true;
    }

    private void reloadSudo(CommandSender sender) {
        SudoCommands commands = sudoCommands;
        if (commands == null) {
            sender.sendMessage("[MCQQAuth] /sudo 功能未启用，请检查插件配置。");
            return;
        }
        // QQ commands also use the Bukkit console sender. Deny reloads routed through that dispatch.
        if (commands.isDispatching()) {
            sender.sendMessage("[MCQQAuth] 此指令仅限 Minecraft 服务端控制台使用。");
            return;
        }
        try {
            int count = commands.reloadAllowlist(sudoAllowlistFile());
            sender.sendMessage("[MCQQAuth] /sudo 允许清单已重载，共 " + count + " 条规则。");
        } catch (IOException | SecurityException | IllegalArgumentException e) {
            sender.sendMessage("[MCQQAuth] " + sudoReloadFailure(e));
        }
    }

    private static String sudoReloadFailure(Exception failure) {
        String detail = failure instanceof IllegalArgumentException
                ? failure.getMessage() : "无法读取 " + SudoCommands.ALLOWLIST_FILE + "，请检查文件路径、权限和 UTF-8 编码。";
        return "/sudo 已暂停。清单加载失败：" + detail + " 修正后请在控制台执行 mcqqauth sudo reload。";
    }

    private void changeMode(CommandSender sender, Mode requested, boolean kick) {
        Mode previous = modes.mode();
        if (previous == null || changingMode) {
            sender.sendMessage("[MCQQAuth] 当前无法执行模式切换，请稍后重试。");
            return;
        }
        changingMode = true;
        try {
            if (kick && !view().ready()) {
                rejectOnlineCheck(sender);
                return;
            }
            if (!persistMode(sender, requested, kick)) {
                return;
            }
            if (previous != requested) {
                applyMode(requested, kick);
                getLogger().info("[管理员操作] " + sender.getName() + " 切换模式 " + previous + " → " + requested);
                if (requested == Mode.OBSERVE) {
                    sender.sendMessage("[MCQQAuth] 已切换至 OBSERVE。授权同步继续运行，后续玩家不再受到 MCQQAuth 白名单拦截。");
                } else if (!kick) {
                    sender.sendMessage("[MCQQAuth] 已切换至 ENFORCE。新登录立即执行授权验证，切换前已在线的玩家不会因此立即被踢出。");
                } else {
                    sender.sendMessage("[MCQQAuth] 已切换至 ENFORCE。新登录立即执行授权验证。");
                }
            } else if (!kick) {
                sender.sendMessage("[MCQQAuth] 当前已是 " + requested + "。");
            }
            if (kick) {
                AuthorizationListener.OnlineCheck result = listener.kickUnauthorizedOnline();
                String summary = (result.complete() ? "在线检查完成" : "授权状态变化，在线检查已中止")
                        + "。已检查 " + result.checked() + " 人，实际踢出 " + result.kicked() + " 人。";
                sender.sendMessage("[MCQQAuth] " + summary);
                getLogger().info("[管理员操作] " + sender.getName() + " 执行 enforce --kick-online：" + summary);
            }
        } finally {
            changingMode = false;
        }
    }

    private boolean persistMode(CommandSender sender, Mode requested, boolean kick) {
        try {
            if (ModeConfig.save(configFile(), requested, () -> !kick || view().ready())) {
                return true;
            }
            rejectOnlineCheck(sender);
        } catch (IllegalArgumentException e) {
            reportSaveFailure(sender, e.getMessage());
        } catch (InvalidConfigurationException | YAMLException e) {
            reportSaveFailure(sender, "请检查 config.yml 的 YAML 格式。");
        } catch (IOException | UnsupportedOperationException | SecurityException e) {
            reportSaveFailure(sender, "无法安全写入 config.yml，请检查文件权限及文件系统。");
        }
        return false;
    }

    private void applyMode(Mode requested, boolean kick) {
        AuthorizationService.View baseline = view();
        listener.clearExemptions();
        if (requested == Mode.ENFORCE) {
            // Protect the sessions first, including if an explicit sweep is interrupted by a disconnect.
            listener.protectCurrentSessions(baseline);
        }
        modes.switchTo(requested);
        if (requested == Mode.ENFORCE) {
            observeNotice.clearVisits();
            if (!kick && view().snapshot() != baseline.snapshot()) {
                // A snapshot published while preparing the transition may have queued under OBSERVE.
                modes.scheduleSync(this::scheduleOnMainThread, listener::checkOnlinePlayers);
            }
        }
    }

    private void rejectOnlineCheck(CommandSender sender) {
        sender.sendMessage("[MCQQAuth] 授权服务未就绪，未切换模式，未执行在线检查。");
        getLogger().info("[管理员操作] " + sender.getName() + " 的 enforce --kick-online 因授权服务未就绪被拒绝。");
    }

    private void reportSaveFailure(CommandSender sender, String reason) {
        sender.sendMessage("[MCQQAuth] 模式未切换。 " + reason);
        getLogger().warning("[管理员操作] " + sender.getName() + " 保存模式失败。 " + reason);
    }
}
