package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommandYamlParser;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.plugin.PluginDescriptionFile;
import org.bukkit.plugin.PluginManager;
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

    @BeforeEach
    void setUp() throws Exception {
        Server server = mock(Server.class);
        doReturn(server).when(plugin).getServer();
        when(server.getPluginManager()).thenReturn(plugins);
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
    void observeRegistersOnlyNoticeListenerAndReportsOfflineBehavior() throws Exception {
        start("mode: OBSERVE");
        verify(plugins).registerEvents(isA(ObserveNotice.class), eq(plugin));
        verify(plugins, never()).registerEvents(isA(AuthorizationListener.class), eq(plugin));
        CommandSender admin = queryAsAdmin();
        verify(admin).sendMessage("MCQQAuth v0.3.0");
        verify(admin).sendMessage("当前模式：OBSERVE");
        verify(admin).sendMessage("授权服务：未就绪");
        verify(admin).sendMessage("当前行为：允许正常登录，仅观察");
        verify(admin).sendMessage("入服公告：已启用");
    }

    @Test
    void enforceRegistersOnlyGateEvenWhenNoticesAreEnabled() throws Exception {
        start("mode: ENFORCE\nobserve: {notice: {enabled: true}}");
        assertLoginDenied();
        CommandSender admin = queryAsAdmin();
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
        start(modeLine + "\n" + """
                api:
                  token: test-only-token
                  snapshot-url: https://localhost:1/internal/mc/snapshot
                  websocket-url: wss://localhost:1/internal/mc/ws
                """);
        assertLoginDenied();
        verify(logger).severe(contains("mode 必须为 OBSERVE 或 ENFORCE"));
    }

    @Test
    void commandUsesExistingBukkitPermission() throws Exception {
        start("mode: OBSERVE");
        Command command = command();
        assertEquals("mcqqauth.admin", command.getPermission());
        CommandSender player = mock(CommandSender.class);
        command.execute(player, "mcqqauth", new String[0]);
        verify(player).hasPermission("mcqqauth.admin");
        verify(player, never()).sendMessage(startsWith("MCQQAuth v"));
    }

    private void start(String yaml) throws Exception {
        Files.writeString(dataFolder.resolve("config.yml"), yaml);
        plugin.onEnable();
    }

    private void assertLoginDenied() {
        var gate = ArgumentCaptor.forClass(AuthorizationListener.class);
        verify(plugins).registerEvents(gate.capture(), eq(plugin));
        verify(plugins, never()).registerEvents(isA(ObserveNotice.class), eq(plugin));
        var event = new AsyncPlayerPreLoginEvent("Steve", InetAddress.getLoopbackAddress(), UUID.randomUUID());
        gate.getValue().onPreLogin(event);
        assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, event.getLoginResult());
    }

    private Command command() {
        return PluginCommandYamlParser.parse(plugin).get(0);
    }

    private CommandSender queryAsAdmin() {
        CommandSender admin = mock(CommandSender.class);
        when(admin.hasPermission("mcqqauth.admin")).thenReturn(true);
        assertTrue(command().execute(admin, "mcqqauth", new String[0]));
        return admin;
    }
}
