package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.OfflinePlayer;
import org.bukkit.command.ConsoleCommandSender;
import org.bukkit.configuration.file.YamlConfiguration;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class SudoCommandsTest {
    @TempDir
    Path directory;

    private final Server server = mock(Server.class);
    private final ConsoleCommandSender console = mock(ConsoleCommandSender.class);

    @ParameterizedTest
    @ValueSource(strings = {"op Steve", "stop", "minecraft:whitelist add Steve", "whitelist remove Steve",
            "whitelist addall Steve", "whitelist-extra add Steve", "whitelist add Steve\nsay Hi",
            "whitelist add Steve\u2028stop"})
    void rejectsCommandsOutsideTheTokenPrefix(String input) {
        assertEquals("rejected", new SudoCommands(server, Set.of("whitelist add")).execute(input).status());
        verifyNoInteractions(server);
    }

    @Test
    void emptyFileAllowsNothingAndInvalidRulesFailClosed() throws Exception {
        Path file = directory.resolve(SudoCommands.ALLOWLIST_FILE);
        Files.writeString(file, "# empty\n\n");
        var commands = new SudoCommands(server, SudoCommands.readAllowlist(file));
        assertEquals("rejected", commands.execute("list").status());
        verifyNoInteractions(server);
        Files.writeString(file, "whitelist\n/whitelist add\n");
        var error = assertThrows(IllegalArgumentException.class, () -> SudoCommands.readAllowlist(file));
        assertTrue(error.getMessage().contains("第 2 行"));
    }

    @Test
    void configuredPrefixesDispatchWholeCommandsWithOriginalArguments() throws Exception {
        Path file = directory.resolve(SudoCommands.ALLOWLIST_FILE);
        Files.writeString(file, "\uFEFF# UTF-8\r\n whitelist add \r\nwhitelist add\r\nexample:inspect world\r\n");
        var prefixes = SudoCommands.readAllowlist(file);
        assertEquals(Set.of("whitelist add", "example:inspect world"), prefixes);
        var commands = new SudoCommands(server, prefixes);
        when(server.getConsoleSender()).thenReturn(console);
        when(server.dispatchCommand(console, "minecraft:whitelist add Steve Alex")).thenReturn(true);
        when(server.dispatchCommand(console, "example:inspect world \"夜间 测试\"")).thenReturn(true);
        assertEquals("ok", commands.execute("whitelist add Steve Alex").status());
        assertEquals("ok", commands.execute("example:inspect world \"夜间 测试\"").status());
        verify(server).dispatchCommand(console, "minecraft:whitelist add Steve Alex");
        verify(server).dispatchCommand(console, "example:inspect world \"夜间 测试\"");
    }

    @Test
    void rootPrefixIncludesExactCommandAndEverySubcommand() {
        var commands = new SudoCommands(server, Set.of("whitelist"));
        when(server.getConsoleSender()).thenReturn(console);
        when(server.dispatchCommand(eq(console), anyString())).thenReturn(true);
        OfflinePlayer player = mock(OfflinePlayer.class);
        when(player.getName()).thenReturn("Alex");
        when(server.getWhitelistedPlayers()).thenReturn(Set.of(player));
        for (String input : Set.of("whitelist", "whitelist on", "whitelist remove Steve", "whitelist add Alex")) {
            assertEquals("ok", commands.execute(input).status());
            verify(server).dispatchCommand(console, "minecraft:" + input);
        }
        when(server.getCommandAliases()).thenReturn(Map.of("minecraft:whitelist", new String[]{"first", "second"}));
        clearInvocations(server);
        assertEquals("rejected", commands.execute("whitelist on").status());
        verify(server, never()).getConsoleSender();
    }

    @Test
    void enabledConfigurationRequiresSecureChannel() {
        var config = new YamlConfiguration();
        config.set("sudo.enabled", false);
        assertNull(SudoChannel.Settings.read(config, "auth-token"));
        config.set("sudo.enabled", true);
        config.set("sudo.token", "distinct-token");
        config.set("sudo.websocket-url", "ws://example.net/internal/mc/sudo/ws");
        assertThrows(IllegalArgumentException.class, () -> SudoChannel.Settings.read(config, "auth-token"));
        config.set("sudo.websocket-url", "wss://example.net/internal/mc/sudo/ws");
        assertNotNull(SudoChannel.Settings.read(config, "auth-token"));
    }
}
