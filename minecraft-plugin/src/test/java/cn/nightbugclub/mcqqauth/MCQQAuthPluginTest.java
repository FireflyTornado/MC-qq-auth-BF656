package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.ConsoleCommandSender;
import org.bukkit.command.PluginCommandYamlParser;
import org.bukkit.entity.Player;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.plugin.PluginDescriptionFile;
import org.bukkit.plugin.PluginManager;
import org.bukkit.permissions.PermissionDefault;
import org.bukkit.scheduler.BukkitScheduler;
import org.bukkit.scheduler.BukkitTask;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.mockito.ArgumentCaptor;

import java.io.InputStream;
import java.net.InetAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.List;
import java.util.Queue;
import java.util.Set;
import java.util.UUID;
import java.util.logging.Logger;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class MCQQAuthPluginTest {
    @TempDir
    Path dataFolder;

    private final MCQQAuthPlugin plugin = mock(MCQQAuthPlugin.class, CALLS_REAL_METHODS);
    private final PluginManager plugins = mock(PluginManager.class);
    private final Logger logger = mock(Logger.class);
    private final Server server = mock(Server.class);
    private final Queue<Runnable> mainTasks = new ArrayDeque<>();

    private static final String TEST_API = """
            api:
              token: test-only-token
              snapshot-url: https://localhost:1/internal/mc/snapshot
              websocket-url: wss://localhost:1/internal/mc/ws
            """;

    @BeforeEach
    void setUp() throws Exception {
        doReturn(server).when(plugin).getServer();
        when(server.getPluginManager()).thenReturn(plugins);
        when(server.isPrimaryThread()).thenReturn(true);
        doReturn(List.of()).when(server).getOnlinePlayers();
        BukkitScheduler scheduler = mock(BukkitScheduler.class);
        when(server.getScheduler()).thenReturn(scheduler);
        when(scheduler.runTask(eq(plugin), any(Runnable.class))).thenAnswer(call -> {
            mainTasks.add(call.getArgument(1));
            return mock(BukkitTask.class);
        });
        doReturn(dataFolder.toFile()).when(plugin).getDataFolder();
        doNothing().when(plugin).saveDefaultConfig();
        doReturn(logger).when(plugin).getLogger();
        doReturn(true).when(plugin).isEnabled();
        doAnswer(call -> getClass().getResourceAsStream("/config.yml")).when(plugin).getResource("config.yml");
        try (InputStream descriptor = getClass().getResourceAsStream("/plugin.yml")) {
            doReturn(new PluginDescriptionFile(descriptor)).when(plugin).getDescription();
        }
    }

    @Test
    void sudoAllowlistIsCreatedEmptyAndExistingRulesArePreserved() throws Exception {
        Path allowlist = dataFolder.resolve(SudoCommands.ALLOWLIST_FILE);
        start("mode: OBSERVE\n" + TEST_API);
        assertEquals(Set.of(), SudoCommands.readAllowlist(allowlist));
        plugin.onDisable();
        Files.writeString(allowlist, "whitelist add\n");
        start("mode: OBSERVE\n" + TEST_API);
        assertEquals(Set.of("whitelist add"), SudoCommands.readAllowlist(allowlist));
    }

    @Test
    void bothListenersRegisterOnceAndObserveReportsOfflineBehavior() throws Exception {
        start("mode: OBSERVE");
        verify(plugins).registerEvents(isA(ObserveNotice.class), eq(plugin));
        verify(plugins).registerEvents(isA(AuthorizationListener.class), eq(plugin));
        CommandSender admin = admin(CommandSender.class);
        execute(admin);
        verify(admin).sendMessage("MCQQAuth v0.4.1");
        verify(admin).sendMessage("当前模式：OBSERVE");
        verify(admin).sendMessage("授权服务：未就绪");
        verify(admin).sendMessage("当前行为：允许正常登录，仅观察");
        verify(admin).sendMessage("入服公告：已启用");
        clearInvocations(admin);
        execute(admin, "mode");
        verify(admin).sendMessage("[MCQQAuth] 当前模式：OBSERVE");
        verify(admin).sendMessage("[MCQQAuth] 当前策略：观察授权状态，不执行登录拦截与在线撤权。");
        verifyNoMoreInteractions(ignoreStubs(admin));
    }

    @Test
    void enforceDisablesNoticesEvenWhenConfigured() throws Exception {
        start("mode: ENFORCE\nobserve: {notice: {enabled: true}}");
        assertLoginDenied();
        CommandSender admin = admin(CommandSender.class);
        execute(admin);
        verify(admin).sendMessage("当前行为：拒绝新玩家登录");
        verify(admin).sendMessage("入服公告：已关闭");
    }

    @Test
    void legacyFileAndUnreadableYamlBothKeepTheGateClosed() throws Exception {
        start("# 0.2.0 config\n");
        assertLoginDenied();
        clearInvocations(plugins);
        start("mode: [OBSERVE");
        assertLoginDenied();
        verify(logger).severe(startsWith("无法读取插件 config.yml"));
    }

    @ParameterizedTest
    @ValueSource(strings = {"mode: TEST", "mode:", "mode: null"})
    void invalidModeIsLoggedAndKeepsTheGateClosed(String modeLine) throws Exception {
        start(modeLine + "\n" + TEST_API);
        assertLoginDenied();
        verify(logger).severe(contains("mode 必须为 OBSERVE 或 ENFORCE"));
    }

    @Test
    void commandUsesExistingBukkitPermission() throws Exception {
        start("mode: OBSERVE");
        Command command = command();
        assertEquals("mcqqauth.admin", command.getPermission());
        assertEquals(PermissionDefault.OP, plugin.getDescription().getPermissions().get(0).getDefault());
        Player player = mock(Player.class);
        command.execute(player, "mcqqauth", new String[]{"mode", "enforce"});
        verify(player).hasPermission("mcqqauth.admin");
        assertEquals("mode: OBSERVE", Files.readString(config()));
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
    }

    @Test
    void consoleAndOpCanSwitchAndRestartReadsTheSavedMode() throws Exception {
        start("mode: OBSERVE");
        ConsoleCommandSender console = admin(ConsoleCommandSender.class);
        execute(console, "MoDe", "EnFoRcE");
        assertEquals("mode: ENFORCE", Files.readString(config()));
        assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, preLogin("A").getLoginResult());
        plugin.onDisable();
        clearInvocations(plugins);
        plugin.onEnable();
        assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, preLogin("A").getLoginResult());

        Player op = admin(Player.class);
        when(op.isOp()).thenReturn(true);
        execute(op, "mode", "ObSeRvE");
        assertEquals("mode: OBSERVE", Files.readString(config()));
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
    }

    @Test
    void invalidArgumentsAndSameModeHaveNoTransitionSideEffects() throws Exception {
        start("mode: observe # 原样保留");
        CommandSender admin = admin(CommandSender.class);
        execute(admin, "mode", "enforc");
        execute(admin, "mode", "observe", "--kick-online");
        execute(admin, "mode", "enforce", "--kick-online", "extra");
        execute(admin, "mode", "OBSERVE");
        assertEquals("mode: observe # 原样保留", Files.readString(config()));
        verify(logger, never()).info(contains("切换模式"));
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
    }

    @Test
    void offlineCleanupIsRejectedInEitherMode() throws Exception {
        start("mode: OBSERVE");
        CommandSender admin = admin(CommandSender.class);
        Player online = mock(Player.class);
        doReturn(List.of(online)).when(server).getOnlinePlayers();
        execute(admin, "mode", "enforce", "--kick-online");
        assertEquals("mode: OBSERVE", Files.readString(config()));
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
        execute(admin, "mode", "enforce");
        execute(admin, "mode", "enforce", "--kick-online");
        assertEquals("mode: ENFORCE", Files.readString(config()));
        verify(admin, times(2)).sendMessage(contains("授权服务未就绪，未切换模式"));
        verify(online, never()).kickPlayer(anyString());
    }

    @Test
    void malformedAndUnwritableConfigKeepTheActivePolicyAndHideCredentials() throws Exception {
        start("mode: OBSERVE");
        CommandSender admin = admin(CommandSender.class);
        Files.writeString(config(), "mode: OBSERVE\nmode: ENFORCE\n" + TEST_API);
        execute(admin, "mode", "enforce");
        verify(admin).sendMessage(contains("重复的顶层 mode"));
        assertEquals("mode: OBSERVE\nmode: ENFORCE\n" + TEST_API, Files.readString(config()));
        Files.delete(config());
        Files.createDirectory(config());
        execute(admin, "mode", "enforce");
        verify(admin).sendMessage(contains("无法安全写入 config.yml"));
        verify(admin, never()).sendMessage(contains("test-only-token"));
        verify(logger, never()).warning(contains("test-only-token"));
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
    }

    @Test
    void duplicateModeAtStartupFailsClosed() throws Exception {
        start("mode: ENFORCE\n'mode': OBSERVE\n" + TEST_API);
        assertLoginDenied();
        verify(logger).severe(contains("重复的顶层 mode"));
    }

    @Test
    void transitionsReuseTheServiceAndSnapshotAndExplicitCleanupReportsActualKicks() throws Exception {
        var snapshot = new AuthProtocol.Snapshot(9, Set.of("a"));
        var ready = new AuthorizationService.View(snapshot, true);
        try (var services = mockConstruction(AuthorizationService.class, (service, context) ->
                when(service.view()).thenReturn(ready))) {
            start("mode: OBSERVE\n" + TEST_API);
            AuthorizationService service = services.constructed().get(0);
            CommandSender admin = admin(CommandSender.class);
            when(admin.getName()).thenReturn("Console");
            Player authorized = online("A");
            Player unauthorized = online("B");
            doReturn(List.of(authorized, unauthorized)).when(server).getOnlinePlayers();
            doAnswer(call -> {
                when(unauthorized.isOnline()).thenReturn(false);
                return null;
            }).when(unauthorized).kickPlayer(anyString());

            execute(admin, "mode", "enforce");
            verify(unauthorized, never()).kickPlayer(anyString());
            assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
            assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, preLogin("B").getLoginResult());
            execute(admin, "mode", "enforce", "--kick-online");
            verify(unauthorized).kickPlayer(anyString());
            verify(authorized, never()).kickPlayer(anyString());
            verify(admin).sendMessage("[MCQQAuth] 在线检查完成。已检查 2 人，实际踢出 1 人。");
            verify(logger).info(contains("Console 执行 enforce --kick-online"));
            execute(admin, "mode", "observe");
            execute(admin);
            verify(admin).sendMessage("授权版本：9");
            verify(admin).sendMessage("有效授权游戏ID：1");
            assertSame(snapshot, service.view().snapshot());
            assertEquals(1, services.constructed().size());
            verify(service).start();
            verify(service, never()).close();
            verify(plugins).registerEvents(isA(ObserveNotice.class), eq(plugin));
            verify(plugins).registerEvents(isA(AuthorizationListener.class), eq(plugin));
        }
    }

    @Test
    void asyncCommandQueuesMutationAndExplicitSwitchFromObserveCleansUpOnline() throws Exception {
        var ready = new AuthorizationService.View(new AuthProtocol.Snapshot(1, Set.of()), true);
        try (var services = mockConstruction(AuthorizationService.class, (service, context) ->
                when(service.view()).thenReturn(ready))) {
            start("mode: OBSERVE\n" + TEST_API);
            assertEquals(1, services.constructed().size());
            Player online = online("A");
            doReturn(List.of(online)).when(server).getOnlinePlayers();
            when(server.isPrimaryThread()).thenReturn(false);
            execute(admin(ConsoleCommandSender.class), "mode", "enforce", "--kick-online");
            verify(online, never()).kickPlayer(anyString());
            assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
            when(server.isPrimaryThread()).thenReturn(true);
            mainTasks.remove().run();
            assertTrue(Files.readString(config()).startsWith("mode: ENFORCE\n"));
            verify(online).kickPlayer(anyString());
        }
    }

    @Test
    void losingReadinessBeforeSavingLeavesBothModesAndOnlinePlayersUntouched() throws Exception {
        var snapshot = new AuthProtocol.Snapshot(1, Set.of());
        var ready = new AuthorizationService.View(snapshot, true);
        var stale = new AuthorizationService.View(snapshot, false);
        try (var services = mockConstruction(AuthorizationService.class, (service, context) ->
                when(service.view()).thenReturn(ready, stale))) {
            String yaml = "mode: OBSERVE\n" + TEST_API;
            start(yaml);
            assertEquals(1, services.constructed().size());
            Player online = online("A");
            doReturn(List.of(online)).when(server).getOnlinePlayers();
            CommandSender admin = admin(CommandSender.class);
            execute(admin, "mode", "enforce", "--kick-online");
            verify(admin).sendMessage(contains("授权服务未就绪，未切换模式"));
            assertEquals(yaml, Files.readString(config()));
            assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, preLogin("A").getLoginResult());
            verify(online, never()).kickPlayer(anyString());
        }
    }

    private void start(String yaml) throws Exception {
        Files.writeString(config(), yaml);
        plugin.onEnable();
    }

    private void assertLoginDenied() {
        assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, preLogin("Steve").getLoginResult());
    }

    private AsyncPlayerPreLoginEvent preLogin(String name) {
        var gate = ArgumentCaptor.forClass(AuthorizationListener.class);
        verify(plugins).registerEvents(gate.capture(), eq(plugin));
        var event = new AsyncPlayerPreLoginEvent(name, InetAddress.getLoopbackAddress(), UUID.randomUUID());
        gate.getValue().onPreLogin(event);
        return event;
    }

    private Command command() {
        return PluginCommandYamlParser.parse(plugin).get(0);
    }

    private static <T extends CommandSender> T admin(Class<T> type) {
        T admin = mock(type);
        when(admin.hasPermission("mcqqauth.admin")).thenReturn(true);
        return admin;
    }

    private void execute(CommandSender sender, String... args) {
        assertTrue(command().execute(sender, "mcqqauth", args));
    }

    private Path config() {
        return dataFolder.resolve("config.yml");
    }

    private static Player online(String name) {
        Player player = mock(Player.class);
        when(player.getName()).thenReturn(name);
        when(player.isOnline()).thenReturn(true);
        return player;
    }
}
